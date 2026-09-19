# -*- coding: utf-8 -*-
"""给「AI 编排」和外部程序用的接口层：把界面上能做的事都变成可调用的方法。

两种用法
--------
1) 直接调 Python 函数（带类型注解 + 中文文档串，smolagents / litellm 之类
   可以直接把模块里的函数包成工具）：

       from smart_tool.core import api
       api.create_project("我的项目", scene="web")
       api.set_variables("我的项目", {"账号": "tomsmith"})
       api.add_steps("我的项目", [
           {"action": "navigate", "url": "https://example.com/login"},
           {"action": "fill", "locator": "//input[@id='username']", "value": "{{账号}}"},
           {"action": "click", "locator": "//button[@type='submit']",
            "wait_after": "element_present", "wait_target": "//a[text()='Logout']"},
       ])
       api.add_loop("我的项目", "{{列表}}", body=[{"action": "note", "text": "{{loop.item}}"}])
       api.validate_project("我的项目")
       api.run_project("我的项目")

2) JSON 入口（给 LLM 的 function calling / 给别的程序用）：

       api.describe_tools()                     # OpenAI tools 格式的清单
       api.call_tool("add_steps", {...})         # 永远返回 {"ok": bool, ...}

约定
----
· 项目名＝`projects/` 下的目录名（中文也行）；
· 步骤位置统一用 **index**（0 开始的下标，与 list_steps 返回的一致）；
· 写操作会先校验流程结构，不合法就抛错、**一个字节都不写**；
· 出错统一抛 ApiError（call_tool 会把它转成 {"ok": false, "error": ...}）；
· 变量清单的值都是文本；步骤里用 {{变量名}} 引用。
"""
import inspect
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from smart_tool import paths
from smart_tool.core import blocks, datastore, free_code
from smart_tool.core.auth_store import state_path
from smart_tool.core.data_sources import FILE_FIELDS
from smart_tool.core.project_store import (
    Locator, ProjectStore, SCENE_DESKTOP, SCENE_WEB, Step, list_projects,
)
from smart_tool.core.step_executor import (
    StepExecutor, available_variables, check_variables, library_written_vars,
)


class ApiError(ValueError):
    """接口调用出错（参数不对、项目不存在、结构不合法……）。"""


# ============================================================
# 一、能力清单：让 AI 知道「有哪些动作、每个动作要填什么」
# ============================================================
def f(name: str, type_: str, desc: str, required: bool = False,
      default: Any = None, choices: Optional[List[str]] = None,
      var: bool = False) -> Dict[str, Any]:
    """描述一个字段（给 AI 看的说明书）。"""
    return {"name": name, "type": type_, "desc": desc, "required": required,
            "default": default, "choices": list(choices or []), "var": var}


#: 所有动作都有的两个字段
COMMON_FIELDS = [
    f("title", "str", "自定义名称（画布/列表上显示；留空＝用动作默认名）"),
    f("note", "str", "备注：写给自己的说明（画布上点开能看到）"),
]

#: 步骤后等待（大多数动作都有）
WAIT_FIELDS = [
    f("wait_after", "str", "这一步做完后再等什么", choices=[
        "element_present", "page_load", "url_changed", "network_idle", "manual"]),
    f("wait_target", "str", "等待目标：等元素时填 XPath，等 URL 时填网址片段"),
    f("wait_seconds", "float", "另外固定再等几秒", default=0),
]

#: 需要元素定位的动作共用（点 / 填 / 选）
LOCATOR_FIELD = f(
    "locator", "str|dict",
    "元素定位。直接写 XPath 字符串，或写对象 "
    "{\"type\":\"xpath\"|\"image\",\"value\":\"...\",\"image\":\"img/兜底图.png\"}；"
    "XPath 里可以写 {{元素定位变量名}}", True)

COLLECT_KINDS = [
    ("text", "取文字（默认）"),
    ("attr", "取属性：extra 里写属性名，如 href / src / data-id"),
    ("html", "取这段 HTML（保留子标签）"),
    ("link", "取链接：相对地址自动补成完整网址（要定位到 <a>）"),
    ("image", "下载图片到 data/files/，变量里存文件名"),
    ("file", "下载附件（点链接取文件）"),
    ("shot", "截图：extra 留空＝截这个元素，写「整页」＝整页，写 x,y,宽,高＝截区域"),
]

DATA_SOURCE_TYPES = [
    ("folder", "读文件夹里的文件（每个文件＝一行；通配符如 *.txt）"),
    ("txt", "读单个文本文件（整个文件＝一行）"),
    ("excel", "读 Excel（.xlsx/.xls，首行可为表头）"),
    ("json", "读 JSON 文件（数组或对象）"),
]


def _actions_for(scene: str) -> List[str]:
    want = SCENE_DESKTOP if scene == SCENE_DESKTOP else SCENE_WEB
    return [a for a, spec in ACTION_SPECS.items() if want in spec["scenes"]]


