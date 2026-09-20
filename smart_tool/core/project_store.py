# -*- coding: utf-8 -*-
"""项目与步骤文件管理：读写 steps.json、管理 img 目录。

steps.json 结构：
{
  "steps": [ {id, action, url?, locator?, value?, wait_after?, wait_target?, prompt?, note?}, ... ],
  "variables": {"账号": "hcw", ...}      # 只有「自定义变量」在这里
}

变量从哪来：节点自己产出。「读取数据」（read_data）节点读文件/文件夹，
把结果放进 output_var 指定的变量（一个列表）；循环节点写 {{那个变量}} 遍历它，
循环体里用 {{loop.item.字段}} 取当前这一项的字段。

旧项目（steps.json 里有项目级 "data_source" 节）在 load() 时自动迁移成
一个「读取数据」节点，见 migrate_project()。
"""
import json
import re
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from smart_tool import paths

# 自动备份：每次真改内容写盘前，把现有的 steps.json 存成 bak1，最多留这么多份
BACKUP_KEEP = 3

# 项目场景：网页（Playwright，XPath / 元素捕获）或桌面（全屏截图定位 + 系统鼠标键盘）
SCENE_WEB, SCENE_DESKTOP = "web", "desktop"
SCENES = ((SCENE_WEB, "网页自动化（浏览器）"),
          (SCENE_DESKTOP, "桌面应用（截图定位 + 鼠标键盘）"))


def normalize_scene(value) -> str:
    """只认 desktop，其余都当网页场景。"""
    return SCENE_DESKTOP if value == SCENE_DESKTOP else SCENE_WEB


@dataclass
class Locator:
    """元素定位：XPath 或 截图。"""
    type: str = "xpath"        # "xpath" | "image"
    value: str = ""
    # type=xpath 时的兜底：XPath 等不到元素/点不动时，
    # 改用这张截图做模板匹配（OpenCV）再试一次。
    # 路径相对项目目录，如 img/cap_20260917_203512_1.png（捕获元素时自动生成）
    image: str = ""
    # 桌面场景：捕获时顺手裁下来的**整窗截图**（相对项目目录，如 img/xx_窗口.png）。
    # 运行时先用它认出窗口，再把「找控件」的范围缩到一个窗口里；
    # 空＝没有（老项目 / 拿不到窗口），行为跟以前一样：全屏匹配。
    window: str = ""
    # 红框相对**窗口左上角**的位置 [dx, dy, 宽, 高]（窗口图的像素）。
    # 窗口内没匹配到控件时，就按它的中心点（这就是「点红框中心」）。
    offset: List[float] = field(default_factory=list)