ACTION_SPECS: Dict[str, Dict[str, Any]] = {
    "navigate": {
        "label": "打开网页", "scenes": [SCENE_WEB],
        "desc": "打开一个网址。站点慢就把 nav_timeout 调大；"
                "「只等到网页结构解析完」，页面稳不稳交给 wait_after。",
        "fields": [f("url", "str", "网址，可含 {{变量}}", True),
                   f("nav_timeout", "int", "打开超时秒数", default=120)] + WAIT_FIELDS,
    },
    "click": {
        "label": "点击", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "点一个元素（网页填 XPath；桌面场景填 img/ 里的模板图名）。"
                "配了 locator.image（兜底截图）时，XPath 点不动会自动改用截图匹配。",
        "fields": [LOCATOR_FIELD,
                   f("click_times", "int", "点几次（2＝双击，桌面场景用）", default=1)]
                  + WAIT_FIELDS,
    },
    "fill": {
        "label": "填入", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "往输入框填内容（先点一下拿到焦点再输入）。",
        "fields": [LOCATOR_FIELD, f("value", "str", "要填的内容，可含 {{变量}}", True)]
                  + WAIT_FIELDS,
    },
    "select": {
        "label": "下拉选择", "scenes": [SCENE_WEB],
        "desc": "选下拉框（<select>）里的某一项；value 可以写 option 的 value，"
                "也可以写它显示的文字。",
        "fields": [LOCATOR_FIELD, f("value", "str", "选哪一项", True)] + WAIT_FIELDS,
    },
    "collect": {
        "label": "采集数据", "scenes": [SCENE_WEB],
        "desc": "把页面上的东西采下来：结果追加到 data/records.jsonl，"
                "同时产出变量（列表模式产出 JSON 数组，配「循环」逐行遍历）。",
        "fields": [
            f("output_var", "str", "产出变量名，如 文章列表", True),
            f("collect_mode", "str", "采集方式", default="page",
              choices=["page", "list"]),
            f("collect_row", "str", "列表模式下「每行的定位」XPath（页面里多行时必填）"),
            f("collect_fields", "list", "要采的字段：[{\"name\":\"标题\","
              "\"kind\":\"text|attr|html|link|image|file|shot\","
              "\"locator\":\"在当前行里找的 XPath\",\"extra\":\"属性名/整页/坐标\"}]", True),
        ] + WAIT_FIELDS,
    },
    "read_data": {
        "label": "读取数据", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "读本机文件/文件夹，产出一个列表变量（配「循环」逐项处理）。"
                f"文件类可用字段：{'、'.join(k for k, _ in FILE_FIELDS)}。",
        "fields": [
            f("output_var", "str", "产出变量名，如 素材列表", True),
            f("data_cfg", "dict", "{\"type\":\"folder|txt|excel|json\",\"path\":\"...\","
              "\"pattern\":\"*.txt\",\"recursive\":false,\"encoding\":\"auto\","
              "\"sheet\":\"\",\"has_header\":true,"
              "\"field_map\":[{\"field\":\"content\",\"var\":\"内容\"}]}", True),
        ],
    },
    "script": {
        "label": "自由代码", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "写一个真正的函数（Python / JavaScript），系统自动调用第一个函数。"
                "语法：@名字 读写变量清单、/图片名 读写图片库、"
                "#名字=路径 传文件进来（写在参数表里）；"
                "page 是浏览器页面对象，log() 写日志。",
        "fields": [
            f("script_lang", "str", "语言", default="python",
              choices=["python", "javascript"]),
            f("script_code", "str", "完整的函数定义，如 "
              "def 处理(#表=D:/a.csv):\\n    @结果 = 表\\n    return @结果", True),
            f("script_timeout", "int", "执行超时秒数（到点会真的中断）", default=30),
        ],
    },
    "call": {
        "label": "调用函数", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "调用【函数库】里定义好的函数（一处定义、多处调用）。",
        "fields": [
            f("func_name", "str", "函数名（先 set_function 定义，或列 list_functions 看）", True),
            f("func_args", "str", "实参：`形参名=值`，逗号分隔；值可写 {{变量}} 或字面量"),
            f("script_timeout", "int", "执行超时秒数", default=30),
        ],
    },
    "note": {
        "label": "提示 / 日志", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "画布上的一句话说明；运行时把内容（可含 {{变量}}）打进日志。",
        "fields": [f("text", "str", "要显示/打印的内容，可含 {{变量}}", True)],
    },
    "loop_start": {
        "label": "循环", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "循环的开头（系统会自动补上配对的「循环结束」）。"
                "loop_expr 写数字＝跑几次；写 {{变量}}＝按它的长度跑。"
                "循环体里用 {{loop.item}} / {{loop.index}}。",
        "fields": [f("loop_expr", "str", "循环什么：10 或 {{列表变量}}", True)],
    },
    "condition_start": {
        "label": "条件 if/else", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "条件判断（系统会自动补上「分支」标记与「条件结束」）。"
                "equal＝把 cond_expr 渲染成文本跟各分支的匹配值比；"
                "expr＝Python 表达式，结果 真/假 走第 1/2 个分支。",
        "fields": [
            f("cond_mode", "str", "判断方式", default="equal", choices=["equal", "expr"]),
            f("cond_expr", "str", "判断内容，如 {{loop.item.地区}} 或 "
              "len({{loop.item.内容}}) > 500", True),
            f("cond_branches", "list", "分支：[{\"name\":\"北京\",\"values\":\"北京,上海\"}]"
              "（values 逗号分隔可多个；expr 模式下前两个分支的 values 可留空）", True),
        ],
    },
    "group_start": {
        "label": "组合", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "把连着的一串步骤收成一个组合（系统会自动补上「组合结束」）。"
                "skip_if_logged_in=true 时标记成「登录用」：带着有效登录态跑会整块跳过。",
        "fields": [f("title", "str", "组合名字", True),
                   f("skip_if_logged_in", "bool", "是不是「登录用」组合", default=False)],
    },
    "pause_for_human": {
        "label": "暂停等人工", "scenes": [SCENE_WEB, SCENE_DESKTOP],
        "desc": "停下来等人工处理（验证码/人机验证），满足恢复条件或人工点「继续」后继续。",
        "fields": [
            f("prompt", "str", "提示语（显示在运行小窗上）"),
            f("resume_condition", "str", "恢复条件", default="manual",
              choices=["manual", "url_changed", "element_present", "url_and_element"]),
            f("resume_url", "str", "URL 变化条件：网址里要包含的片段（支持 * 通配）"),
            f("resume_element", "str", "元素出现条件：等哪个 XPath 出现"),
            f("resume_timeout", "int", "最多等几秒", default=300),
        ],
    },
    # ---- 桌面场景专用 ----
    "win_activate": {
        "label": "激活窗口", "scenes": [SCENE_DESKTOP],
        "desc": "把目标程序的窗口切到最前面（桌面场景用）。",
        "fields": [f("win_title", "str", "窗口标题里的一小段，如 记事本", True)],
    },
    "hotkey": {
        "label": "按键", "scenes": [SCENE_DESKTOP],
        "desc": "按快捷键（桌面场景用），如 enter、ctrl+s、alt+f4。",
        "fields": [f("keys", "str", "要按的键", True)],
    },
    "delay": {
        "label": "等待", "scenes": [SCENE_DESKTOP],
        "desc": "纯等几秒（桌面场景用；网页里请用步骤后等待）。",
        "fields": [f("wait_seconds", "float", "等几秒", True)],
    },
}
# 循环结束 / 条件结束 / 分支 / 组合结束 是结构标记，由系统自动补齐，不用手写
AUTO_MARKERS = ("loop_end", "condition_end", "branch", "group_end")


def list_actions(scene: str = "") -> List[Dict[str, Any]]:
    """列出所有能用的动作（可按场景过滤）。

    scene 留空＝全部；"web"＝网页自动化；"desktop"＝桌面应用。
    每个动作给出：动作名、中文名、适用场景、说明、字段清单。
    """
    out = []
    for name, spec in ACTION_SPECS.items():
        if scene and (SCENE_DESKTOP if scene == SCENE_DESKTOP else SCENE_WEB) \
                not in spec["scenes"]:
            continue
        out.append({
            "action": name,
            "label": spec["label"],
            "scenes": list(spec["scenes"]),
            "desc": spec["desc"],
            "fields": spec["fields"] + COMMON_FIELDS,
        })
    return out


def describe_action(action: str) -> Dict[str, Any]:
    """看某个动作要填哪些字段（写步骤前先看一眼，省得来回试）。"""
    spec = ACTION_SPECS.get(str(action).strip())
    if spec is None:
        raise ApiError(f"不认识的 action：{action}。可用动作：{'、'.join(ACTION_SPECS)}")
    return {"action": action, "label": spec["label"], "scenes": spec["scenes"],
            "desc": spec["desc"], "fields": spec["fields"] + COMMON_FIELDS}


def free_code_guide() -> Dict[str, str]:
    """自由代码 / 函数库的语法说明（写脚本节点前先看这个）。"""
    return {
        "python 例子": (
            "def 清洗标题(#价格表=D:/data/价格表.xlsx, 后缀='（已处理）'):\n"
            "    标题 = @原始标题.strip()        # 读变量清单（直接写 标题 也行）\n"
            "    @结果 = 标题 + 后缀             # 等号左边＝写回变量清单\n"
            "    /封面 = 'D:/图片/封面.png'      # 等号左边＝存回图片库\n"
            "    log('处理完：' + @结果)\n"
            "    return @结果"
        ),
        "javascript 例子": (
            "function 清洗标题(#价格表=D:/data/价格表.xlsx) {\n"
            "    const 标题 = 原始标题.trim();   // 读变量清单\n"
            "    @结果 = 标题 + '（JS）';         // 写回变量清单\n"
            "    return 标题;\n"
            "}"
        ),
        "说明": (
            "代码框里写一个完整的函数（Python 或 JS，JS 支持 ES6）；"
            "系统自动调用第一个函数，不用自己写调用。"
            "@名字 读写【变量清单】的变量（读也可以直接写名字；名字带点时必须写 @名字，"
            "如 @loop.item.标题）；/图片名 读＝得到 img 下绝对路径，写＝存回图片库；"
            "#名字=路径 只写在参数表里，函数里用「名字」拿到的就是那个文件路径"
            "（路径可写 {{变量}}）。还能用 log()、page（浏览器页面对象）、"
            "current_url、project_dir。变量里存的都是文本，要算数先 int()/float()。"
            "return 的值只打进日志——要写回变量清单请用 @名字 = 值。"
        ),
    }


# ============================================================
# 二、项目
# ============================================================
def _store(project: str) -> ProjectStore:
    name = str(project or "").strip()
    if not name:
        raise ApiError("项目名不能为空")
    store = ProjectStore(paths.PROJECTS_DIR / name)
    if not store.steps_file.exists():
        raise ApiError(
            f"项目「{name}」不存在。用 list_projects() 看看有哪些，"
            f"或者先 create_project('{name}') 建一个。")
    return store


def list_projects_info() -> List[Dict[str, Any]]:
    """列出所有项目：名字、步数、张数、场景、用了哪些变量。"""
    out = []
    for store in list_projects():
        steps = store.load_steps()
        out.append({
            "name": store.name,
            "dir": str(store.dir),
            "scene": store.load_scene(),
            "steps": len(steps),
            "images": store.image_count(),
            "variables": sorted(store.load_variables()),
            "locators": sorted(store.load_locators()),
            "auth": store.load_auth().get("name", ""),
        })
    return out


def create_project(project: str, scene: str = SCENE_WEB,
                   variables: Optional[Dict[str, str]] = None,
                   overwrite: bool = False) -> Dict[str, Any]:
    """建一个新项目（scene："web" 网页自动化 / "desktop" 桌面应用）。

    overwrite=True 时若同名项目已存在就先删掉重建。
    """
    clean = str(project or "").strip()
    if not clean:
        raise ApiError("项目名不能为空")
    if any(ch in clean for ch in '\\/:*?"<>|'):
        raise ApiError(f"项目名不能包含这些字符：\\ / : * ? \" < > |（现在是「{clean}」）")
    store = ProjectStore(paths.PROJECTS_DIR / clean)
    if store.steps_file.exists():
        if not overwrite:
            raise ApiError(f"项目「{clean}」已经存在了（要覆盖就传 overwrite=True）")
        shutil.rmtree(store.dir)
    store.ensure()
    store.save([], dict(variables or {}), scene=scene)
    return {"name": clean, "dir": str(store.dir), "scene": store.load_scene()}


def delete_project(project: str) -> Dict[str, Any]:
    """删掉一个项目（连目录一起删，慎重）。"""
    store = _store(project)
    shutil.rmtree(store.dir)
    return {"deleted": store.name}


def rename_project(project: str, new_name: str) -> Dict[str, Any]:
    """给项目改名（目录一起改名）。"""
    store = _store(project)
    clean = str(new_name or "").strip()
    if not clean:
        raise ApiError("新名字不能为空")
    target = paths.PROJECTS_DIR / clean
    if target.exists():
        raise ApiError(f"项目「{clean}」已经存在了")
    store.dir.rename(target)
    return {"name": clean, "dir": str(target)}


def get_project(project: str) -> Dict[str, Any]:
    """项目概况：场景、变量、元素定位、登录态、函数库、步骤数。"""
    store = _store(project)
    steps = store.load_steps()
    return {
        "name": store.name,
        "dir": str(store.dir),
        "scene": store.load_scene(),
        "real_mouse": store.load_real_mouse(),
        "step_count": len(steps),
        "variables": store.load_variables(),
        "locators": store.load_locators(),
        "auth": store.load_auth(),
        "functions": [{"name": x["name"], "lang": x["lang"], "params": x["params"],
                       "desc": x["desc"]} for x in store.load_functions()],
        "images": sorted(p.name for p in (store.dir / "img").glob("*")
                         if p.is_file()) if (store.dir / "img").is_dir() else [],
    }