@dataclass
class Step:
    """单个步骤。"""
    id: int
    # navigate/click/fill/select/pause_for_human/loop_start/loop_end/script
    action: str
    title: str = ""                        # 自定义名称（画布/列表上显示；空＝用动作默认名）
    url: str = ""                          # navigate 用
    locator: Optional[Locator] = None      # click/fill/select 用
    value: str = ""                        # fill 用（可含 {{变量}}）
    wait_after: str = ""                   # element_present/url_changed/network_idle/manual
    wait_target: str = ""                 # 等待目标（XPath 或 URL 片段）
    wait_seconds: float = 0.0             # 执行完这个步骤后再固定等 N 秒（0=不等）
    nav_timeout: int = 120                # navigate 专用：打开网页最多等几秒（默认 120）
    prompt: str = ""                      # pause_for_human 的提示
    note: str = ""                        # 用户备注
    # ---- pause_for_human 专用：人工暂停后的恢复信号 ----
    # manual              仅人工点"继续"
    # url_changed         当前 URL 包含 resume_url（支持 * 通配）
    # element_present     resume_element(XPath) 出现在页面且可见
    # url_and_element     双重信号：URL 与元素同时满足（默认推荐，防误判）
    resume_condition: str = "manual"
    resume_url: str = ""
    resume_element: str = ""
    resume_timeout: int = 300             # 等待人工操作的最长秒数
    # 画布坐标 [x, y]（自由拖拽画布用；None 表示首次加载时自动排版）
    pos: Optional[List[float]] = None
    # ---- script 专用：自由代码节点（写一个真正的函数，系统自动调用它）----
    # script_code ：完整的函数定义。语法见 core/free_code.py：
    #               #文件=路径 写在签名里；@名字 读写变量清单；/图片名 读写图片库
    script_lang: str = "python"           # python | javascript
    script_code: str = ""
    script_timeout: int = 30              # 秒；到点中断（卡在等外部返回时拦不住）
    # ---- call 专用：调用「函数库」里的函数（项目管理→函数库，一处定义多处调用）----
    # func_name ：函数名
    # func_args ：实参，写法 `形参名=值`（值可写 {{变量}} 或字面量），逗号分隔
    func_name: str = ""
    func_args: str = ""
    # ---- read_data 专用：读文件 / 文件夹，产出一个「列表变量」 ----
    # data_cfg 的字段与 DataSourceConfig 一致（type/path/pattern/recursive/
    # encoding/sheet/has_header/field_map/vars_picked）
    output_var: str = ""                   # 产出变量名，如「文章列表」
    data_cfg: Dict[str, Any] = field(default_factory=dict)
    # ---- loop_start 专用：循环什么 ----
    # 【次数/列表】按 loop_expr 先算出一份固定清单，再逐项跑（for 语义）：
    #   纯数字 10        跑 10 次（loop.item = 索引 0~9）
    #   {{变量}}         按变量的「长度」跑：列表 / JSON 数组 / 多行文本按项数，
    #                   取值为整数则按该数；每一项注入 {{loop.item}}
    #   其他文本         按行/逗号切分成多项；只有一项就只跑一次
    loop_expr: str = ""
    # 【条件】每轮**先判断**再决定跑不跑（while / until 语义），判断不出次数
    loop_mode: str = "each"                # each＝次数/列表；cond＝条件
    # 判断什么：element＝网页上有这个元素；image＝屏幕上找到这张图；
    #          var＝变量满足条件（配 loop_cond_op / loop_cond_value）；
    #          expr＝自定义表达式
    loop_cond_kind: str = "element"
    loop_cond_arg: str = ""                # XPath / 图片名 / 变量名 / 表达式
    loop_cond_op: str = ""                 # var 用：判断方式，见 blocks.COND_OPS
    loop_cond_value: str = ""              # var 用：要比较的值
    loop_cond_stop: bool = False           # 成立时：False＝继续下一轮；True＝结束循环
    loop_interval: float = 1.0             # 每轮之间等几秒（0＝不等，监控类必填）
    loop_max: int = 10000                  # 最多跑几轮，0＝不限（死循环刹车）
    # ---- condition_start 专用：条件判断 ----
    # cond_mode: rule  条件节点只提供「判断的数据」（cond_expr 写 {{变量}}）；
    #                  块里**直属的动作节点**各自带一条规则（cond_op + cond_value），
    #                  从上往下比，第一个成立的执行；cond_op 为空＝兜底（else）。
    #            expr  Python 表达式（能当数字的变量按数字代入），
    #                  结果为 True/False 时走第 1/2 个动作节点，其他结果按值匹配
    cond_mode: str = "rule"
    cond_expr: str = ""
    # ---- 动作节点的规则：它是「条件」里的一个直属节点时才有意义 ----
    # cond_op 见 core/blocks.py 的 COND_OPS；空串＝兜底，必须放在最后一个
    cond_op: str = ""
    cond_value: str = ""
    # ---- 桌面场景专用（scene=desktop）----
    win_title: str = ""                  # win_activate：窗口标题里的一小段
    keys: str = ""                       # hotkey：要按的键，如 ctrl+s、enter
    click_times: int = 1                 # click：点几次（2＝双击）
    # 图片匹配的相似度阈值（0.5~0.99；0＝用默认 0.8）。
    # 浅色 / 低对比界面里匹配分数天然偏低，可以调低一点；调高则更严、宁可不点。
    # 对这一步里的所有图片匹配都生效（定位、等待图片出现/消失）。
    image_threshold: float = 0.0
    # ---- collect 专用：把页面上的东西采下来（存 data/ + 进变量）----
    # collect_mode: page＝当前页面采一条；list＝页面上多行，每行采一条
    # collect_row ：list 模式里「每一行」的 XPath
    # collect_fields：要采哪些字段，每项：
    #   {"name": 字段名, "kind": text/attr/html/link/image/file/shot,
    #    "locator": 定位（list 模式里是在「当前行」里找）,
    #    "extra": 取属性时＝属性名；截图时＝"整页" 或 "x,y,宽,高"（留空＝截元素）}
    collect_mode: str = "page"
    collect_row: str = ""
    collect_fields: List[Dict[str, str]] = field(default_factory=list)
    # ---- note 专用：画布上的提示 / 运行日志 ----
    # 这段文字直接画在画布卡片上（当说明用），运行时会把 {{变量}} 渲染出来
    # 打进日志——想看看某个变量到底取到了什么，插一个这种节点就行。
    text: str = ""
    # ---- group_start 专用：这个组合是「登录用」的 ----
    # 运行时如果用的是有效登录态（cookie 还没过期），整个组合直接跳过，
    # 不用再登一遍；登录态失效时会自动重跑整条流程，那时它照常执行。
    skip_if_logged_in: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "action": self.action}
        if self.title:
            d["title"] = self.title
        if self.url:
            d["url"] = self.url
        if self.locator:
            loc = asdict(self.locator)
            for k in ("image", "window", "offset"):
                if not loc.get(k):
                    loc.pop(k, None)        # 空字段别往文件里塞
            d["locator"] = loc
        if self.value:
            d["value"] = self.value
        if self.action == "navigate":
            d["nav_timeout"] = self.nav_timeout
        if self.wait_after:
            d["wait_after"] = self.wait_after
            if self.wait_target:
                d["wait_target"] = self.wait_target
        if self.wait_seconds:
            d["wait_seconds"] = self.wait_seconds
        if self.prompt:
            d["prompt"] = self.prompt
        if self.note:
            d["note"] = self.note
        if self.action == "pause_for_human":
            d["resume_condition"] = self.resume_condition
            if self.resume_url:
                d["resume_url"] = self.resume_url
            if self.resume_element:
                d["resume_element"] = self.resume_element
            d["resume_timeout"] = self.resume_timeout
        if self.action == "script":
            d["script_lang"] = self.script_lang
            d["script_code"] = self.script_code
            d["script_timeout"] = self.script_timeout
        if self.action == "call":
            d["func_name"] = self.func_name
            d["script_timeout"] = self.script_timeout
            if self.func_args:
                d["func_args"] = self.func_args
        if self.action == "read_data":
            d["output_var"] = self.output_var
            d["data_cfg"] = dict(self.data_cfg or {})
        if self.action == "collect":
            d["output_var"] = self.output_var
            d["collect_mode"] = self.collect_mode or "page"
            if self.collect_row:
                d["collect_row"] = self.collect_row
            if self.collect_fields:
                d["collect_fields"] = [dict(f) for f in self.collect_fields
                                       if isinstance(f, dict)]
        if self.action == "loop_start":
            d["loop_mode"] = self.loop_mode or "each"
            if self.loop_mode == "cond":
                d["loop_cond_kind"] = self.loop_cond_kind or "element"
                d["loop_cond_arg"] = self.loop_cond_arg
                d["loop_cond_op"] = self.loop_cond_op
                d["loop_cond_value"] = self.loop_cond_value
                d["loop_cond_stop"] = bool(self.loop_cond_stop)
                d["loop_interval"] = float(self.loop_interval or 0)
                d["loop_max"] = int(self.loop_max or 0)
            else:
                d["loop_expr"] = self.loop_expr
        if self.action == "condition_start":
            d["cond_mode"] = self.cond_mode
            if self.cond_expr:
                d["cond_expr"] = self.cond_expr
        if self.cond_op or self.cond_value:
            # 这个动作节点摆在「条件」里，自带一条规则（判断方式 + 值）
            d["cond_op"] = self.cond_op
            d["cond_value"] = self.cond_value
        if self.action == "win_activate" and self.win_title:
            d["win_title"] = self.win_title
        if self.action == "hotkey" and self.keys:
            d["keys"] = self.keys
        if self.action == "click" and int(self.click_times or 1) != 1:
            d["click_times"] = int(self.click_times)
        if self.image_threshold:
            d["image_threshold"] = float(self.image_threshold)
        if self.pos is not None:
            d["pos"] = [float(self.pos[0]), float(self.pos[1])]
        if self.skip_if_logged_in:
            d["skip_if_logged_in"] = True
        if self.text:
            d["text"] = self.text
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        loc = d.get("locator")
        locator = Locator(**loc) if loc else None
        pos = d.get("pos")
        if pos is not None and len(pos) >= 2:
            pos = [float(pos[0]), float(pos[1])]
        else:
            pos = None
        return cls(
            id=d["id"],
            action=d["action"],
            title=d.get("title", ""),
            url=d.get("url", ""),
            locator=locator,
            value=d.get("value", ""),
            wait_after=d.get("wait_after", ""),
            wait_target=d.get("wait_target", ""),
            wait_seconds=float(d.get("wait_seconds", 0) or 0),
            nav_timeout=int(d.get("nav_timeout", 120) or 120),
            prompt=d.get("prompt", ""),
            note=d.get("note", ""),
            resume_condition=d.get("resume_condition", "manual"),
            resume_url=d.get("resume_url", ""),
            resume_element=d.get("resume_element", ""),
            resume_timeout=int(d.get("resume_timeout", 300)),
            pos=pos,
            script_lang=d.get("script_lang", "python"),
            script_code=d.get("script_code", ""),
            script_timeout=int(d.get("script_timeout", 30)),
            func_name=d.get("func_name", ""),
            func_args=d.get("func_args", ""),
            output_var=d.get("output_var", ""),
            data_cfg=dict(d.get("data_cfg") or {}),
            collect_mode=d.get("collect_mode", "page") or "page",
            collect_row=d.get("collect_row", ""),
            collect_fields=[dict(f) for f in d.get("collect_fields", [])
                            if isinstance(f, dict)],
            loop_expr=d.get("loop_expr", ""),
            loop_mode=d.get("loop_mode") or "each",
            loop_cond_kind=d.get("loop_cond_kind") or "element",
            loop_cond_arg=d.get("loop_cond_arg", ""),
            loop_cond_op=d.get("loop_cond_op", ""),
            loop_cond_value=d.get("loop_cond_value", ""),
            loop_cond_stop=bool(d.get("loop_cond_stop")),
            loop_interval=float(d.get("loop_interval", 1.0) or 0),
            loop_max=int(d.get("loop_max", 10000) or 0),
            cond_mode=d.get("cond_mode") or "rule",
            cond_expr=d.get("cond_expr", ""),
            cond_op=d.get("cond_op", ""),
            cond_value=d.get("cond_value", ""),
            win_title=d.get("win_title", ""),
            keys=d.get("keys", ""),
            click_times=int(d.get("click_times", 1) or 1),
            image_threshold=float(d.get("image_threshold", 0) or 0),
            skip_if_logged_in=bool(d.get("skip_if_logged_in")),
            text=str(d.get("text") or ""),
        )