def set_scene(project: str, scene: str) -> Dict[str, Any]:
    """切换项目场景："web"（浏览器）/ "desktop"（桌面应用）。"""
    store = _store(project)
    store.save(store.load_steps(), scene=scene)
    return {"scene": store.load_scene()}


# ============================================================
# 三、变量 / 元素定位 / 登录态 / 函数库
# ============================================================
def get_variables(project: str) -> Dict[str, str]:
    """看变量清单（自定义变量 + 元素定位，运行时两个会合并）。"""
    store = _store(project)
    return store.load_all_variables()


def set_variables(project: str, variables: Dict[str, str],
                  merge: bool = True) -> Dict[str, str]:
    """设置变量（merge=True 时只覆盖/新增传进来的那些，其余保留）。"""
    store = _store(project)
    data = store.load_variables() if merge else {}
    data.update({str(k): str(v) for k, v in (variables or {}).items()})
    store.save_variables(data)
    return store.load_variables()


def delete_variable(project: str, name: str) -> Dict[str, str]:
    """删掉一个变量。"""
    store = _store(project)
    data = store.load_variables()
    data.pop(str(name), None)
    store.save_variables(data)
    return data


def get_locators(project: str) -> Dict[str, str]:
    """看「元素定位」清单（名字 → XPath）。"""
    return _store(project).load_locators()


def set_locators(project: str, locators: Dict[str, str],
                 merge: bool = True) -> Dict[str, str]:
    """设置「元素定位」（名字 → XPath）。步骤里写 {{名字}} 就能复用。"""
    store = _store(project)
    data = store.load_locators() if merge else {}
    data.update({str(k): str(v) for k, v in (locators or {}).items()})
    store.save_locators(data)
    return store.load_locators()


def get_auth(project: str) -> Dict[str, str]:
    """看登录态配置：name＝用哪个登录态；check_locator＝登录后才有的元素。"""
    store = _store(project)
    cfg = store.load_auth()
    cfg["file"] = str(state_path(store.dir, cfg.get("name", ""))) \
        if cfg.get("name") else ""
    cfg["saved"] = bool(cfg.get("name")) and \
        state_path(store.dir, cfg["name"]).exists()
    return cfg


def set_auth(project: str, name: str = "", check_locator: str = "") -> Dict[str, str]:
    """配登录态（跑一次流程会把 cookie 存下来；下次带着它跑，能跳过登录步骤）。"""
    store = _store(project)
    store.save_auth(name, check_locator)
    return get_auth(project)


def clear_auth(project: str) -> Dict[str, Any]:
    """清掉已保存的登录态文件（下次跑会重新登录一遍）。"""
    store = _store(project)
    cfg = store.load_auth()
    if cfg.get("name"):
        p = state_path(store.dir, cfg["name"])
        if p.exists():
            p.unlink()
    return {"cleared": True, "auth": get_auth(project)}


def list_functions(project: str) -> List[Dict[str, Any]]:
    """列出【函数库】里的函数（名字、语言、形参、说明、代码）。"""
    return _store(project).load_functions()


def set_function(project: str, code: str, desc: str = "",
                 lang: str = "python", name: str = "") -> Dict[str, Any]:
    """往【函数库】里写一个函数（一处定义、多处调用）。

    code 里写完整的函数定义（和自由代码节点同一套写法）。
    name 留空时用代码里 def / function 后面的名字。
    """
    store = _store(project)
    name = str(name or "").strip()
    lang = "javascript" if str(lang).lower().startswith("java") else "python"
    fc = free_code.analyze(code, lang, store.load_all_variables(), (),
                           require_file_paths=False)
    if fc.errors:
        raise ApiError("代码有问题：" + "；".join(fc.errors))
    if fc.main is None:
        raise ApiError("代码里要有一个函数定义，如 def 处理():")
    final = name or fc.main.name
    funcs = [x for x in store.load_functions() if x["name"] != final]
    funcs.append({
        "name": final, "lang": lang,
        "params": ", ".join(fc.main.param_names),
        "code": free_code.function_source(code, fc.main) if name else code,
        "desc": desc,
    })
    store.save_functions(funcs)
    return {"name": final, "lang": lang, "params": ", ".join(fc.main.param_names)}


def delete_function(project: str, name: str) -> List[str]:
    """删掉函数库里的一个函数（用到它的「调用函数」节点会报找不到函数）。"""
    store = _store(project)
    funcs = [x for x in store.load_functions() if x["name"] != str(name)]
    store.save_functions(funcs)
    return [x["name"] for x in funcs]


# ============================================================
# 四、步骤（编排的核心）
# ============================================================
def _summary(step: Step) -> str:
    """一句话说清这一步在干什么（给 AI 看进度用）。"""
    a = step.action
    if a == "navigate":
        return f"打开 {step.url}"
    if a in ("click", "fill", "select"):
        loc = (step.locator.value if step.locator else "")
        extra = f" = {step.value}" if a in ("fill", "select") and step.value else ""
        return f"{ACTION_SPECS[a]['label']} {loc}{extra}"
    if a == "collect":
        names = "、".join(str(x.get("name") or "") for x in (step.collect_fields or []))
        mode = "列表" if (step.collect_mode or "page") == "list" else "当前页"
        return f"{mode}采集 {names} → {{{{{step.output_var}}}}}"
    if a == "read_data":
        cfg = step.data_cfg or {}
        return f"读取 {cfg.get('type', '')} {cfg.get('path', '')} → {{{{{step.output_var}}}}}"
    if a == "script":
        fc = free_code.analyze(step.script_code or "", step.script_lang or "python",
                               (), (), require_func=False, require_file_paths=False)
        who = fc.main.name if fc.main else "（还没写函数）"
        return f"自由代码 {step.script_lang}：{who}()"
    if a == "call":
        return f"调用函数 {step.func_name}({step.func_args})"
    if a == "note":
        return "提示：" + (step.text or "").replace("\n", " ")[:40]
    if a == "loop_start":
        return f"循环 {step.loop_expr}"
    if a == "loop_end":
        return "循环结束"
    if a == "condition_start":
        n = len(step.cond_branches or [])
        return f"条件（{step.cond_mode}）{step.cond_expr}，{n} 个分支"
    if a == "branch":
        return "分支"
    if a == "condition_end":
        return "条件结束"
    if a == "group_start":
        return f"组合「{step.title or '组合'}」" + ("（登录用）" if step.skip_if_logged_in else "")
    if a == "group_end":
        return "组合结束"
    if a == "pause_for_human":
        return f"暂停等人工（{step.resume_condition}）：{step.prompt}"
    if a == "win_activate":
        return f"激活窗口 {step.win_title}"
    if a == "hotkey":
        return f"按键 {step.keys}"
    if a == "delay":
        return f"等待 {step.wait_seconds}s"
    return a


def list_steps(project: str, with_notes: bool = False) -> Dict[str, Any]:
    """看项目的流程：顺序、缩进层级、结构块范围（插步骤的时候要用到）。

    返回 steps（每项含 index / id / action / title / summary / depth）和
    blocks（每个循环、条件、组合占了哪几行，以及它的「体内」范围）。
    往某个块里加步骤：用 add_steps(project, [...], at=块的 body_end)。
    """
    store = _store(project)
    steps = store.load_steps()
    dep = blocks.depths(steps)
    nums = blocks.step_numbers(steps)
    items = []
    for i, s in enumerate(steps):
        item = {"index": i, "id": s.id, "action": s.action,
                "title": s.title or "", "summary": _summary(s), "depth": dep[i],
                "number": nums[i]}
        if with_notes:
            item["note"] = s.note or ""
        items.append(item)
    sp_list = []
    for sp in blocks.spans(steps):
        sp_list.append({"kind": sp.kind, "start": sp.start, "end": sp.end,
                        "depth": sp.depth,
                        "body_start": sp.inner_lo, "body_end": sp.inner_hi,
                        "label": _summary(steps[sp.start])})
    return {"name": store.name, "scene": store.load_scene(),
            "steps": items, "blocks": sp_list}


def _step_from_dict(d: Dict[str, Any]) -> Step:
    """把 AI/JSON 传来的字典变成一个 Step（顺手把常见错误说清楚）。"""
    if not isinstance(d, dict):
        raise ApiError(f"步骤应该是一个对象，收到的是 {type(d).__name__}")
    action = str(d.get("action") or "").strip()
    spec = ACTION_SPECS.get(action)
    if spec is None:
        if action in AUTO_MARKERS:
            raise ApiError(
                f"「{action}」是结构标记，不用自己写："
                "循环用 add_loop()、条件用 add_condition()、组合用 add_group()，"
                "系统会自动补齐配对标记。")
        raise ApiError(f"不认识的 action：{action}。可用动作：{'、'.join(ACTION_SPECS)}")

    common = {"title", "note", "pos", "id", "action"}
    allowed = common | {x["name"] for x in spec["fields"]}
    unknown = [k for k in d if k not in allowed]
    if unknown:
        raise ApiError(
            f"「{action}」没有这些字段：{'、'.join(unknown)}。"
            f"可以填的是：{'、'.join(sorted(allowed - {'id', 'action'}))}")

    kw: Dict[str, Any] = {}
    for item in spec["fields"]:
        name = item["name"]
        if name in d and d[name] is not None:
            kw[name] = d[name]
    missing = [item["name"] for item in spec["fields"]
               if item["required"] and not str(d.get(item["name"], "")).strip()]
    if missing:
        raise ApiError(f"「{action}」少了必填字段：{'、'.join(missing)}"
                       f"（用 describe_action('{action}') 看怎么写）")

    # 定位：允许直接写 XPath 字符串，也允许写 {"type","value","image"}
    loc = d.get("locator")
    if loc is not None:
        if isinstance(loc, str):
            kw["locator"] = Locator(type="xpath", value=loc)
        elif isinstance(loc, dict):
            kw["locator"] = Locator(type=str(loc.get("type") or "xpath"),
                                    value=str(loc.get("value") or ""),
                                    image=str(loc.get("image") or ""))
        else:
            raise ApiError("locator 要么写 XPath 字符串，要么写 {type,value,image}")
    if action == "condition_start" and not d.get("cond_branches"):
        kw["cond_branches"] = [blocks.new_branch("分支 1"), blocks.new_branch("分支 2")]
    if action == "collect" and d.get("collect_fields"):
        kw["collect_fields"] = [dict(x) for x in d["collect_fields"] if isinstance(x, dict)]
    if action == "read_data" and d.get("data_cfg"):
        kw["data_cfg"] = dict(d["data_cfg"])

    step = Step(id=int(d.get("id") or 0), action=action, **kw)
    step.title = str(d.get("title") or "")
    step.note = str(d.get("note") or "")
    if isinstance(d.get("pos"), (list, tuple)) and len(d["pos"]) >= 2:
        step.pos = [float(d["pos"][0]), float(d["pos"][1])]
    return step


def _finish(steps: List[Step]) -> List[Step]:
    """保存前统一处理：补分支清单、重排 id。"""
    blocks.normalize_branch_lists(steps)
    for i, s in enumerate(steps, start=1):
        s.id = i
    return steps


def _save_checked(store: ProjectStore, steps: List[Step]) -> List[Step]:
    """校验结构后写盘（不合法就报错、不动文件）。"""
    problem = blocks.validate(steps)
    if problem:
        raise ApiError(f"这么改会让流程结构不合法：{problem}\n"
                       "（循环 / 条件 / 组合的配对标记不能拆开："
                       "往块里加步骤请用块的 body_end 位置）")
    _finish(steps)
    store.save(steps)
    return steps


def add_steps(project: str, steps: List[Dict[str, Any]],
              at: Optional[int] = None) -> Dict[str, Any]:
    """加步骤（一次可以加一串）。

    at＝插到第几个步骤之前（0 开始；留空＝追加到最后）。
    要插进某个循环/分支/组合里，就把 at 设成那个块的 body_end
    （list_steps 的 blocks 里有）。
    加「循环 / 条件 / 组合」请用 add_loop / add_condition / add_group，
    它们会自动补配对标记。
    """
    store = _store(project)
    current = store.load_steps()
    new = [_step_from_dict(x) for x in (steps or [])]
    if not new:
        raise ApiError("steps 是空的，没东西可加")
    pos = len(current) if at is None else int(at)
    if not 0 <= pos <= len(current):
        raise ApiError(f"at 超出范围：{pos}（当前一共 {len(current)} 个步骤，"
                       f"合法范围 0~{len(current)}）")
    result = current[:pos] + new + current[pos:]
    _save_checked(store, result)
    return {"added": len(new), "at": pos,
            "range": [pos, pos + len(new) - 1],
            "steps": [_summary(s) for s in new]}