class ProjectStore:
    """单个自动化项目的存储管理。"""

    def __init__(self, project_dir: Path):
        self.dir = project_dir
        self.steps_file = project_dir / "steps.json"
        self.img_dir = project_dir / "img"

    @property
    def name(self) -> str:
        return self.dir.name

    def ensure(self):
        """确保项目目录与 img 目录存在。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.img_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """读取整个 steps.json（旧项目的「数据源」在这里自动迁移成读取节点）。"""
        if not self.steps_file.exists():
            return {"steps": [], "variables": {}, "layout": ""}
        return migrate_project(
            json.loads(self.steps_file.read_text(encoding="utf-8"))
        )

    def load_steps(self) -> List[Step]:
        return [Step.from_dict(s) for s in self.load().get("steps", [])]

    def load_variables(self) -> Dict[str, str]:
        return self.load().get("variables", {})

    def load_locators(self) -> Dict[str, str]:
        """「元素定位」变量：名字 → XPath（捕获元素时存下来的）。

        它和自定义变量存的是同一个「名字 → 值」的东西，只是单独一组：
        变量清单里分开展示，运行时一起注入，所以步骤里能直接写 {{登录框}}。
        """
        raw = self.load().get("locators", {}) or {}
        return {str(k).strip(): str(v)
                for k, v in raw.items() if str(k).strip()}

    def load_all_variables(self) -> Dict[str, str]:
        """运行时 / 变量下拉真正能用的全部变量＝自定义变量 + 元素定位。

        两者重名时以「元素定位」为准（变量清单里会提示重名）。
        """
        merged = dict(self.load_variables())
        merged.update(self.load_locators())
        return merged

    def load_functions(self) -> List[Dict[str, Any]]:
        """函数库：一处定义、多处调用（「调用函数」节点用）。

        每一条：{"name": 函数名, "lang": python|javascript, "params": 形参（逗号分隔）,
                 "code": 代码, "desc": 说明}
        """
        out: List[Dict[str, Any]] = []
        for f in self.load().get("functions", []) or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "lang": str(f.get("lang") or "python"),
                "params": str(f.get("params") or ""),
                "code": str(f.get("code") or ""),
                "desc": str(f.get("desc") or ""),
            })
        return out

    def save_functions(self, functions: List[Dict[str, Any]]):
        """只更新函数库，其余配置保持不变。"""
        data = dict(self.load())
        data["functions"] = [
            {
                "name": str(f.get("name") or "").strip(),
                "lang": str(f.get("lang") or "python"),
                "params": str(f.get("params") or ""),
                "code": str(f.get("code") or ""),
                "desc": str(f.get("desc") or ""),
            }
            for f in (functions or [])
            if str(f.get("name") or "").strip()
        ]
        self._write(data)

    def load_layout_version(self) -> str:
        """画布排版版本；与当前版本不一致时自动重排为横向布局。"""
        return self.load().get("layout", "") or ""

    def save(self, steps: List[Step], variables: Optional[Dict[str, str]] = None,
             layout_version: Optional[str] = None,
             scene: Optional[str] = None):
        """保存步骤与变量。variables/layout/scene 为 None 时保留原值。"""
        old = self.load()
        data: Dict[str, Any] = {"steps": [s.to_dict() for s in steps]}
        data["variables"] = (
            variables if variables is not None else old.get("variables", {})
        )
        # 画布上手动连的箭头：纯展示，执行器不读，但改动步骤时必须原样保留
        data["canvas_edges"] = old.get("canvas_edges", [])
        # 画布缩放（Ctrl+滚轮调的）：同理，保存步骤时别弄丢
        if old.get("zoom"):
            data["zoom"] = old["zoom"]
        data["layout"] = (
            layout_version if layout_version is not None
            else old.get("layout", "")
        )
        if old.get("real_mouse"):
            data["real_mouse"] = True     # 项目级开关，别被保存步骤时弄丢
        if old.get("auth"):
            data["auth"] = dict(old["auth"])   # 登录态配置同理
        if old.get("locators"):
            data["locators"] = dict(old["locators"])   # 元素定位同理
        if old.get("functions"):
            data["functions"] = list(old["functions"])   # 函数库同理（别被保存步骤弄丢）
        scene_val = normalize_scene(
            scene if scene is not None else old.get("scene"))
        if scene_val == SCENE_DESKTOP:
            data["scene"] = SCENE_DESKTOP
        self._write(data)

    def save_variables(self, variables: Dict[str, str]):
        """只更新变量，步骤保持不变。"""
        self.save(self.load_steps(), variables)

    def save_locators(self, locators: Dict[str, str]):
        """只更新「元素定位」，其余配置保持不变。"""
        data = dict(self.load())
        data["locators"] = {str(k).strip(): str(v)
                            for k, v in (locators or {}).items()
                            if str(k).strip()}
        self._write(data)

    def load_canvas_edges(self) -> List[list]:
        """画布上手动连的箭头（纯展示，不参与执行）。

        每一条是两端：步骤 id，或 "frame:<起始步骤id>"（循环 / 条件框）。
        """
        out: List[list] = []
        for e in self.load().get("canvas_edges", []) or []:
            if isinstance(e, (list, tuple)) and len(e) == 2:
                out.append([e[0], e[1]])
        return out

    def save_canvas_edges(self, edges: List[list]):
        """只更新画布手动连线，其余配置保持不变。"""
        data = dict(self.load())
        data["canvas_edges"] = [[a, b] for a, b in edges]
        self._write(data)

    def load_canvas_zoom(self) -> float:
        """画布缩放（Ctrl+滚轮调的那个）。没存过 / 不合法 → 0（＝按默认 100% 打开）。"""
        try:
            zoom = float(self.load().get("zoom") or 0)
        except (TypeError, ValueError):
            return 0.0
        # 和画布自己的上下限对齐（0.2~5 倍），超出就当成没存过
        return zoom if 0.2 <= zoom <= 5.0 else 0.0

    def save_canvas_zoom(self, zoom: float):
        """只更新画布缩放，其余配置保持不变。"""
        data = dict(self.load())
        data["zoom"] = round(float(zoom), 3)
        self._write(data)

    def load_real_mouse(self) -> bool:
        """这个项目要不要用「真实鼠标」（OS 级点击，默认关）。"""
        return bool(self.load().get("real_mouse"))

    def save_real_mouse(self, on: bool):
        """只更新「真实鼠标」开关，其余配置保持不变。"""
        data = dict(self.load())
        data["real_mouse"] = bool(on)
        self._write(data)

    def load_scene(self) -> str:
        """这个项目的场景：web（浏览器）/ desktop（桌面应用）。"""
        return normalize_scene(self.load().get("scene"))

    def load_auth(self) -> Dict[str, str]:
        """项目级登录态配置。

        - name：运行时用哪个登录态（`auth/<name>.json`）；空＝不用登录态
        - check_locator：**登录后才会出现的元素**（XPath）。用它做「体检」：
          带上登录态打开第一个网页后，这个元素在＝还有效；不在＝失效，
          执行器会自动清掉它、把整条流程重跑一遍（走完整登录步骤）。
        """
        raw = self.load().get("auth") or {}
        return {
            "name": str(raw.get("name") or ""),
            "check_locator": str(raw.get("check_locator") or ""),
        }

    def save_auth(self, name: str, check_locator: str = ""):
        """只更新登录态配置，其余保持不变。"""
        data = dict(self.load())
        name = str(name or "").strip()
        data["auth"] = {
            "name": name,
            "check_locator": str(check_locator or "").strip(),
        }
        self._write(data)
        if not name:
            return
        # 顺手把 auth 目录建出来：用户在【项目管理…】里点【登录态】也有地方放
        (self.dir / "auth").mkdir(parents=True, exist_ok=True)

    @property
    def is_desktop(self) -> bool:
        return self.load_scene() == SCENE_DESKTOP

    # ------------------------------
    # 写盘 + 自动备份
    # ------------------------------
    def _write(self, data: Dict[str, Any]):
        """写 steps.json；写之前先备份一份现有的（位置类改动不占备份额度）。"""
        self.ensure()
        self._backup_steps(data)
        self.steps_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _backup_steps(self, new_data: Dict[str, Any]):
        """把现有的 steps.json 备份成 steps.json.bak1，旧的依次往后挪。

        最多留 BACKUP_KEEP 份：bak1 最新、bak3 最旧。
        只挪了画布位置 / 手动连线这种「没动内容」的写盘会跳过，
        免得随手拖两下就把早先的好版本挤出备份队列。
        """
        if not self.steps_file.exists():
            return
        try:
            old = json.loads(self.steps_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if _content_key(old) == _content_key(new_data):
            return

        baks = [self.steps_file.with_name(f"{self.steps_file.name}.bak{i}")
                for i in range(1, BACKUP_KEEP + 1)]
        # 先往后挪：bak2→bak3、bak1→bak2，再把当前文件存成 bak1
        for older, newer in zip(reversed(baks[1:]), reversed(baks[:-1])):
            if newer.exists():
                try:
                    shutil.copy2(newer, older)
                except OSError:
                    pass
        try:
            shutil.copy2(self.steps_file, baks[0])
        except OSError:
            pass

    def image_count(self) -> int:
        """img 目录内图片文件数量（删除前提示用）。"""
        if not self.img_dir.exists():
            return 0
        exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
        return sum(1 for f in self.img_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in exts)

    def delete(self):
        """删除整个项目目录（含 steps.json 与 img 截图、备份）。"""
        shutil.rmtree(self.dir, ignore_errors=True)


def _content_key(data: Dict[str, Any]) -> str:
    """「实质内容」指纹：忽略画布坐标与手动连线，用来判断这次写盘算不算真改动。"""
    steps = []
    for s in data.get("steps") or []:
        if isinstance(s, dict):
            s = {k: v for k, v in s.items() if k != "pos"}
        steps.append(s)
    return json.dumps(
        {"steps": steps, "variables": data.get("variables") or {}},
        ensure_ascii=False, sort_keys=True,
    )


# ------------------------------
# 「读取数据」节点改名 → 别处的引用一起改
# ------------------------------
def _field_map_of(step: Step) -> Dict[str, str]:
    """读取节点的字段清单：{原始字段: 变量名}。"""
    out: Dict[str, str] = {}
    for m in (step.data_cfg or {}).get("field_map") or []:
        if not isinstance(m, dict):
            continue
        field = (m.get("field") or "").strip()
        var = (m.get("var") or "").strip()
        if field and var:
            out[field] = var
    return out


def _rename_in(obj: Any, mapping: Dict[str, str], hits: Dict[str, int]):
    """递归把文本里的 {{旧名}} 换成 {{新名}}，并记下改了几处。"""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                obj[k] = _REF_RE.sub(lambda m: _renamed(m, mapping, hits), v)
            else:
                _rename_in(v, mapping, hits)
    elif isinstance(obj, list):
        for v in obj:
            _rename_in(v, mapping, hits)


def _renamed(match: "re.Match", mapping: Dict[str, str],
             hits: Dict[str, int]) -> str:
    name = match.group(1).strip()
    new = mapping.get(name)
    if new is None:
        return match.group(0)
    hits[name] = hits.get(name, 0) + 1
    return "{{" + new + "}}"


def rename_field_refs(steps: List[Step], old_node: Step, new_node: Step,
                      skip: int = -1) -> List[str]:
    """读取节点改名后，把其它步骤里对它的引用一起改掉。

    - 产出变量改名：{{旧变量}} → {{新变量}}（循环节点里那句也会跟着变）
    - 字段改名：{{loop.item.旧字段}} → {{loop.item.新字段}}

    只改精确的 {{...}} 占位符；返回改动说明（没有改动返回空列表）。
    """
    pairs: List[tuple] = []
    old_var = (old_node.output_var or "").strip()
    new_var = (new_node.output_var or "").strip()
    if old_var and new_var and old_var != new_var:
        pairs.append((old_var, new_var))
    new_fields = _field_map_of(new_node)
    for field, var in _field_map_of(old_node).items():
        new = new_fields.get(field)
        if new and new != var:
            pairs.append((f"loop.item.{var}", f"loop.item.{new}"))
    if not pairs:
        return []
    mapping = dict(pairs)
    hits: Dict[str, int] = {}
    for i, s in enumerate(steps):
        if i == skip:
            continue
        # 直接改步骤对象自己的字段（url / value / cond_value / data_cfg.path …）
        _rename_in(s.__dict__, mapping, hits)
    return [f"{{{{{old}}}}} → {{{{{new}}}}}（{hits.get(old, 0)} 处）"
            for old, new in pairs if hits.get(old, 0)]


# ------------------------------
# 旧项目迁移：项目级「数据源」→ 一个「读取数据」节点
# ------------------------------
# 文件类数据源没挑过变量时，用这些短名字当变量名（不再出现 file.content 写法）
FILE_VAR_SHORT = {
    "content": "内容", "name": "文件名", "stem": "文件主名",
    "parent_name": "父文件夹名", "path": "路径", "suffix": "扩展名",
    "size": "大小", "folder_count": "同文件夹文件数", "total": "文件总数",
}
DEFAULT_LIST_VAR = "数据列表"
# {{变量}} 占位符
_REF_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _strip_var_prefix(name: str) -> str:
    """去掉旧写法的 row. / file. 前缀。"""
    for p in ("row.", "file."):
        if name.startswith(p):
            return name[len(p):]
    return name


def _resolve_field(cfg_type: str, fld: str) -> str:
    """把字段短名还原成数据行里的原始键（file.xxx / row.xxx）。"""
    f = (fld or "").strip()
    if not f:
        return ""
    if f.startswith(("row.", "file.")):
        return f
    return ("file." if cfg_type in ("txt", "folder") else "row.") + f


def _materialize_cfg(ds: Dict[str, Any]):
    """把旧数据源配置整理成读取节点的 data_cfg。

    返回 (cfg, 旧引用名→字段名 的映射, 是否已经定下了字段清单)。
    旧项目里步骤可能写 {{标题}}（改名后）也可能写 {{file.content}}（原始名），
    两种写法都要能翻译成新写法 {{loop.item.字段}}。
    """
    cfg = dict(ds)
    cfg_type = cfg.get("type", "")
    picked = bool(cfg.get("field_map")) or "field_map" in cfg
    mapping: Dict[str, str] = {}
    items: List[Dict[str, str]] = []
    if picked:
        for m in cfg.get("field_map") or []:
            if not isinstance(m, dict):
                continue
            field = _resolve_field(cfg_type, m.get("field", ""))
            if not field:
                continue
            var = _strip_var_prefix((m.get("var") or "").strip()) \
                or _strip_var_prefix(field)
            var = var or field
            items.append({"field": field, "var": var})
            mapping[field] = var
            mapping[m.get("field", "")] = var
            mapping[(m.get("var") or "").strip()] = var
        cfg["field_map"] = items
        cfg["vars_picked"] = True
        return cfg, mapping, True

    # 没挑过变量：旧行为是「产出全部原始变量」，这里换成一套看得懂的名字
    if cfg_type in ("txt", "folder"):
        from smart_tool.core.data_sources import FILE_FIELDS
        for key, _label in FILE_FIELDS:
            field = f"file.{key}"
            var = FILE_VAR_SHORT.get(key, key)
            items.append({"field": field, "var": var})
            mapping[field] = var
        cfg["field_map"] = items
        cfg["vars_picked"] = True
        return cfg, mapping, True

    # Excel / JSON 得先读表头才知道列名；读不出来就保持原样（运行时按原始键取）
    try:
        from smart_tool.core.data_sources import (
            DataSourceConfig, raw_columns,
        )
        cols = raw_columns(DataSourceConfig.from_dict(cfg))
    except Exception:
        cols = []
    if cols:
        for field in cols:
            var = _strip_var_prefix(field)
            items.append({"field": field, "var": var})
            mapping[field] = var
        cfg["field_map"] = items
        cfg["vars_picked"] = True
        return cfg, mapping, True
    cfg["field_map"] = []
    cfg["vars_picked"] = False
    return cfg, mapping, False


def _rewrite_refs(text: str, mapping: Dict[str, str], named: bool) -> str:
    """把循环体里的字段引用改成 {{loop.item.字段}}。"""
    def sub(m):
        name = m.group(1).strip()
        if name in mapping:
            return "{{loop.item." + mapping[name] + "}}"
        if not named and name.startswith(("row.", "file.")):
            # 没定下字段清单（Excel/JSON 读不出表头）：保持原始键
            return "{{loop.item." + name + "}}"
        return m.group(0)
    return _REF_RE.sub(sub, text)


def _rewrite_all_refs(obj: Any, mapping: Dict[str, str], named: bool):
    """递归改写一步里所有文本字段中的引用。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                obj[k] = _rewrite_refs(v, mapping, named)
            else:
                _rewrite_all_refs(v, mapping, named)
    elif isinstance(obj, list):
        for v in obj:
            _rewrite_all_refs(v, mapping, named)