def update_step(project: str, index: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    """改某一步：只传要改的字段（就地合并，其余保持不变）。

    如 update_step("项目", 3, {"value": "{{新内容}}", "wait_seconds": 2})。
    """
    store = _store(project)
    steps = store.load_steps()
    i = int(index)
    if not 0 <= i < len(steps):
        raise ApiError(f"index 超出范围：{i}（当前 {len(steps)} 个步骤）")
    merged = steps[i].to_dict()
    merged.update({k: v for k, v in (patch or {}).items() if k != "action"})
    if patch and patch.get("action") and patch["action"] != steps[i].action:
        raise ApiError("不能改动作类型（action）：请删掉这一步再重新加一个")
    steps[i] = _step_from_dict(merged)
    _save_checked(store, steps)
    return {"index": i, "summary": _summary(steps[i])}


def delete_steps(project: str, index: int, count: int = 1) -> Dict[str, Any]:
    """删步骤（count＝删几个；删「循环/条件/组合」的开始标记时整块一起删）。"""
    store = _store(project)
    steps = store.load_steps()
    i, n = int(index), max(1, int(count))
    if not 0 <= i < len(steps):
        raise ApiError(f"index 超出范围：{i}（当前 {len(steps)} 个步骤）")
    end = i
    for start, stop in _block_ranges(steps):
        if start == i:
            end = stop
    end = max(end, i + n - 1)
    if end >= len(steps):
        raise ApiError("要删的范围超出末尾了")
    removed = steps[i:end + 1]
    result = steps[:i] + steps[end + 1:]
    _save_checked(store, result)
    return {"removed": len(removed), "summaries": [_summary(s) for s in removed]}


def _block_ranges(steps: List[Step]) -> List[tuple]:
    """每个块（循环/条件/组合）的 (开始下标, 结束下标)。"""
    return [(sp.start, sp.end) for sp in blocks.spans(steps)
            if sp.kind in ("loop", "condition", "group")]


def move_step(project: str, index: int, to: int) -> Dict[str, Any]:
    """把某一步挪到别的位置（to＝目标下标，0 开始）。"""
    store = _store(project)
    steps = store.load_steps()
    i, j = int(index), int(to)
    if not 0 <= i < len(steps):
        raise ApiError(f"index 超出范围：{i}")
    if not 0 <= j < len(steps):
        raise ApiError(f"to 超出范围：{j}（当前 {len(steps)} 个步骤）")
    step = steps.pop(i)
    steps.insert(j, step)
    _save_checked(store, steps)
    return {"moved": _summary(step), "from": i, "to": j}


def add_loop(project: str, loop_expr: str, body: Optional[List[Dict]] = None,
             at: Optional[int] = None, title: str = "", note: str = "") -> Dict[str, Any]:
    """加一个循环（自动补「循环结束」）。

    loop_expr：数字＝跑几次；{{变量}}＝按它的长度跑。
    body：循环体里的步骤（可选，之后也能用 add_steps 往 body_end 里插）。
    """
    store = _store(project)
    steps = store.load_steps()
    pos = len(steps) if at is None else int(at)
    inner = [_step_from_dict(x) for x in (body or [])]
    new = [Step(id=0, action="loop_start", loop_expr=str(loop_expr),
                title=title, note=note)] + inner + [Step(id=0, action="loop_end")]
    result = steps[:pos] + new + steps[pos:]
    _save_checked(store, result)
    return {"range": [pos, pos + len(new) - 1],
            "body_range": [pos + 1, pos + len(inner)],
            "summary": f"循环 {loop_expr}（{len(inner)} 个步骤在体内）"}


def add_condition(project: str, cond_expr: str,
                  branches: Optional[List[Dict[str, Any]]] = None,
                  cond_mode: str = "equal", at: Optional[int] = None,
                  title: str = "", note: str = "") -> Dict[str, Any]:
    """加一个条件判断（自动补「分支」标记与「条件结束」）。

    branches：[{"name": "北京", "values": "北京,上海", "steps": [...步骤...]}, ...]
    · equal 模式：values 是匹配值（逗号分隔可多个，必填）；
    · expr 模式：cond_expr 算出来 真/假 → 走第 1/2 个分支（values 可留空）。
    都不匹配就整段跳过。
    """
    store = _store(project)
    steps = store.load_steps()
    pos = len(steps) if at is None else int(at)
    items = branches or [{"name": "分支 1"}, {"name": "分支 2"}]
    if len(items) < 1:
        raise ApiError("至少要有一个分支")
    new: List[Step] = [Step(id=0, action="condition_start", cond_mode=cond_mode,
                            cond_expr=str(cond_expr), title=title, note=note,
                            cond_branches=[blocks.new_branch(
                                str(x.get("name") or f"分支 {i + 1}"),
                                str(x.get("values") or "")) for i, x in enumerate(items)])]
    body_ranges = []
    for item in items:
        new.append(Step(id=0, action="branch"))
        start = len(new)
        new.extend([_step_from_dict(x) for x in (item.get("steps") or [])])
        body_ranges.append([pos + start, pos + len(new) - 1])
    new.append(Step(id=0, action="condition_end"))
    result = steps[:pos] + new + steps[pos:]
    _save_checked(store, result)
    return {"range": [pos, pos + len(new) - 1], "branch_bodies": body_ranges,
            "summary": f"条件（{cond_mode}）{cond_expr}，{len(items)} 个分支"}


def add_group(project: str, title: str, body: Optional[List[Dict]] = None,
              skip_if_logged_in: bool = False, at: Optional[int] = None,
              note: str = "") -> Dict[str, Any]:
    """把一串步骤收成一个「组合」（自动补「组合结束」）。

    skip_if_logged_in=True＝标记成「登录用」：带着有效登录态跑会整块跳过。
    """
    store = _store(project)
    steps = store.load_steps()
    pos = len(steps) if at is None else int(at)
    inner = [_step_from_dict(x) for x in (body or [])]
    new = [Step(id=0, action="group_start", title=str(title), note=note,
                skip_if_logged_in=bool(skip_if_logged_in))] + inner \
        + [Step(id=0, action="group_end")]
    result = steps[:pos] + new + steps[pos:]
    _save_checked(store, result)
    return {"range": [pos, pos + len(new) - 1],
            "body_range": [pos + 1, pos + len(inner)],
            "summary": f"组合「{title}」（{len(inner)} 个步骤）"}


def clear_steps(project: str) -> Dict[str, Any]:
    """清空流程（保留变量、函数库、图片库）。"""
    store = _store(project)
    store.save([])
    return {"cleared": True}


def available_variables_of(project: str) -> List[str]:
    """这个项目里能用的变量名（自定义 + 元素定位 + 节点产出 + loop.*）。"""
    store = _store(project)
    return available_variables(store.load_steps(), store.load_all_variables(),
                               library_written_vars(store.dir))


# ============================================================
# 五、校验 + 运行
# ============================================================
def validate_project(project: str) -> Dict[str, Any]:
    """跑之前的体检：结构、变量来源、自由代码写法，一次都看一遍。

    返回 {"ok": bool, "structure": "", "variables": [...], "code": [...]}。
    """
    store = _store(project)
    steps = store.load_steps()
    problems: List[str] = []
    structure = blocks.validate(steps) or ""
    if structure:
        problems.append(structure)
    variables = check_variables(steps, store.load_all_variables(),
                                library_written_vars(store.dir))
    problems.extend(variables)
    conn = _connect_roundtrip(steps)
    problems.extend(conn)
    lib = {x["name"]: x for x in store.load_functions()}
    code: List[str] = []
    for i, s in enumerate(steps):
        if s.action == "script":
            fc = free_code.analyze(s.script_code or "", s.script_lang or "python",
                                   list(store.load_all_variables()),
                                   _image_names(store))
            for e in fc.errors:
                code.append(f"第 {i} 步（自由代码）：{e}")
        elif s.action == "call":
            name = (s.func_name or "").strip()
            if name and name not in lib:
                code.append(f"第 {i} 步（调用函数）：函数库里没有「{name}」")
    problems.extend(code)
    return {"ok": not problems, "structure": structure,
            "variables": list(variables), "code": code,
            "problems": problems}


def _connect_roundtrip(steps: List[Step]) -> List[str]:
    """检查每一步的字段能不能真正用起来（定位缺失、网址为空等）。"""
    out = []
    for i, s in enumerate(steps):
        a = s.action
        if a == "navigate" and not (s.url or "").strip():
            out.append(f"第 {i} 步（打开网页）没填网址")
        if a in ("click", "fill", "select"):
            if not s.locator or not (s.locator.value or "").strip():
                out.append(f"第 {i} 步（{ACTION_SPECS[a]['label']}）没填定位")
            if a == "fill" and not (s.value or "").strip():
                out.append(f"第 {i} 步（填入）没填内容")
            if a == "select" and not (s.value or "").strip():
                out.append(f"第 {i} 步（下拉选择）没填选哪一项")
        if a == "collect":
            if not (s.output_var or "").strip():
                out.append(f"第 {i} 步（采集数据）没填产出变量名")
            if not (s.collect_fields or []):
                out.append(f"第 {i} 步（采集数据）没配字段")
            if (s.collect_mode or "page") == "list" and not (s.collect_row or "").strip():
                out.append(f"第 {i} 步（采集数据）是列表模式但没填「每行定位」")
        if a == "read_data":
            cfg = s.data_cfg or {}
            if not cfg.get("type") or not cfg.get("path"):
                out.append(f"第 {i} 步（读取数据）没配好 type / path")
        if a == "loop_start" and not (s.loop_expr or "").strip():
            out.append(f"第 {i} 步（循环）没填循环内容")
        if a == "condition_start" and not (s.cond_expr or "").strip():
            out.append(f"第 {i} 步（条件）没填判断内容")
        if a == "pause_for_human":
            if s.resume_condition in ("url_changed", "url_and_element") \
                    and not (s.resume_url or "").strip():
                out.append(f"第 {i} 步（暂停）选了 URL 条件但没填 URL")
            if s.resume_condition in ("element_present", "url_and_element") \
                    and not (s.resume_element or "").strip():
                out.append(f"第 {i} 步（暂停）选了元素条件但没填元素 XPath")
    return out


def _image_names(store: ProjectStore) -> List[str]:
    img = store.dir / "img"
    out: List[str] = []
    if img.is_dir():
        for p in sorted(img.iterdir()):
            if p.is_file():
                out.append(p.stem)
                out.append(p.name)
    return out


def run_project(project: str, headless: bool = True,
                on_log: Optional[Callable[[str], None]] = None,
                timeout_hint: int = 0) -> Dict[str, Any]:
    """跑一遍项目（同步执行）。

    headless=False 会显示浏览器窗口，方便调试。
    on_log＝每来一行日志就回调一次（想做进度条/流式输出就传它）。
    返回 {"ok": bool, "error": "", "logs": [...], "variables": {...}}。
    """
    store = _store(project)
    steps = store.load_steps()
    problem = blocks.validate(steps)
    if problem:
        raise ApiError(f"流程结构不合法，先修好再跑：{problem}")
    if not steps:
        raise ApiError("这个项目还没有步骤")
    logs: List[str] = []

    def _log(line: str):
        logs.append(line)
        if on_log is not None:
            try:
                on_log(line)
            except Exception:
                pass

    executor = StepExecutor(
        steps=steps,
        variables=store.load_all_variables(),
        headless=bool(headless),
        project_dir=store.dir,
        log=_log,
        scene=store.load_scene(),
        auth=store.load_auth(),
    )
    try:
        executor.run()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "logs": logs, "variables": dict(executor.variables)}
    return {"ok": True, "error": "", "logs": logs,
            "variables": dict(executor.variables)}


# ============================================================
# 六、采集到的数据 / 图片
# ============================================================
def list_records(project: str, limit: int = 20) -> Dict[str, Any]:
    """看采集到的数据（最多 limit 条，limit=0 表示全部）。"""
    store = _store(project)
    n = None if int(limit) == 0 else max(1, int(limit))
    rows = datastore.read_records(store.dir, limit=n)
    return {"total": len(datastore.read_records(store.dir, limit=None)),
            "columns": datastore.columns(rows), "rows": rows}


def export_records(project: str, fmt: str = "xlsx", path: str = "") -> Dict[str, Any]:
    """把采集到的数据导出成 Excel(.xlsx) 或 CSV。

    path 留空＝导出到项目目录 data/ 下（文件名带时间戳）。
    """
    from datetime import datetime

    store = _store(project)
    fmt = str(fmt or "xlsx").lower().lstrip(".")
    if fmt not in ("xlsx", "csv"):
        raise ApiError("fmt 只能填 xlsx 或 csv")
    target = Path(path) if path else \
        store.dir / "data" / f"导出_{datetime.now():%Y%m%d_%H%M%S}.{fmt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = datastore.export_xlsx(store.dir, target) if fmt == "xlsx" \
        else datastore.export_csv(store.dir, target)
    return {"path": str(target), "rows": rows, "fmt": fmt}