def _loop_body_ranges(steps: List[Dict[str, Any]],
                      starts: List[int]) -> List[tuple]:
    """这些「循环开始」各自对应的循环体范围（左闭右开，不含两端的标记）。"""
    out = []
    for a in starts:
        depth = 0
        for b in range(a + 1, len(steps)):
            act = steps[b].get("action")
            if act == "loop_start":
                depth += 1
            elif act == "loop_end":
                if depth == 0:
                    out.append((a + 1, b))
                    break
                depth -= 1
    return out


def _remap_endpoint(value: Any, id_map: Dict[Any, int]) -> Any:
    """画布连线端点：步骤 id，或 "frame:<起始步骤id>"。"""
    if isinstance(value, str) and value.startswith("frame:"):
        try:
            n = int(value[6:])
        except ValueError:
            return value
        return f"frame:{id_map.get(n, n)}"
    return id_map.get(value, value)


#: 容器开始标记 → 结束标记（迁移时用来跳过整块）
_BLOCK_STARTS = {"loop_start": "loop_end", "condition_start": "condition_end",
                 "group_start": "group_end"}
_BLOCK_ENDS = set(_BLOCK_STARTS.values())


def _migrate_conditions(steps: List[Dict[str, Any]]) -> None:
    """条件节点升级成「条件提供数据 + 动作节点自带规则」（每次 load 都会跑）。

    旧写法：条件体里用 `branch` 标记分段，每段的规则存在条件的 `cond_branches` 里。

        条件（判断内容 {{标题}}）
          分支
            点击 公示公告        ← 配置记在条件上：[{名: 公示公告, 包含, 公示}]
          分支
            点击 申报通知
          条件结束

    新写法：没有分支标记了，条件体里的**直属动作节点**各自带一条规则
    （`cond_op` / `cond_value`），从上往下第一个成立的就执行那一个：

        条件（判断的数据 {{标题}}）
          点击 公示公告          ← cond_op=contains cond_value=公示
          点击 申报通知          ← cond_op=contains cond_value=申报
          条件结束

    一段里不止一个步骤的（示例项目里就有），自动收成一个「组合」、规则挂在组合上，
    行为跟原来完全一致；空的段补一个记号节点，免得把规则弄丢。
    旧的「变量相等」判断方式同时升级成「规则 + 等于」（匹配值本来就是
    「命中任意一个就行」，跟逗号多值语义一致）。
    """
    next_id = max([int(s.get("id") or 0) for s in steps] or [0]) + 1

    def new_id() -> int:
        nonlocal next_id
        next_id += 1
        return next_id - 1

    def block_end(nodes: List[Dict[str, Any]], start: int) -> int:
        """start 是容器开始标记 → 配对结束标记的下标（找不到就给最后一行）。"""
        depth = 0
        for k in range(start, len(nodes)):
            a = nodes[k].get("action")
            if a in _BLOCK_STARTS:
                depth += 1
            elif a in _BLOCK_ENDS:
                depth -= 1
                if depth == 0:
                    return k
        return len(nodes) - 1

    def chunks(nodes: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any],
                                                          Optional[List[Dict[str, Any]]],
                                                          Optional[Dict[str, Any]],
                                                          str]]:
        """把一层里的节点切成「块」：(开始标记, 内部, 结束标记, 动作)。

        普通步骤的内部与结束标记是 None；容器（循环 / 条件 / 组合）整块算一个。
        只认本层的标记 —— 嵌套在里面的块由调用方递归处理，不会跟本层的
        「分支」标记混在一起。
        """
        out = []
        i = 0
        while i < len(nodes):
            s = nodes[i]
            a = s.get("action")
            if a in _BLOCK_STARTS:
                end = block_end(nodes, i)
                out.append((s, nodes[i + 1:end], nodes[end], a))
                i = end + 1
            else:
                out.append((s, None, None, a))
                i += 1
        return out

    def placeholder(op: str, value: str, index: int) -> Dict[str, Any]:
        """旧写法里某一段一个步骤都没有 → 放个记号节点，把规则留着。"""
        return {"id": new_id(), "action": "note", "text": "",
                "title": f"（原来第 {index + 1} 个分支下面没有动作节点）",
                "cond_op": op, "cond_value": value}

    def wrap(seg: List[Dict[str, Any]], op: str, value: str,
             index: int, name: str = "") -> List[Dict[str, Any]]:
        """一段里不止一个步骤 → 收成一个组合，规则挂在组合开始上。

        组合沿用旧分支的名字（如「已登录」），省得用户还想知道原来叫啥。
        """
        start: Dict[str, Any] = {
            "id": new_id(), "action": "group_start",
            "title": name or f"原来第 {index + 1} 个分支的步骤",
            "cond_op": op, "cond_value": value,
        }
        if seg[0].get("pos"):
            start["pos"] = list(seg[0]["pos"])
        end: Dict[str, Any] = {"id": new_id(), "action": "group_end"}
        if seg[-1].get("pos"):
            end["pos"] = list(seg[-1]["pos"])
        return [start] + seg + [end]

    def rebuild(cond: Dict[str, Any],
                inner: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把「分支标记分段」的老条件体，改成「动作节点自带规则」。

        已经是新写法（没有分支标记、条件上也没有 cond_branches）的直接跳过 ——
        每次 load 都会跑一遍迁移，不能把好好的结构再包一次组合。
        """
        if "cond_branches" not in cond and not any(
                s.get("action") == "branch" for s in inner):
            return walk(inner)

        rules = [dict(m) for m in (cond.pop("cond_branches", None) or [])
                 if isinstance(m, dict)]
        was_equal = (cond.get("cond_mode") or "equal") == "equal"
        if was_equal:
            cond["cond_mode"] = "rule"

        # 按本层的「分支」标记切段（嵌套在里面的块整体算一个节点）
        segments: List[List[Any]] = [[]]
        for chunk in chunks(inner):
            if chunk[3] == "branch":
                segments.append([])
            else:
                segments[-1].append(chunk)
        if len(segments) > 1 and not segments[0]:
            segments.pop(0)          # 第一个分支标记之前一定是空的，去掉这个空壳

        out: List[Dict[str, Any]] = []
        for k, seg in enumerate(segments):
            rule = rules[k] if k < len(rules) else {}
            op = str(rule.get("op") or "").strip()
            value = rule.get("value")
            if value is None:
                value = rule.get("values") or ""
            value = str(value)
            if not op and was_equal:
                op = "eq"
            nodes: List[Dict[str, Any]] = []
            for s, body, end, a in seg:
                nodes.append(s)
                if body is not None:
                    nodes.extend(rebuild(s, body) if a == "condition_start"
                                 else walk(body))
                    nodes.append(end)
            if not nodes:
                out.append(placeholder(op, value, k))
            elif len(seg) == 1:
                nodes[0]["cond_op"] = op
                nodes[0]["cond_value"] = value
                out.extend(nodes)
            else:
                out.extend(wrap(nodes, op, value, k,
                                str(rule.get("name") or "")))
        return out

    def walk(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """升级一个层级的节点列表（递归进循环 / 条件 / 组合）。"""
        out: List[Dict[str, Any]] = []
        for s, body, end, a in chunks(nodes):
            out.append(s)
            if body is not None:
                out.extend(rebuild(s, body) if a == "condition_start"
                           else walk(body))
                out.append(end)
        return out

    steps[:] = walk(steps)


def _alive_edges(edges: Any, steps: List[Dict[str, Any]]) -> List[list]:
    """只留两端都还指得到步骤的「手动连线」。

    迁移会删掉「分支」标记、还会重排步骤号，而 canvas_edges 是按步骤 id 存的：
    不清掉就会留下死链（界面上加载时会自己丢掉），重排编号之后更麻烦——旧 id
    可能正好被别的步骤占用，箭头就悄悄连到不相干的地方去了。
    端点写法：步骤 id，或 "frame:<起始步骤id>"（循环 / 条件那个虚线框）。
    """
    alive = {s.get("id") for s in steps}

    def ok(value: Any) -> bool:
        if isinstance(value, str) and value.startswith("frame:"):
            value = value[6:]
        try:
            return int(value) in alive
        except (TypeError, ValueError):
            return False

    return [list(e) for e in (edges or [])
            if isinstance(e, (list, tuple)) and len(e) == 2
            and ok(e[0]) and ok(e[1])]


def migrate_project(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """旧项目自动升级（每次 load 都会跑，结果直到下次 save 才落盘）。

    旧版把「读哪个文件夹、要哪些字段」放在项目级的 data_source 里，循环节点
    只写一句"用数据源"。现在改成影刀那样的节点式：

        [5] 读取数据  输出「数据列表」 →  [6] 循环 {{数据列表}}

    循环体里原来的 {{标题}} / {{file.content}} / {{row.列名}} 一并改成
    {{loop.item.标题}}。步骤号重排成 1..N，画布手动连线跟着换号。
    """
    data = dict(raw or {})
    ds = data.pop("data_source", None) or {}
    steps = [dict(s) for s in (data.get("steps") or []) if isinstance(s, dict)]
    data["steps"] = steps
    _migrate_conditions(steps)                   # 条件节点的新旧写法升级
    # 迁移删掉的步骤（分支标记）会留下指不到的手动连线，先清掉再往下走
    data["canvas_edges"] = _alive_edges(data.get("canvas_edges"), steps)
    if not (ds.get("type") and ds.get("path")):
        return data

    loops = [i for i, s in enumerate(steps)
             if s.get("action") == "loop_start"
             and (s.get("loop_source") or "data") == "data"]
    if not loops:
        return data

    cfg, mapping, named = _materialize_cfg(ds)
    ranges = _loop_body_ranges(steps, loops)
    for lo, hi in ranges:
        for s in steps[lo:hi]:
            _rewrite_all_refs(s, mapping, named)

    taken = set(data.get("variables") or {})
    out_var = DEFAULT_LIST_VAR
    i = 2
    while out_var in taken:
        out_var = f"{DEFAULT_LIST_VAR}{i}"
        i += 1

    for pos in loops:
        s = steps[pos]
        s["loop_expr"] = "{{" + out_var + "}}"
        s.pop("loop_source", None)
        s.pop("loop_items", None)
        s.pop("loop_range", None)

    read_step: Dict[str, Any] = {
        "id": 0, "action": "read_data",
        "output_var": out_var, "data_cfg": cfg,
    }
    steps.insert(loops[0], read_step)

    # 顺序号重排成 1..N（画布手动连线跟着换号）
    id_map: Dict[Any, int] = {}
    for n, s in enumerate(steps, start=1):
        id_map[s.get("id")] = n
        s["id"] = n
    if data.get("canvas_edges"):
        data["canvas_edges"] = _alive_edges([
            [_remap_endpoint(e[0], id_map), _remap_endpoint(e[1], id_map)]
            for e in data["canvas_edges"]
            if isinstance(e, (list, tuple)) and len(e) == 2
        ], steps)
    return data


# Windows 文件夹名非法字符
_INVALID_NAME_CHARS = set('\\/:*?"<>|')


def validate_project_name(name: str) -> Optional[str]:
    """返回错误信息；合法返回 None。"""
    name = name.strip()
    if not name:
        return "项目名称不能为空"
    if any(c in _INVALID_NAME_CHARS for c in name):
        return '项目名称不能包含 \\ / : * ? " < > | 等字符'
    if name in (".", ".."):
        return "项目名称不合法"
    if (paths.PROJECTS_DIR / name).exists():
        return f"项目「{name}」已存在"
    return None


def install_demo_project(
        projects_dir: Optional[Path] = None) -> Optional[Path]:
    """把内置的示例项目复制到 projects/ 下（已经有了就不动）。

    首次运行准备环境时用。返回新复制出来的项目目录；模板不在、或者同名项目
    已经有了，就返回 None（当作「不用管」）。
    """
    root = Path(projects_dir) if projects_dir is not None else paths.PROJECTS_DIR
    src = paths.demo_template_dir()
    if not src.is_dir():
        return None
    dst = root / src.name
    if dst.exists():
        return None
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def list_projects() -> List[ProjectStore]:
    """列出所有项目（含 steps.json 的目录）。"""
    paths.ensure_dirs()
    result = []
    for p in sorted(paths.PROJECTS_DIR.iterdir()):
        if p.is_dir() and (p / "steps.json").exists():
            result.append(ProjectStore(p))
    return result


def create_project(name: str, initial_url: str = "",
                   scene: str = SCENE_WEB) -> ProjectStore:
    """新建项目。

    :param initial_url: 网页场景下非空时自动生成第 1 步「打开网页」。
    :param scene: 场景；桌面场景不带网址，改为先放一个「激活窗口」占位。
    """
    paths.ensure_dirs()
    err = validate_project_name(name)
    if err:
        raise ValueError(err)
    scene = normalize_scene(scene)
    store = ProjectStore(paths.PROJECTS_DIR / name.strip())
    store.ensure()
    if not store.steps_file.exists():
        steps: List[Step] = []
        if scene == SCENE_DESKTOP:
            steps.append(Step(id=1, action="win_activate"))
        else:
            url = initial_url.strip()
            if url:
                steps.append(Step(id=1, action="navigate", url=url))
        store.save(steps, {}, scene=scene)
    return store