def list_images(project: str) -> List[Dict[str, Any]]:
    """列出图片库（项目 img/ 里的图片）。"""
    store = _store(project)
    img = store.dir / "img"
    out = []
    for p in sorted(img.iterdir()) if img.is_dir() else []:
        if p.is_file():
            out.append({"name": p.name, "stem": p.stem,
                        "path": str(p), "bytes": p.stat().st_size})
    return out


def import_image(project: str, source: str, name: str = "") -> Dict[str, Any]:
    """把外面的一张图片拷进项目图片库（name 留空＝用原文件名）。"""
    store = _store(project)
    src = Path(source)
    if not src.is_file():
        raise ApiError(f"找不到这个文件：{src}")
    img = store.dir / "img"
    img.mkdir(parents=True, exist_ok=True)
    target = img / (str(name).strip() or src.name)
    if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"):
        target = target.with_suffix(src.suffix or ".png")
    shutil.copyfile(src, target)
    return {"name": target.name, "path": str(target)}


# ============================================================
# 七、JSON 入口（给 LLM 的 function calling 用）
# ============================================================
TOOLS: Dict[str, Callable] = {}


def _register(*funcs: Callable):
    for fn in funcs:
        TOOLS[fn.__name__] = fn


_register(
    list_projects_info, create_project, delete_project, rename_project, get_project,
    set_scene,
    get_variables, set_variables, delete_variable,
    get_locators, set_locators, get_auth, set_auth, clear_auth,
    list_functions, set_function, delete_function,
    list_steps, add_steps, update_step, delete_steps, move_step,
    add_loop, add_condition, add_group, clear_steps, available_variables_of,
    validate_project, run_project,
    list_records, export_records, list_images, import_image,
    list_actions, describe_action, free_code_guide,
)

_PY_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean",
             list: "array", dict: "object"}


def _schema_for(fn: Callable) -> Dict[str, Any]:
    """按函数签名生成 JSON Schema（工具调用的参数说明）。"""
    sig = inspect.signature(fn)
    props, required = {}, []
    for name, param in sig.parameters.items():
        ann = param.annotation
        text = getattr(ann, "__name__", None)
        json_type = "string"
        optional = False
        if str(ann).startswith("Optional"):
            optional = True
            inner = str(ann)[8:].split("]")[0].strip()
            json_type = {"str": "string", "int": "integer", "float": "number",
                         "bool": "boolean", "list": "array",
                         "dict": "object"}.get(inner, "string")
        elif text in ("list", "List"):
            json_type = "array"
        elif text in ("dict", "Dict"):
            json_type = "object"
        else:
            json_type = _PY_TYPES.get(ann, "string")
        item = {"type": json_type}
        default = param.default
        if default is not inspect.Parameter.empty and default is not None:
            item["default"] = default
        props[name] = item
        if default is inspect.Parameter.empty and not optional:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def describe_tools() -> List[Dict[str, Any]]:
    """拿到工具清单（OpenAI / litellm 的 tools 格式，直接扔给模型就行）。"""
    out = []
    for name, fn in TOOLS.items():
        doc = (fn.__doc__ or "").strip()
        summary = doc.split("\n")[0] if doc else name
        out.append({
            "type": "function",
            "function": {"name": name, "description": doc or summary,
                         "parameters": _schema_for(fn)},
        })
    return out


def list_tools() -> List[Dict[str, str]]:
    """工具名 + 一句话说明（先扫一眼有哪些能力）。"""
    return [{"name": n, "desc": (fn.__doc__ or "").strip().split("\n")[0]}
            for n, fn in TOOLS.items()]


def call_tool(name: str, args: Union[Dict[str, Any], str, None] = None) -> Dict[str, Any]:
    """JSON 入口：按名字调工具，永远返回 {"ok": bool, ...}（不抛异常）。

    例：call_tool("add_steps", {"project": "演示", "steps": [{"action": "note",
        "text": "开始"}]})
    """
    fn = TOOLS.get(str(name or "").strip())
    if fn is None:
        return {"ok": False, "error": f"没有这个工具：{name}。"
                                     f"可用工具：{'、'.join(TOOLS)}"}
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError as e:
            return {"ok": False, "error": f"args 不是合法 JSON：{e}"}
    args = args or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "args 要是一个对象（字典）"}
    unknown = [k for k in args if k not in inspect.signature(fn).parameters]
    if unknown:
        return {"ok": False, "error": f"{name} 没有这些参数：{'、'.join(unknown)}。"
                                     f"参数是：{'、'.join(inspect.signature(fn).parameters)}"}
    try:
        return {"ok": True, "result": fn(**args)}
    except ApiError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ============================================================
# 八、命令行（给 agent / 人调试用）
# ============================================================
def _main(argv: List[str]) -> int:
    """python -m smart_tool.core.api tools|list|describe <action>|call <name> <json>"""
    cmd = (argv[1] if len(argv) > 1 else "tools").lower()
    if cmd == "tools":
        for item in list_tools():
            print(f"{item['name']:24s} {item['desc']}")
        return 0
    if cmd == "list":
        for a in list_actions(argv[2] if len(argv) > 2 else ""):
            print(f"{a['action']:18s} {a['label']:8s} {a['desc'][:60]}")
        return 0
    if cmd == "describe":
        if len(argv) < 3:
            print("用法：python -m smart_tool.core.api describe navigate")
            return 2
        print(json.dumps(describe_action(argv[2]), ensure_ascii=False, indent=2))
        return 0
    if cmd == "call":
        if len(argv) < 3:
            print('用法：python -m smart_tool.core.api call list_steps '
                  '\'{"project":"演示"}\'')
            return 2
        out = call_tool(argv[2], argv[3] if len(argv) > 3 else "{}")
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if out.get("ok") else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
