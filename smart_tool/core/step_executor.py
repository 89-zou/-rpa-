# -*- coding: utf-8 -*-
"""Playwright 步骤执行器：按 steps.json 顺序执行，支持 XPath 定位与人工暂停。

纯 Python 实现，不依赖 PyQt6，可在命令行或 QThread 中运行。
"""
import ast
import base64
import fnmatch
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from playwright.sync_api import (
    Page, TimeoutError as PlaywrightTimeout, sync_playwright,
)

from smart_tool.core import (
    auth_store, blocks, browser_setup, data_sources, datastore, desktop, free_code,
    image_locator, project_store,
)
from smart_tool.core import real_mouse as real_mouse_mod
from smart_tool.core.blocks import Block
from smart_tool.core.data_sources import DataSourceConfig, load_rows
from smart_tool.core.project_store import Locator, Step

# fill 真能填的元素：input / textarea（可编辑区域另判）；<select> 要用「下拉选择」动作
FILLABLE_TAGS = ("input", "textarea")
# 这些 input 类型 fill 填不了
UNFILLABLE_INPUT_TYPES = ("checkbox", "radio", "file", "button", "submit",
                          "reset", "image", "hidden")
# 拦下来之后建议换成哪个动作
ACTION_HINT = {
    "select": "这是下拉框，请把动作改成「下拉选择」。",
    "checkbox": "这是勾选框，请把动作改成「点击」。",
    "radio": "这是单选按钮，请把动作改成「点击」。",
}

# 桌面场景：屏幕上找图最多等多久（秒）
DESKTOP_IMAGE_WAIT_S = 10.0

# 暂停轮询间隔（秒）：兼顾响应速度与 CPU 占用
PAUSE_POLL_INTERVAL = 0.5
# 等待期间日志节流（秒），避免刷屏
PAUSE_LOG_INTERVAL = 10
# 打开网页的默认等待上限（秒）。站点慢的时候「load」事件迟迟不触发，
# 所以只等到 DOM 解析完成就算打开，页面是否稳定交给步骤里的「步骤后等待」。
# 每条【打开网页】可以在步骤编辑器里单独改这个秒数。
NAV_TIMEOUT_DEFAULT_S = 120
# 点击元素的等待上限（毫秒）
CLICK_TIMEOUT_MS = 20000
# 采集节点下载图片 / 附件的等待上限（毫秒）
DOWNLOAD_TIMEOUT_MS = 30000
# 采集节点「取一个字段」的等待上限（毫秒）。
# 它本该是「等这个元素出现」，但列表采集是「每行 × 每字段」都要查一次，
# 用默认的 30 秒会被行数放大成灾难（20 行错一个字段＝10 分钟，看着像卡死），
# 所以压到 3 秒：正常页面够用，写错了也能很快报出来。
COLLECT_TIMEOUT_MS = 3000
# 每个步骤之间的最小缓冲（秒）。站点慢的时候连点太密会丢事件
# （典型：点了发布，但页面正好在自己刷新，这一下点击就被吃掉了）
STEP_GAP_SECONDS = 1.0
# 填入前「点击获取焦点」的等待上限：点不到就退回直接 fill，不长时间卡住
FOCUS_CLICK_TIMEOUT_MS = 5000
# 步骤后等待：元素 / URL / 页面加载的上限（毫秒）
WAIT_ELEMENT_TIMEOUT_MS = 30000
WAIT_URL_TIMEOUT_MS = 60000
WAIT_PAGE_TIMEOUT_MS = 60000
# 等待轮询间隔（秒）：比暂停轮询更密，跳转后能尽快继续
WAIT_POLL_INTERVAL = 0.25
# 判定「页面已稳定」需要连续几次检查都满足（约 0.5~1 秒没有变化）
PAGE_STABLE_CHECKS = 3
# Playwright 报「选择器语法不对」时消息里会出现这些关键字
INVALID_SELECTOR_HINTS = (
    "unexpected token", "is not a valid", "not a valid xpath",
    "invalid selector", "failed to execute",
)


def _looks_invalid_selector(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in INVALID_SELECTOR_HINTS)


def _first_line(err: Exception, limit: int = 120) -> str:
    """异常信息的第一行（Playwright 的报错动辄几十行，日志里只留第一行）。"""
    text = str(err).strip()
    return text.splitlines()[0][:limit] if text else type(err).__name__


def _literal(value: str) -> str:
    """把变量值渲染成 Python 字面量：能当数字就当数字，否则当带引号的字符串。"""
    text = (value or "").strip()
    if re.fullmatch(r"-?\d+", text) or re.fullmatch(r"-?\d+\.\d+", text):
        return text
    return repr(text)


def _as_number(text: str) -> Optional[float]:
    """能当数字就当数字，否则 None（条件的「大于 / 小于」用）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rule_step(node: Any) -> Step:
    """节点的规则挂在谁身上：块挂在它的开始标记上（组合/循环/条件都一样）。"""
    return node.start if isinstance(node, Block) else node


def _node_label(node: Any) -> str:
    """日志里怎么称呼一个节点。"""
    step = _rule_step(node)
    return step.title or blocks.ACTION_CN.get(step.action, step.action)


def _to_text(value: Any) -> str:
    """任意值转成可注入 {{变量}} 的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _as_list(value: Any) -> Optional[List[Any]]:
    """把变量值当列表看：真列表原样、JSON 数组解析、文本按换行/逗号切分。

    认不出来（None / 空）返回 None，交给调用方按普通文本处理。
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    lines = [x.strip() for x in re.split(r"[\r\n]+", text) if x.strip()]
    if len(lines) > 1:
        return lines
    return [x.strip() for x in re.split(r"[,，]", lines[0]) if x.strip()]


def _item_record(item: Any) -> Dict[str, str]:
    """列表里的一项 → 本轮注入的变量。

    对象（例如「读取数据」读出来的一个文件）把字段展开成 {{loop.item.字段}}；
    标量放进 {{loop.item}}。
    """
    if isinstance(item, dict):
        rec = {"loop.item": _to_text(item)}
        for k, v in item.items():
            rec[f"loop.item.{k}"] = _to_text(v)
        return rec
    return {"loop.item": _to_text(item)}


# {{变量}} 占位符
VAR_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
# 循环自动注入的变量（不需要配置，循环里天然有值）
LOOP_VARS = ("loop.item", "loop.index")
# 运行时才有的变量前缀（{{loop.item.字段}} / {{loop.index}}）
LOOP_PREFIX = "loop."
# 图片库里认这些后缀（脚本里 /名字 找的就是它们）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def _image_ext_of(data: bytes) -> str:
    """按文件头猜图片格式（脚本把 bytes 存回图片库时用）。"""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".png"


def _to_var_text(value: Any) -> str:
    """变量统一存文本。

    列表 / 字典转成 JSON：这样「脚本返回一个列表 → 循环遍历它」能直接用
    （直接用 str() 会得到 Python 的单引号写法，循环解析不了）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict)):
        try:
            data = list(value) if isinstance(value, tuple) else value
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return str(value)


def _short_text(value: Any, limit: int = 120) -> str:
    """日志里显示返回值：太长就截断。"""
    text = _to_var_text(value)
    return text if len(text) <= limit else text[:limit] + "…"


class _ScriptTimeout(BaseException):
    """脚本超时的内部信号。

    故意继承 BaseException：这样脚本自己写的 try/except Exception 不会把它吃掉。
    """

    def __init__(self, line: int):
        super().__init__(f"脚本超时（第 {line} 行附近）")
        self.line = line


class _LoopGuard(ast.NodeTransformer):
    """给每个循环体、函数体开头插一句 `__tick__(行号)`——超时了就在那儿抛异常。

    这样 Python 死循环能真的被打断（不用子进程，`page` 照样能用）；
    缺点是卡在 time.sleep / 浏览器调用这种「等外部」的写法上时还得等它返回。
    插进去的语句沿用原节点的行号，所以报错行号跟用户看到的一致。
    """

    def _tick(self, node) -> ast.Expr:
        call = ast.Expr(value=ast.Call(
            func=ast.Name(id="__tick__", ctx=ast.Load()),
            args=[ast.Constant(value=max(1, int(node.lineno)))],
            keywords=[]))
        return ast.copy_location(call, node)

    def _guard(self, node):
        self.generic_visit(node)
        node.body.insert(0, self._tick(node))
        return node

    visit_While = visit_For = visit_AsyncFor = _guard
    visit_FunctionDef = visit_AsyncFunctionDef = _guard


def step_var_fields(step: Step) -> List[str]:
    """步骤里可能出现 {{变量}} 的文本字段。"""
    texts = [step.url, step.value, step.wait_target,
             step.resume_url, step.resume_element, step.prompt,
             step.loop_expr, step.cond_expr,
             step.loop_cond_arg,       # 循环条件：变量名 / 图片名 / 表达式
             step.loop_cond_value,
             step.win_title, step.keys, step.text,
             step.func_args,           # 调用函数：实参里可写 {{变量}}
             step.script_code]         # 自由代码：#文件的路径里可写 {{变量}}
    # 定位也可以是变量（如 {{登录框}}：元素定位存在变量清单里）
    if step.locator is not None and step.locator.type != "image":
        texts.append(step.locator.value)
    # 「读取数据」的路径可以写日期变量，如 D:\输出数据\{{年}}\{{月}}
    path = (step.data_cfg or {}).get("path")
    if isinstance(path, str):
        texts.append(path)
    # 作为「条件」里的一个动作节点时，它自带的值也可以写 {{变量}}
    texts.append(step.cond_value)
    # 「采集数据」的行定位、字段定位与属性名里也允许写 {{变量}}
    texts.append(step.collect_row)
    for item in step.collect_fields or []:
        if isinstance(item, dict):
            texts.extend([item.get("locator", ""), item.get("extra", "")])
    return [t for t in texts if t]


def collect_fields(step: Step) -> List[Dict[str, str]]:
    """「采集数据」节点里配好的字段（没写名字的丢掉）。"""
    out: List[Dict[str, str]] = []
    for item in step.collect_fields or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append(item)
    return out


def collect_outputs(step: Step) -> List[str]:
    """「采集数据」节点会产出哪些变量名。

    - 一条记录模式：每个字段一个 `{{变量.字段}}`
    - 列表模式：`{{变量}}`（JSON 数组，每项一个对象，配「循环」节点遍历）
    """
    var = (step.output_var or "").strip()
    if not var:
        return []
    if (step.collect_mode or "page") == "list":
        return [var]
    return [f"{var}.{str(f.get('name')).strip()}" for f in collect_fields(step)]


def collect_variables(steps: List[Step]) -> List[str]:
    """收集步骤中引用到的全部 {{变量}}（按出现顺序去重）。"""
    found: List[str] = []
    for s in steps:
        for text in step_var_fields(s):
            for name in VAR_PATTERN.findall(text):
                if name not in found:
                    found.append(name)
    return found


def library_written_vars(project_dir) -> List[str]:
    """函数库里所有函数写回的变量名。

    「调用函数」节点本身看不出会产出什么变量（代码在函数库里），
    所以界面上做变量检查 / 变量下拉时把它一起传进去。
    """
    if not project_dir:
        return []
    out: List[str] = []
    try:
        store = project_store.ProjectStore(Path(project_dir))
        for f in store.load_functions():
            for name in free_code.written_vars(str(f.get("code") or ""),
                                               str(f.get("lang") or "python")):
                if name not in out:
                    out.append(name)
    except Exception:
        pass                    # 函数库读不出来不算致命，别拦住界面
    return out


def produced_variables(steps: List[Step],
                       extra_names: Optional[Sequence[str]] = None
                       ) -> Dict[str, Step]:
    """会产出变量的节点 → 那个节点。

    「读取数据」「采集数据」按配置产出；「自由代码」按代码里 `@名字 = 值`
    写回的名字算（这样运行前的变量检查不会把它当成"没有来源"）。
    extra_names：函数库里的函数写回的变量名（「调用函数」节点用得到，
    但要读函数库才知道，所以由调用方传进来）。
    """
    out: Dict[str, Step] = {}
    for s in steps:
        if s.action == "read_data":
            name = (s.output_var or "").strip()
            if name:
                out[name] = s
        elif s.action == "collect":
            for name in collect_outputs(s):
                out.setdefault(name, s)
        elif s.action == "script":
            for name in free_code.written_vars(s.script_code or "", s.script_lang):
                out.setdefault(name, s)
        elif s.action == "call":
            for name in (extra_names or ()):
                out.setdefault(name, s)
    return out


def produced_loop_fields(node: Step) -> List[str]:
    """这个产出节点被「循环」遍历时，循环体里能用的 {{loop.item.字段}}。"""
    if node.action == "collect":
        if (node.collect_mode or "page") != "list":
            return []
        return [str(f.get("name")).strip() for f in collect_fields(node)]
    fields: List[str] = []
    for item in (node.data_cfg or {}).get("field_map") or []:
        if isinstance(item, dict):
            var = (item.get("var") or "").strip()
            if var and var not in fields:
                fields.append(var)
    return fields


def available_variables(steps: List[Step],
                        project_variables: Optional[dict] = None,
                        extra_names: Optional[Sequence[str]] = None) -> List[str]:
    """步骤里可以插入的变量名（变量下拉 / 提示用）。

    顺序：自定义变量 → 读取 / 采集 / 自由代码产出的变量 → 运行时变量
    （{{loop.index}} 与各产出节点的 {{loop.item.字段}}）。
    """
    names: List[str] = list(project_variables or {})
    produced = produced_variables(steps, extra_names)
    names.extend(produced)
    names.extend(extra_names or ())
    names.extend(LOOP_VARS)
    for node in produced.values():
        names.extend(f"loop.item.{f}" for f in produced_loop_fields(node))
    out: List[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def loop_fields(steps: List[Step], start: Step) -> List[str]:
    """这个循环遍历的数据有哪些字段（供 {{loop.item.字段}} 使用）。

    循环内容写的是 {{A}}、而 A 是某个「读取数据」/「采集数据」节点产出的 →
    返回那个节点配置的字段名；否则返回空（列表项是普通文本，只有 {{loop.item}} 本身）。
    """
    raw = (start.loop_expr or "").strip()
    m = VAR_PATTERN.fullmatch(raw)
    if not m:
        return []
    node = produced_variables(steps).get(m.group(1))
    if node is None:
        return []
    return produced_loop_fields(node)


def check_variables(steps: List[Step],
                    project_variables: Optional[dict] = None,
                    extra_names: Optional[Sequence[str]] = None) -> List[str]:
    """运行前检查变量是否有来源，返回问题清单（空 = 没问题）。

    变量的来源：
    1. 项目变量（手工加的账号密码之类）；
    2. 「读取数据」/「采集数据」节点产出的列表变量（循环用它遍历）；
    3. 「自由代码」里 `@名字 = 值` 写回的变量（extra_names：函数库里的函数写回的）；
    4. 循环体内自动有的 {{loop.item}} / {{loop.item.字段}} / {{loop.index}}。

    重点盯「跑起来才发现是空的」：循环外引用了 loop.*、字段名写错、
    变量根本没来源。
    """
    proj_vars = set(project_variables or ())
    produced = produced_variables(steps, extra_names)

    # 每个循环块里能用的 loop.* 名字（按步骤 id 记）
    scope_by_id: Dict[int, set] = {}
    try:
        tree = blocks.parse(steps)
    except blocks.StructureError:
        tree = []       # 结构问题由别处报错，这里不做重复提示

    def walk(nodes: List[Any], scopes: List[set]):
        for n in nodes:
            if not isinstance(n, Block):
                continue
            if n.kind == "loop":
                names = set(LOOP_VARS)
                names.update(f"loop.item.{f}" for f in loop_fields(steps, n.start))
                inner = scopes + [names]
                for s in n.steps:
                    scope_by_id[s.id] = set().union(*inner)
                walk(n.nodes, inner)
            else:
                for s in n.steps:
                    if scopes:
                        scope_by_id[s.id] = set().union(*scopes)
                walk(n.nodes, scopes)

    walk(tree, [])

    problems: List[str] = []
    seen = set()

    def add(step_id: int, text: str):
        key = (step_id, text)
        if key in seen:
            return
        seen.add(key)
        problems.append(f"步骤 {step_id}：{text}")

    # 条件里「直属动作节点」的位置：下标 → (条件下标, 第几个, 一共几个, 判断方式)
    cond_children: Dict[int, Tuple[int, int, int, str]] = {}
    all_spans = blocks.spans(steps)
    for sp in all_spans:
        if sp.kind != "condition":
            continue
        mode = steps[sp.start].cond_mode or "rule"
        kids = blocks.direct_children(all_spans, sp)
        for k, row in enumerate(kids):
            cond_children[row] = (sp.start, k, len(kids), mode)

    # 先看「读取数据」节点自身配全了没有
    for name, node in produced.items():
        if node.action != "read_data":
            continue
        if not (node.data_cfg or {}).get("type") or not (node.data_cfg or {}).get("path"):
            add(node.id, f"「读取数据」节点还没选好文件 / 文件夹（它要产出 {{{{{name}}}}}）")

    # 条件节点自己：判断的数据填了没有、里面有没有动作节点
    for sp in all_spans:
        if sp.kind != "condition":
            continue
        cond = steps[sp.start]
        if not (cond.cond_expr or "").strip():
            add(cond.id, "「条件」节点还没填「判断的数据」"
                         "（双击节点填写，如 {{loop.item.标题}}）")
        if not blocks.direct_children(all_spans, sp):
            add(cond.id, "「条件」里还没有动作节点"
                         "（在条件里点【＋ 点击创建新节点】加一个）")

    for idx, s in enumerate(steps):
        if s.action == "read_data" and not (s.output_var or "").strip():
            add(s.id, "「读取数据」节点还没填产出变量名（双击节点填写，如 文章列表）")
        if s.action == "collect":
            if not (s.output_var or "").strip():
                add(s.id, "「采集数据」节点还没填产出变量名（双击节点填写，如 采集结果）")
            if not collect_fields(s):
                add(s.id, "「采集数据」节点还没加要采集的字段"
                          "（双击节点添加：文字 / 属性 / 链接 / 图片 / 截图）")
            if (s.collect_mode or "page") == "list" and not (s.collect_row or "").strip():
                add(s.id, "「采集数据」是列表模式，但没填「每行的定位」"
                          "（比如 //div[@class='item']）")
        if s.action == "loop_start":
            if (s.loop_mode or "each") == "cond":
                kind = (s.loop_cond_kind or "element").strip()
                if not (s.loop_cond_arg or "").strip():
                    add(s.id, "「循环」选了条件方式，但还没填条件内容"
                              "（双击循环节点填写：XPath / 图片 / 变量 / 表达式）")
                elif kind == "var" and not (s.loop_cond_op or "").strip():
                    add(s.id, "「循环」的变量条件还没选判断方式"
                              "（等于 / 包含 / 大于 …）")
            elif not (s.loop_expr or "").strip():
                add(s.id, "「循环」节点还没填循环内容（双击节点填写：数字＝跑几次，"
                          "或 {{变量}}＝按它的长度跑）")
        rule = cond_children.get(idx)
        if rule and rule[3] != "expr":
            _cond, order, total, _mode = rule
            op = blocks.rule_op(s)
            if op and not blocks.rule_value(s).strip():
                add(s.id, f"「条件」里的第 {order + 1} 个动作节点选了"
                          f"「{blocks.COND_OP_CN[op]}」，但没填要比较的值")
            if not op and order < total - 1:
                add(s.id, f"「条件」里的第 {order + 1} 个动作节点是兜底"
                          "（无条件成立），排在它后面的永远轮不到"
                          "——兜底要放在最后一个")
        for text in step_var_fields(s):
            for name in VAR_PATTERN.findall(text):
                if name.startswith(LOOP_PREFIX):
                    scope = scope_by_id.get(s.id, set())
                    if not scope:
                        add(s.id, f"{{{name}}} 只在循环体里有值，"
                                  "请把这一步放进循环里（或改掉这个引用）")
                    elif name not in scope:
                        field = name[len("loop.item."):] \
                            if name.startswith("loop.item.") else name
                        add(s.id, f"循环遍历的数据里没有「{field}」这个字段，"
                                  "请检查「读取数据」节点里勾选的字段名")
                    continue
                if name in proj_vars or name in produced:
                    continue
                add(s.id, f"{{{name}}} 找不到来源："
                          "要么在【项目管理…】里加一个自定义变量，"
                          "要么用「读取数据」/「采集数据」节点产出它")
    return problems


class PauseHandle:
    """人工暂停句柄：执行器持有它轮询，UI 线程通过它下发决定。

    - manual_continue.set()  用户点"继续"
    - manual_abort.set()     用户点"终止"
    两者只可能先到一个，执行器每 0.5s 检查一次。
    """

    def __init__(self):
        self.manual_continue = threading.Event()
        self.manual_abort = threading.Event()


class AuthExpired(Exception):
    """登录态失效：中断这一轮，清掉登录态把整条流程重跑一遍。

    不是"错误"，所以不会写「步骤出错」，只有 run() 里那一层会接住它。
    """


class StepExecutor:
    """执行一组步骤。"""

    def __init__(
        self,
        steps: List[Step],
        variables: Optional[Dict[str, str]] = None,
        headless: bool = False,
        project_dir: Optional[Path] = None,
        log: Callable[[str], None] = print,
        on_pause: Optional[Callable[[Step], Optional[PauseHandle]]] = None,
        on_resume: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[int], None]] = None,
        on_state: Optional[Callable[[str], None]] = None,
        real_mouse: bool = False,
        scene: str = "web",
        auth: Optional[Dict[str, str]] = None,
    ):
        """
        :param project_dir: 项目目录，用于解析 locator.value 中相对路径的截图
                            （如 img/step_03.png）。
        :param on_pause: 暂停开始回调，返回 PauseHandle（GUI 模式）；
                         返回 None 表示调用方不支持人工信号（CLI 模式），
                         此时仅靠 resume_condition 自动检测，manual 条件则等回车。
        :param on_resume: 暂停结束回调，参数为原因：
                          auto（信号自动检测）/manual（人工继续）/abort（人工终止）。
        :param on_step: 每一步开始执行时回调（参数是步骤 id），给运行小窗显示进度。
        :param on_state: 手动暂停的状态回调：paused（停住了）/ running（继续了）。
        :param real_mouse: 用 OS 级真实鼠标点击（pyautogui）代替合成事件，
                           给 canvas / 拖拽类站点用。默认关。
        :param scene: web（浏览器）/ desktop（桌面应用：全屏截图定位 + 系统鼠标键盘）。
        :param auth: 项目级登录态配置 {"name": 用哪个, "check_locator": 登录后才有的元素}。
                     name 非空时，启动浏览器就带上 `auth/<name>.json` 里的 cookie；
                     第一个网页打开后做一次体检，失效就清掉重跑一遍（走完整登录），
                     跑完把最新 cookie 存回去（续期）；第一次会自动创建。
        """
        self.steps = steps
        self.variables = variables or {}
        self.headless = headless
        self.project_dir = Path(project_dir) if project_dir else None
        self.log = log
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_step = on_step
        self.on_state = on_state
        self.real_mouse = bool(real_mouse) and not headless
        self.desktop = scene == "desktop"
        self._real_mouse = None
        self._stop = False
        self._page: Optional[Page] = None
        # ---- 登录态（cookie / localStorage 复用）----
        auth = auth or {}
        self._auth_name = str(auth.get("name") or "").strip()
        self._auth_check_locator = str(auth.get("check_locator") or "").strip()
        self._auth_path = (auth_store.state_path(self.project_dir, self._auth_name)
                           if (self.project_dir and self._auth_name) else None)
        self._auth_using = False        # 这一轮是不是带着登录态在跑
        self._auth_checked = False      # 体检过了没有（只查第一次打开的网页）
        self._auth_expired = False      # 体检不通过 → 别把坏的状态存回去
        # 显示编号：组合是 2-4 这样的范围、结束标记没有编号，所以日志里不能直接用
        # step.id，否则跟画布上看到的数字对不上
        self._labels = {id(s): n for s, n
                        in zip(steps, blocks.step_numbers(steps)) if n}
        # 用户在小窗上点【暂停】时置位：执行器在每个步骤开始前停住等它清掉
        self._pause_requested = threading.Event()
        # 自由代码节点写出过哪些变量：循环里这些变量要跨轮保留（累加器 / 拼接）
        self._script_written: set = set()
        # 函数库（项目里的 functions）：第一次真要调用时才去读盘
        self._functions: Optional[Dict[str, Dict[str, Any]]] = None

    # ------------------------------
    # 对外控制
    # ------------------------------
    def stop(self):
        """请求停止（线程安全，主线程调用）。"""
        self._stop = True
        self._pause_requested.clear()      # 暂停中也要能停下来
        self.log("收到停止请求，将在当前步骤完成后退出。")

    def set_user_pause(self, paused: bool):
        """小窗上的手动暂停 / 继续（线程安全，主线程调用）。

        不是立刻停：执行器在**每个步骤开始前**检查一次，所以点了暂停后，
        当前这一步（连同它的等待）会跑完才停住，最长可能等上十几秒。
        """
        if paused:
            self._pause_requested.set()
        else:
            self._pause_requested.clear()

    def _check_user_pause(self) -> bool:
        """步骤边界上的检查点：用户按了暂停就停在这儿等。

        返回 False 表示这次暂停里被按了终止，调用方该收工了。
        """
        if not self._pause_requested.is_set():
            return not self._stop
        self._notify_state("paused")
        self.log("已暂停（小窗上点【继续】恢复）。")
        while self._pause_requested.is_set() and not self._stop:
            time.sleep(0.1)
        if self._stop:
            return False
        self._notify_state("running")
        self.log("继续执行。")
        return True

    def _notify_state(self, state: str):
        if self.on_state is None:
            return
        try:
            self.on_state(state)
        except Exception:
            pass

    def _on_dialog(self, dialog):
        """页面弹出 confirm/alert/离开确认时一律点「确定」。

        Playwright 默认会把弹窗「取消」掉：站点在提交表单前问一句
        「确定要发布吗」，被取消后就什么都没发生——日志上完全看不出来。
        这里统一点确定，并把弹窗内容写进日志备查。
        """
        try:
            kind = dialog.type
            msg = (dialog.message or "").replace("\n", " ")[:80]
            self.log(f"  页面弹窗（{kind}）：{msg or '（无文字）'} → 已点「确定」")
            dialog.accept()
        except Exception as e:
            self.log(f"  处理页面弹窗失败：{str(e).splitlines()[0][:100]}")

    def run(self):
        """启动浏览器并按块树执行步骤（循环 / 条件可互相嵌套）。

        登录态：配了就用——启动就把 `auth/<名字>.json` 里的 cookie 带上，
        第一个网页打开后做一次「体检」（查「登录后才有的元素」在不在）。
        体检不过＝失效：把整条流程重跑一遍（这一遍不带登录态，会走完整的登录步骤），
        跑完把最新 cookie 存回去。第一次运行时文件还不存在，会自动创建。
        """
        self._script_written.clear()        # 每一轮执行重新统计脚本产出的变量
        nodes = blocks.parse(self.steps)
        if self.desktop:
            self._run_desktop(nodes)
            return
        if self.real_mouse:
            self.log(
                "真实鼠标模式已开启：浏览器窗口要保持可见、在最前面，"
                "全程别动鼠标键盘（鼠标会被程序占用）。\n"
                "   紧急情况把鼠标猛地甩到屏幕左上角可急停；"
                "用不了时会自动退回普通点击。"
            )
        use_auth = bool(self._auth_path) and self._auth_path.exists()
        if use_auth:
            self.log(f"使用登录态「{self._auth_name}」"
                     f"（{auth_store.describe_path(self._auth_path)}）")
            if not self._auth_check_locator:
                self.log("   注意：没配「登录后才有的元素」，没法自动发现登录态失效。"
                         "建议去【项目管理…】→【登录态】里填一个（比如后台菜单的 XPath）。")
        elif self._auth_path:
            self.log(f"登录态「{self._auth_name}」还没保存过："
                     "这次会走完整流程，跑完自动存一份，以后直接登录。")
        try:
            self._run_browser(nodes, use_auth=use_auth)
        except AuthExpired:
            self.log("登录态已失效 → 清掉它，按流程里的登录步骤重跑一遍。")
            self._run_browser(nodes, use_auth=False)

    def _run_browser(self, nodes: List[Any], use_auth: bool):
        """开一个浏览器把流程跑一遍（use_auth＝这一轮带不带登录态）。"""
        self._auth_using = bool(use_auth)
        self._auth_checked = False
        self._auth_expired = False
        # 打包版不带浏览器内核：先看一眼，别让用户看到 Playwright 那句英文报错
        if not self.desktop and not browser_setup.is_installed():
            raise RuntimeError(browser_setup.hint())
        # 内核可能在「程序目录旁的浏览器文件夹」里：告诉 playwright 去那儿找
        browser_setup.ensure_env()
        kwargs = {"storage_state": str(self._auth_path)} if use_auth else {}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(**kwargs)
            self._page = context.new_page()
            # 页面弹窗一律「确定」：Playwright 默认是「取消」，
            # 于是「确定要发布/离开吗」被点成了取消，操作会静默失败
            self._page.on("dialog", self._on_dialog)
            try:
                self._run_nodes(nodes)
            finally:
                self._save_auth(context)
                self.log("执行结束，关闭浏览器。")
                browser.close()
                self._page = None

    # ------------------------------
    # 登录态：体检 / 续期 / 登录组合跳过
    # ------------------------------
    def _maybe_check_auth(self):
        """带登录态跑的时候，第一次打开网页后查一次「登录后才有的元素」。

        在不在决定后面怎么走：在＝继续（登录那几步会被跳过）；不在＝失效，
        抛 AuthExpired 让 run() 清掉登录态重跑一遍。
        """
        if self._auth_checked or not self._auth_using:
            return
        if not self._auth_check_locator:
            return
        self._auth_checked = True
        # 体检元素里也能写变量（如 {{登录后菜单}}＝元素定位），
        # 变量没定义就不体检了——宁可跳过这一关，也别误判成「失效」
        xpath = self._resolve_value(self._auth_check_locator).strip()
        if "{{" in xpath:
            self.log(f"  体检元素里有没定义的变量：{xpath[:60]} → 这次跳过体检")
            return
        try:
            found = self._page.locator(f"xpath={xpath}").count() > 0
        except Exception as e:
            self.log(f"  登录态体检出错（当作失效）：{str(e).splitlines()[0][:80]}")
            found = False
        if found:
            self.log("  登录态体检通过：已经是登录状态（登录那几步会自动跳过）。")
            return
        self.log(f"  页面上没有「{xpath}」（登录后才有的元素）→ 判定登录态已失效。")
        self._auth_expired = True
        raise AuthExpired()

    def _skip_group(self, block: Block) -> bool:
        """「登录用」的组合：当前用的是有效登录态时整块跳过。"""
        if not block.start.skip_if_logged_in or not self._auth_using:
            return False
        self.log(f"  【{block.start.title or '组合'}】是登录用的组合，"
                 "当前用的是有效登录态 → 跳过。")
        return True

    def _save_auth(self, context):
        """跑完把登录态存回去：第一次是新建，之后是续期（服务端会刷新 cookie）。"""
        if not self._auth_path or self._auth_expired:
            return
        try:
            state = context.storage_state()
        except Exception as e:
            self.log(f"  登录态保存失败：{str(e).splitlines()[0][:100]}")
            return
        if not (state or {}).get("cookies"):
            self.log("  这次没拿到任何 cookie，先不写登录态文件（免得把好的覆盖了）。")
            return
        try:
            self._auth_path.parent.mkdir(parents=True, exist_ok=True)
            self._auth_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"  登录态已更新：{self._auth_name}"
                     f"（{auth_store.describe_path(self._auth_path)}）")
        except OSError as e:
            self.log(f"  登录态保存失败：{e}")

    def _run_desktop(self, nodes: List[Any]):
        """桌面场景：不开浏览器，全屏截图定位 + 系统级鼠标键盘。"""
        if not desktop.available():
            raise desktop.DesktopError(
                "这个项目是「桌面应用」场景，但缺少依赖。\n    "
                + desktop.missing_hint()
            )
        self.log(
            "桌面场景：全屏截图定位 + 系统级鼠标键盘。\n"
            "   运行时别动鼠标键盘（程序要用它们）；也别让本工具的窗口盖住目标程序"
            "（截图会拍到它，定位就不准了）。\n"
            "   右下角会显示运行小窗，它盖住的那一小块屏幕截不到——"
            "目标控件正好在那儿的话会「找不到图片」，把目标程序窗口错开一点再跑。\n"
            "   紧急情况把鼠标猛地甩到屏幕左上角可急停。"
        )
        try:
            self._run_nodes(nodes)
        finally:
            self.log("执行结束。")

    def _run_nodes(self, nodes: List[Any]):
        """顺序执行同一层里的节点（普通步骤或嵌套的块）。"""
        for node in nodes:
            # 每个步骤开始前的检查点：能顺手处理「用户暂停」与「已停止」
            if not self._check_user_pause():
                self.log("已停止。")
                return
            if isinstance(node, Block):
                self._run_block(node)
            else:
                self._execute_step(node)
            # 步骤之间的最小缓冲：站点慢的时候连续操作太密，容易丢事件
            # （典型：点了发布但页面正在自己刷新，这一下点击就被吃掉了）
            if STEP_GAP_SECONDS > 0:
                time.sleep(STEP_GAP_SECONDS)

    def _run_block(self, block: Block):
        if block.kind == "loop":
            self._run_loop(block)
        elif block.kind == "condition":
            self._run_condition(block)
        else:
            # 组合：结构壳子，按顺序跑里面的节点
            #（如果是「登录用」的组合、而这次登录态还有效，整块跳过）
            if block.kind == "group" and self._skip_group(block):
                return
            self._run_nodes(block.nodes)

    def _run_condition(self, block: Block):
        """条件节点：算出结果，从上往下走第一个成立的动作节点；都不成立就整块跳过。

        动作节点自带规则（判断方式 + 值）；它本身也可能是「组合」之类的块，
        那就整块执行。
        """
        step = block.start
        if (step.cond_mode or "rule") == "expr":
            result, is_bool = self._condition_result(step)
            self.log(f"[条件] 表达式 {step.cond_expr} → 结果「{result}」")
            for i, node in enumerate(block.nodes):
                if self._expr_hit(_rule_step(node), i, result, is_bool):
                    self.log(f"  走「{_node_label(node)}」")
                    self._run_nodes([node])
                    return
            self.log("  没有动作节点匹配，跳过条件体。")
            return

        data = self._condition_data(step)
        self.log(f"[条件] 数据 {step.cond_expr} → 「{data}」")
        for node in block.nodes:
            why = self._rule_hit(_rule_step(node), data)
            if why:
                self.log(f"  走「{_node_label(node)}」（{why}）")
                self._run_nodes([node])
                return
        self.log("  没有动作节点匹配，跳过条件体。")

    def _condition_data(self, step: Step) -> str:
        """规则模式：条件节点提供的那份数据（渲染掉 {{变量}} 再取文本）。"""
        raw = (step.cond_expr or "").strip()
        if not raw:
            raise ValueError("条件节点还没填「判断的数据」，请点开条件节点填写")
        return self._resolve_value(raw).strip()

    def _rule_hit(self, step: Step, data: str) -> str:
        """这个动作节点成立吗？成立返回一句原因（写日志用），不成立返回空串。

        · 判断方式留空＝兜底，无条件成立；
        """
        op = blocks.rule_op(step)
        if not op:
            return "兜底"
        return self._compare(op, blocks.rule_value(step), data,
                             _node_label(step))

    def _compare(self, op: str, raw_value: str, data: str, label: str) -> str:
        """按「判断方式 + 值」比一比：成立返回一句原因，不成立返回空串。

        · 包含 / 不包含 / 等于 / 不等于：值支持逗号分隔多个，命中任意一个就算；
        · 大于 / 小于 / 大于等于 / 小于等于：两边都要能当数字。
        """
        raw = self._resolve_value(raw_value).strip()
        if not raw:
            return ""                       # 值没填 → 不成立（保存时会提示补上）
        if op in blocks.COND_NUMBER_OPS:
            left, right = _as_number(data), _as_number(raw)
            if left is None or right is None:
                raise ValueError(
                    f"「{label}」要按数字比，但「{data}」和「{raw}」不都是数字")
            hit = {"gt": left > right, "lt": left < right,
                   "ge": left >= right, "le": left <= right}[op]
            return f"{blocks.COND_OP_CN[op]} {raw}" if hit else ""
        items = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        if op == "contains":
            hit = any(v in data for v in items)
        elif op == "not_contains":
            hit = all(v not in data for v in items)
        elif op == "eq":
            hit = any(data == v for v in items)
        else:                               # ne
            hit = all(data != v for v in items)
        return f"{blocks.COND_OP_CN.get(op, op)} {raw}" if hit else ""

    def _expr_hit(self, step: Step, index: int, result: str,
                  is_bool: Optional[bool]) -> bool:
        """表达式模式：真走第 1 个动作节点、假走第 2 个；算出别的值就按「值」匹配。"""
        if is_bool is not None:
            return index == (0 if is_bool else 1)
        values = [self._resolve_value(v).strip()
                  for v in blocks.rule_values(step)]
        return result in values

    def _condition_result(self, step: Step):
        """表达式模式的结果，返回 (文本, 是否布尔值)；布尔值 None 表示按值匹配动作节点。"""
        raw = (step.cond_expr or "").strip()
        if not raw:
            raise ValueError("条件节点还没填判断内容，请点开条件节点填写")
        code = VAR_PATTERN.sub(
            lambda m: _literal(self.variables.get(m.group(1), "")), raw
        )
        scope = {"vars": self.variables, "log": self.log,
                 **self._expr_helpers()}
        try:
            value = eval(compile(code, "<条件>", "eval"), scope)   # noqa: S307
        except Exception as e:
            raise ValueError(f"条件表达式「{raw}」算不出来：{e}")
        if isinstance(value, bool):
            return ("是" if value else "否"), value
        return str(value).strip(), None

    def _run_loop(self, block: Block):
        """按循环内容逐项执行循环体。

        循环表达式（循环节点里唯一的那个输入框）：
        - 数字 10        → 跑 10 次，loop.item = 当前索引（0 起）
        - {{变量}}       → 变量值是列表/多行文本就逐项遍历、是数字就跑那么多次
        - 其他文本       → 按行 / 逗号拆成多项
        每一轮注入 {{loop.item}}（当前项，对象会展开成 {{loop.item.字段}}）
        与 {{loop.index}}（第几轮，从 1 开始）。

        循环方式选「条件」时不走这里，见 _run_while_loop。
        """
        start_step = block.start
        if (start_step.loop_mode or "each") == "cond":
            self._run_while_loop(block)
            return
        records, source_label = self._loop_records(start_step)
        if records is None:
            return

        base_vars = dict(self.variables)
        # 循环体里「自由代码」写出的变量要跨轮保留（累加器、拼接清单之类），
        # 其余变量每轮都重置回循环开始前的样子（免得带上一轮的脏值）。
        total = len(records)
        self.log(f"[循环开始] {source_label}，共 {total} 项")
        for i, rec in enumerate(records, start=1):
            if self._stop:
                self.log("已停止。")
                break
            self.log(f"──── 循环 {i}/{total} ────")
            carried_script = {
                k: v for k, v in self.variables.items()
                if k in self._script_written and not k.startswith(LOOP_PREFIX)
            }
            self.variables = {**base_vars, **carried_script, **rec,
                              "loop.index": str(i)}
            self._run_nodes(block.nodes)
        self.log(f"[循环结束] 完成 {total} 项")
        # 恢复循环外的变量；但脚本产出的（新造的 / 改过值的）保留最后一次的值
        carried = {
            k: v for k, v in self.variables.items()
            if not k.startswith(LOOP_PREFIX)
            and (k not in base_vars or k in self._script_written)
        }
        self.variables = {**base_vars, **carried}

    def _loop_records(self, start_step: Step):
        """准备每一项的记录，返回 (记录列表, 来源说明)；None 表示没有可循环的数据。"""
        raw = (start_step.loop_expr or "").strip()
        if not raw:
            raise ValueError(
                "「循环」节点还没填循环内容，请双击它填写：\n"
                "    写数字＝跑几次（如 10）；写变量＝按它的长度跑（如 {{文章列表}}）"
            )
        items, label = self._loop_items(raw)
        if not items:
            self.log(f"[循环] {label} 是空的，跳过循环体。")
            return None, ""
        return [_item_record(x) for x in items], label

    def _loop_items(self, raw: str):
        """把循环表达式解析成「要重复的项」列表，返回 (列表, 来源说明)。"""
        # 1) 纯数字：跑这么多次，每一项就是当前索引
        if raw.isdigit():
            n = int(raw)
            if n <= 0:
                self.log(f"  循环次数 {raw} 不是正数，本次不执行循环体。")
                return [], f"循环 {raw} 次"
            return list(range(n)), f"循环 {n} 次"

        # 2) 整框只写了一个变量：按它的值来决定跑几项
        m = VAR_PATTERN.fullmatch(raw)
        if m:
            name = m.group(1)
            if name not in self.variables:
                raise ValueError(
                    f"循环内容「{raw}」里的变量 {{{{{name}}}}} 现在还没有值。\n"
                    "    如果它由「读取数据」节点产出，请把那个节点放到循环前面。"
                )
            value = self.variables[name]
            text = str(value).strip()
            if text.isdigit():          # 变量值是数字 → 当作次数
                n = int(text)
                self.log(f"  变量 {{{{{name}}}}} = {n} → 循环 {n} 次")
                return list(range(max(0, n))), f"变量 {{{{{name}}}}}（{n} 次）"
            items = _as_list(value) or []
            return items, f"变量 {{{{{name}}}}}（{len(items)} 项）"

        # 3) 其他写法：渲染成文本后按行 / 逗号拆
        text = self._resolve_value(raw)
        lost = [n for n in VAR_PATTERN.findall(text) if n not in self.variables]
        if lost:
            names = "、".join(f"{{{{{n}}}}}" for n in lost)
            raise ValueError(
                f"循环内容「{raw}」里的变量 {names} 现在还没有值。\n"
                "    如果它由「读取数据」节点产出，请把那个节点放到循环前面。"
            )
        items = _as_list(text) or []
        return items, f"循环内容：{raw[:30]}（{len(items)} 项）"

    # ------------------------------
    # 条件循环（while）
    # ------------------------------
    def _run_while_loop(self, block: Block):
        """条件循环：每轮**先判断**，再决定跑不跑这一轮。

        · 成立时＝继续下一轮 → while(条件) { 循环体 }
        · 成立时＝结束循环   → until(条件) { 循环体 }（一直等到条件成立）

        每轮之间等 `loop_interval` 秒，最多跑 `loop_max` 轮（0＝不限）。
        这两道刹车是给"监控某个东西出现"这类场景兜底的：条件写错了也不至于永远空转。
        """
        start = block.start
        stop_when = bool(start.loop_cond_stop)
        interval = max(0.0, float(start.loop_interval or 0))
        limit = max(0, int(start.loop_max or 0))
        self.log(f"[循环开始] {blocks.loop_summary(start)}"
                 f"（{'成立就结束' if stop_when else '成立就继续'}；"
                 f"每轮间隔 {interval:g} 秒，最多 {limit or '不限'} 轮）")

        base_vars = dict(self.variables)
        rounds = 0
        while True:
            if self._stop:
                self.log("已停止。")
                break
            if limit and rounds >= limit:
                self.log(f"[循环结束] 到设定的最多轮数 {limit} 了，先停下"
                         "（想跑更久就把循环的「最多轮数」调大，填 0 表示不限）")
                break
            ok_cond, why = self._loop_cond_ok(start)
            if (ok_cond if stop_when else not ok_cond):
                self.log(f"[循环结束] {'按设定结束循环' if ok_cond else '条件不成立'}"
                         f"（{why}），共跑了 {rounds} 轮")
                break
            rounds += 1
            carried_script = {
                k: v for k, v in self.variables.items()
                if k in self._script_written and not k.startswith(LOOP_PREFIX)
            }
            self.variables = {**base_vars, **carried_script,
                              "loop.index": str(rounds)}
            self.log(f"──── 循环 {rounds}"
                     f"{'/' + str(limit) if limit else ''} ────")
            self._run_nodes(block.nodes)
            if interval > 0 and not self._stop:
                time.sleep(interval)

        # 恢复循环外的变量；但脚本产出的（新造的 / 改过值的）保留最后一次的值
        carried = {
            k: v for k, v in self.variables.items()
            if not k.startswith(LOOP_PREFIX)
            and (k not in base_vars or k in self._script_written)
        }
        self.variables = {**base_vars, **carried}

    def _loop_cond_ok(self, start: Step):
        """循环的条件成立吗？返回 (是否成立, 一句说明)。"""
        kind = (start.loop_cond_kind or "element").strip()
        arg = (start.loop_cond_arg or "").strip()

        if kind == "element":
            if not arg:
                raise ValueError("「循环」的条件还没填 XPath，请双击循环节点填写。")
            ok = self._element_present(self._resolve_value(arg).strip())
            return ok, f"网页元素{'在' if ok else '不在'}：{arg[:40]}"

        if kind == "image":
            if not arg:
                raise ValueError(
                    "「循环」的条件还没选图片，请双击循环节点用【选择图片…】挑一张。")
            path = self._image_map().get(arg)
            if not path:
                raise ValueError(
                    f"「循环」要看的图片「{arg}」不在项目图片库里。\n"
                    "   双击循环节点，用【选择图片…】重新挑一张（会存进项目 img/）。")
            ok = desktop.exists(path)
            return ok, f"屏幕上{'找到' if ok else '没找到'} {Path(path).name}"

        if kind == "var":
            if not arg:
                raise ValueError(
                    "「循环」的变量条件还没填变量名，请双击循环节点填写。")
            op = (start.loop_cond_op or "").strip()
            if not op:
                raise ValueError(
                    "「循环」的变量条件还没选判断方式（等于 / 包含 / 大于 …），"
                    "请双击循环节点填写。")
            data = self._resolve_value(arg).strip()
            why = self._compare(op, start.loop_cond_value, data, "循环条件")
            return bool(why), f"{arg} = 「{data}」→ {why or '不成立'}"

        if not arg:
            raise ValueError("「循环」的表达式条件还没填，请双击循环节点填写。")
        code = VAR_PATTERN.sub(
            lambda m: _literal(self.variables.get(m.group(1), "")), arg)
        scope = {"vars": self.variables, "log": self.log, **self._expr_helpers()}
        try:
            value = eval(compile(code, "<循环条件>", "eval"), scope)   # noqa: S307
        except Exception as e:
            raise ValueError(f"循环条件「{arg}」算不出来：{e}")
        return bool(value), f"表达式 {arg[:40]} → {value}"

    def _expr_helpers(self) -> Dict[str, Any]:
        """表达式里能直接用的函数（条件的表达式模式、循环的条件都能用）。

        · 元素存在("//*[@id='ok']")  网页上有没有这个元素（不等、不抛异常）
        · 图片存在("完成.png")        项目 img/ 里那张图现在在不在屏幕上
        """
        return {
            "元素存在": self._element_exists,
            "图片存在": self._image_exists,
        }

    def _element_exists(self, xpath: str) -> bool:
        """网页上这个 XPath 存在且可见吗（即时看一眼，不等、不抛异常）。"""
        return self._element_present(str(xpath or "").strip())

    def _image_exists(self, name: str, threshold: float = 0.0) -> bool:
        """项目图片库里这张图，现在在屏幕上吗（即时看一眼）。"""
        path = self._image_map().get(str(name or "").strip())
        if not path:
            return False
        return desktop.exists(path, threshold or None)

    # ------------------------------
    # 步骤分发
    # ------------------------------
    def _execute_step(self, step: Step):
        self.log(f"[步骤 {self._labels.get(id(step), step.id)}] {step.action}")
        if self.on_step is not None:
            try:
                self.on_step(step.id)      # 给运行小窗显示「跑到第几步了」
            except Exception:
                pass
        try:
            if step.action == "navigate":
                self._require_web(step, "打开网页")
                self._navigate(step)
            elif step.action == "read_data":
                self._read_data(step)
            elif step.action == "collect":
                self._require_web(step, "采集数据")
                self._collect(step)
            elif step.action == "win_activate":
                self._win_activate(step)
            elif step.action == "hotkey":
                self._hotkey(step)
            elif step.action == "delay":
                self._delay(step)
            elif step.action == "note":
                self._note(step)
            elif step.action == "click":
                if self.desktop:
                    self._desktop_click(step)
                else:
                    self._click(step)
            elif step.action == "fill":
                if self.desktop:
                    self._desktop_fill(step)
                else:
                    self._fill(step)
            elif step.action == "select":
                self._require_web(step, "下拉选择")
                self._select(step)
            elif step.action == "pause_for_human":
                self._pause_for_human(step)
            elif step.action == "script":
                self._run_script(step)
            elif step.action == "call":
                self._run_call(step)
            elif step.action in ("loop_start", "loop_end", "condition_start",
                                 "condition_end"):
                # 结构标记，正常路径在 blocks.parse 阶段已被剥离
                self.log(f"  {step.action} 为结构标记，跳过")
            else:
                self.log(f"  未知 action: {step.action}，跳过")
                return
            # pause_for_human 的恢复信号本身就是验证条件，不再重复 wait_after；
            # delay 自己就是等待，别再叠加一次「额外等待」
            if step.action not in ("pause_for_human", "delay"):
                self._wait_after(step)
            # 这个步骤自己设的额外等待（秒）：慢站点、点了没反应时加大它
            if step.action != "delay" and step.wait_seconds and step.wait_seconds > 0:
                self.log(f"  再固定等 {step.wait_seconds:g}s")
                time.sleep(float(step.wait_seconds))
        except AuthExpired:
            raise                       # 「换条路重跑」，不是步骤出错，别写错误日志
        except Exception as e:
            self.log(f"  步骤出错: {e}")
            raise

    # ------------------------------
    # 场景检查：动作别用错场景
    # ------------------------------
    def _require_web(self, step: Step, name: str):
        """桌面场景里出现网页动作 → 说清楚该怎么改。"""
        if not self.desktop:
            return
        raise ValueError(
            f"这一步是网页动作「{name}」，但当前项目是「桌面应用」场景"
            "（靠截图定位 + 系统鼠标键盘）。\n"
            "   请把它改成桌面动作（激活窗口 / 点击 / 输入文字 / 按键 / 等待），"
            "或者新建一个「网页自动化」场景的项目。"
        )

    def _require_desktop(self, step: Step, name: str):
        """网页场景里出现桌面动作 → 说清楚该怎么改。"""
        if self.desktop:
            return
        raise ValueError(
            f"这一步是桌面动作「{name}」，但当前项目是「网页自动化」场景。\n"
            "   请新建一个「桌面应用」场景的项目，或把它换成网页动作。"
        )

    # ------------------------------
    # 定位辅助
    # ------------------------------
    def _resolve_xpath(self, locator: Locator):
        """XPath → Playwright 定位器。

        XPath 里可以写 {{变量}}：捕获到的元素存成「元素定位」后（见变量清单），
        这里写 {{登录框}} 就能复用，改一处全项目都跟着变。
        """
        return self._page.locator(f"xpath={self._resolve_value(locator.value)}")

    def _resolve_image_path(self, locator: Locator) -> Path:
        """截图相对路径（相对项目目录）→ 绝对路径。"""
        p = Path(locator.value)
        if not p.is_absolute() and self.project_dir:
            p = self.project_dir / p
        return p

    def _locate_by_image(self, locator: Locator) -> image_locator.ImageMatch:
        """截图模板匹配，返回 viewport CSS 像素坐标。"""
        return image_locator.locate_on_page(
            self._page,
            self._resolve_image_path(locator),
            log=self.log,
        )

    def _resolve_value(self, value: str) -> str:
        """把 {{变量}} 替换为实际值。"""
        for k, v in self.variables.items():
            value = value.replace(f"{{{{{k}}}}}", str(v))
        return value

    # ------------------------------
    # 自由代码节点（script）：写一个真正的函数，系统自动调用它
    # ------------------------------
    def _run_script(self, step: Step):
        """跑用户写的函数（语法见 core/free_code.py）。

        · 参数：#文件=路径 写在函数签名里（路径可以写 {{变量}}）
        · 读变量：直接写变量名，或者 @名字
        · 写回变量：@名字 = 值；存回图片库：/图片名 = 图片
        """
        code = step.script_code or ""
        if not code.strip():
            self.log("  代码为空，跳过。")
            return
        self._run_free_code(step, code, (step.script_lang or "python").lower(),
                            {}, kind="script")

    # ------------------------------
    # 调用函数（函数库里的函数，一处定义多处调用）
    # ------------------------------
    def _function_lib(self) -> Dict[str, Dict[str, Any]]:
        """函数库：项目 steps.json 里的 functions（第一次用到时才读盘）。"""
        if self._functions is None:
            lib: Dict[str, Dict[str, Any]] = {}
            if self.project_dir:
                store = project_store.ProjectStore(Path(self.project_dir))
                for f in store.load_functions():
                    lib[f["name"]] = f
            self._functions = lib
        return self._functions

    def _run_call(self, step: Step):
        """调用「函数库」里的一个函数。

        实参写法 `形参名=值`（值可写 {{变量}} 或字面量）；没写的形参用函数
        签名里的默认值（文件参数＝签名里写的那个路径）。
        """
        name = (step.func_name or "").strip()
        if not name:
            raise ValueError(
                "「调用函数」节点还没选函数：双击它，在「调用函数」里选一个。\n"
                "   函数在【项目管理…】→【函数库】里定义（一处定义、多处调用）。"
            )
        func = self._function_lib().get(name)
        if func is None:
            raise ValueError(
                f"找不到函数「{name}」：可能被改名或删掉了。\n"
                "   去【项目管理…】→【函数库】里看看，或者在这个节点里重新选一个。"
            )
        code = str(func.get("code") or "")
        if not code.strip():
            self.log(f"  函数「{name}」还没有代码，跳过。")
            return
        given = dict(free_code.parse_call_args(step.func_args or ""))
        self._run_free_code(step, code, str(func.get("lang") or "python").lower(),
                            given, kind="call")

    def _run_free_code(self, step: Step, code: str, lang: str,
                       given: Dict[str, str], kind: str):
        """跑一段用户代码（自由代码节点 / 函数库里的函数）。

        given：调用方给的实参（形参名 → 值，可写 {{变量}}）；自由代码节点没有
        调用方，所以是空的——文件参数按签名里写的路径走。
        """
        fc = free_code.analyze(code, lang, list(self.variables), self._image_names(),
                               require_file_paths=(kind == "script"))
        if fc.errors:
            raise ValueError("代码有问题：\n    " + "\n    ".join(fc.errors))
        main = fc.main
        who = (f"函数「{main.name}」" if kind == "call" else f"自由代码 {main.name}")

        valid = set(main.param_names)
        unknown = [k for k in given if k not in valid]
        if unknown:
            raise ValueError(
                f"{who}没有这些参数：{'、'.join(unknown)}。\n"
                f"   它的形参是：{'、'.join(valid) or '（没有）'}"
                "——形参写在函数签名的括号里。")
        args = free_code.auto_args(fc, resolve=self._resolve_value)
        args.update({k: self._resolve_value(v) for k, v in given.items()})
        for p in main.params:           # 文件参数没给路径：提一句，省得后面莫名其妙
            if p.kind == free_code.KIND_FILE and not str(args.get(p.name) or "").strip():
                self.log(f"  提示：文件参数「{p.name}」这次没有路径"
                         "（函数里用到它就会报错）")

        summary = "、".join(f"{k}={'（空）' if v == '' else _short_text(v, 40)}"
                            for k, v in args.items())
        self.log(f"  执行 {who}" + (f"：{summary}" if summary else ""))

        timeout = max(1, int(step.script_timeout or 30))
        before = dict(self.variables)
        if lang == "javascript":
            returned, logs, js_vars, out_imgs = self._exec_free_js(
                step, fc, args, timeout, who)
            for k, v in js_vars.items():
                if before.get(str(k)) != _to_var_text(v):
                    self._write_var(str(k), v)
            for img_name, value in out_imgs.items():
                if value in (None, "", b""):
                    continue
                try:
                    self._save_image_and_log(img_name, value)
                except Exception as e:
                    self.log(f"  图片「{img_name}」存回去失败：{e}")
        else:
            returned, logs = self._exec_free_python(step, fc, args, timeout, who)

        for line in logs:
            self.log(f"  [脚本] {line}")
        if returned is not None:
            self.log(f"  {who}返回：{_short_text(returned)}")
        changed = [k for k, v in self.variables.items() if before.get(k) != v]
        if changed:
            self.log(f"  写回变量：{'、'.join(changed)}")

    # ------------------------------
    # 自由代码 / 函数的执行
    # ------------------------------
    def _exec_free_python(self, step: Step, fc, args: Dict[str, str],
                          timeout: int, who: str):
        """本机跑用户写的 Python 函数：返回 (返回值, 日志)。

        超时：编译前给每个循环体 / 函数体开头插一句「到点没」的检查，
        所以死循环会在超时那一刻被打断（进程内执行，page 照样能用）。
        例外：卡在 time.sleep(600) 或浏览器调用这种「等外部返回」的写法上，
        只能等它自己返回——所以页面上等元素请写 page.xxx(..., timeout=毫秒)。
        """
        logs: List[str] = []
        filename = f"<{who}>"
        try:
            tree = fc.python_tree(args)
        except SyntaxError as e:
            raise ValueError(
                f"Python 代码语法错误（第 {int(e.lineno or 1)} 行）：{e.msg}") from None
        tree = _LoopGuard().visit(tree)
        ast.fix_missing_locations(tree)
        compiled = compile(tree, filename, "exec")

        deadline = time.monotonic() + timeout

        def __tick__(line: int):
            if time.monotonic() > deadline:
                raise _ScriptTimeout(line)

        ns: Dict[str, Any] = {
            **self.variables,           # 变量直接当名字用（中文当标识符没问题）
            "__args__": dict(args),
            "__get_var__": self._get_var,
            "__set_var__": self._write_var,
            "__img_path__": self._image_path_or_raise,
            "__save_img__": self._save_image_and_log,
            "log": lambda m: logs.append(str(m)),
            "page": self._page,
            "current_url": self._current_url(),
            "project_dir": str(self.project_dir) if self.project_dir else "",
            "__tick__": __tick__,
        }
        started = time.monotonic()
        try:
            exec(compiled, ns)          # noqa: S102 - 运行用户自己的代码
        except _ScriptTimeout as e:
            raise ValueError(
                f"{who}超时：超过设定的 {timeout} 秒，已在第 {e.line} 行附近中断。\n"
                "    （循环里的等待请用 page.xxx(..., timeout=毫秒)；"
                "time.sleep 这种系统等待没法中断，只能等它自己醒）"
            ) from None
        except Exception as e:
            raise ValueError(
                f"{who}出错{self._error_line(e, filename)}："
                f"{type(e).__name__}: {e}{self._error_hint(e)}") from e
        used = time.monotonic() - started
        if used > timeout:
            self.log(f"  提示：{who}耗时 {used:.1f}s，已超过设定 {timeout}s"
                     "（卡在等外部返回的调用上时没法中断）")
        return ns.get("__ret__"), logs

    def _exec_free_js(self, step: Step, fc, args: Dict[str, str],
                      timeout: int, who: str):
        """在页面里跑用户写的 JS 函数。

        返回 (返回值, 日志, 页面里的变量快照, 要存回图片库的东西)。
        """
        if self._page is None:
            raise RuntimeError(f"{who}是 JavaScript，需要浏览器页面，但浏览器没启动")
        body = fc.js_body(variables=list(self.variables), timeout_ms=timeout * 1000)
        try:
            res = self._page.evaluate(body, {
                "vars": dict(self.variables),
                "args": dict(args),
                "imgs": self._image_map(),
                "url": self._current_url(),
                "project_dir": str(self.project_dir) if self.project_dir else "",
            })
        except Exception as e:
            if "__SCRIPT_TIMEOUT__" in str(e):
                raise ValueError(
                    f"{who}超时：超过设定的 {timeout} 秒，已中断。"
                    "（同步死循环会把页面卡死，那种拦不住）") from None
            raise RuntimeError(f"{who}执行失败：{e}") from e
        res = res or {}
        logs = [str(x) for x in (res.get("logs") or [])]
        return (res.get("ret"), logs, dict(res.get("vars") or {}),
                dict(res.get("imgs") or {}))

    # ---- 变量 / 图片 的读写小工具（脚本里 @名字 / /图片 用）----
    def _get_var(self, name: str) -> str:
        return self.variables.get(str(name), "")

    def _write_var(self, name: str, value: Any) -> str:
        """写回变量清单（脚本里 `@名字 = 值` 走这里）。"""
        text = _to_var_text(value)
        self._script_written.add(str(name))     # 脚本产出的：循环里跨轮保留
        self.variables[str(name)] = text
        return text

    def _image_names(self) -> List[str]:
        """图片库里所有图片的名字（不含扩展名与带扩展名两种都算）。"""
        return list(self._image_map())

    def _image_map(self) -> Dict[str, str]:
        """图片名 → 绝对路径（脚本里 /名字 用它，也整批传给 JS）。"""
        out: Dict[str, str] = {}
        if not self.project_dir:
            return out
        img_dir = Path(self.project_dir) / "img"
        if not img_dir.is_dir():
            return out
        for p in sorted(img_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.setdefault(p.stem, str(p))
                out.setdefault(p.name, str(p))
        return out

    def _image_path_or_raise(self, name: str) -> str:
        path = self._image_map().get(str(name or "").strip())
        if not path:
            raise ValueError(
                f"图片库里没有「{name}」这张图。\n"
                f"   把图片放进项目 img/ 目录（文件名用 {name}.png），"
                "或者在【项目管理…】→【图片库】里导入。")
        return path

    def _save_image_and_log(self, name: str, value: Any) -> str:
        """脚本里 `/名字 = 图片` 走这里：存好并打一行日志。"""
        rel = self._save_image(name, value)
        self.log(f"  图片已存回图片库：{rel}")
        return rel

    def _save_image(self, name: str, value: Any) -> str:
        """把图片存回图片库（img/名字.xxx），返回相对路径。"""
        if not self.project_dir:
            raise ValueError("这个脚本没有项目目录，存不回图片库。")
        key = str(name or "").strip()
        if not key:
            raise ValueError("存图片时要写名字：`/名字 = 图片`")
        img_dir = Path(self.project_dir) / "img"
        img_dir.mkdir(parents=True, exist_ok=True)

        def target(ext: str) -> Path:
            if Path(key).suffix.lower() in IMAGE_EXTS:
                return img_dir / key
            return img_dir / f"{key}{ext}"

        if isinstance(value, (str, Path)):
            text = str(value).strip()
            if text.startswith("data:image/"):          # JS 里截图回来常是这种
                head, _, b64 = text.partition(",")
                ext = ".png"
                for key_name, e in (("jpeg", ".jpg"), ("jpg", ".jpg"),
                                    ("png", ".png"), ("gif", ".gif"),
                                    ("webp", ".webp")):
                    if key_name in head.lower():
                        ext = e
                        break
                path = target(ext)
                path.write_bytes(base64.b64decode(b64))
                return f"img/{path.name}"
            src = Path(text)
            if not src.is_absolute():
                src = Path(self.project_dir) / src
            if src.is_file():
                path = target(src.suffix.lower() or ".png")
                shutil.copyfile(src, path)
                return f"img/{path.name}"
            raise ValueError(f"要存回图片库的「{text}」不是一个存在的图片文件。")
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            path = target(_image_ext_of(data))
            path.write_bytes(data)
            return f"img/{path.name}"
        try:                                            # 装了 Pillow 才支持图片对象
            from PIL.Image import Image as _PilImage
            if isinstance(value, _PilImage):
                path = target(".png")
                value.save(path)
                return f"img/{path.name}"
        except ImportError:
            pass
        raise ValueError(
            "存回图片库的图片只能是：图片文件路径、图片字节（bytes），"
            "或者 Base64 / dataURL 文本。")

    @staticmethod
    def _error_line(err: Exception, filename: str) -> str:
        """报错带行号（只认用户代码那个文件里的行）。"""
        tb, line = err.__traceback__, None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == filename:
                line = tb.tb_lineno
            tb = tb.tb_next
        return f"（第 {line} 行）" if line else ""

    @staticmethod
    def _error_hint(err: Exception) -> str:
        """新手最容易踩的几个坑，顺手提示一下。"""
        msg = str(err)
        if "builtin_function_or_method" in msg:
            return ("\n    （旧写法 vars[\"变量名\"] 已经不支持了："
                    "读变量直接写名字，写回用 @名字 = 值）")
        if isinstance(err, TypeError) and "str" in msg and "int" in msg:
            return "\n    （变量清单里的值都是文本，要算数先 int(...) / float(...)）"
        if isinstance(err, NameError):
            return ("\n    （这个名字没定义：要读变量清单里的变量，"
                    "直接写名字或 @名字）")
        if isinstance(err, KeyError):
            return "\n    （字典里没有这个键，先确认一下读到的内容）"
        return ""

    # ------------------------------
    # 动作（桌面场景）
    # ------------------------------
    def _desktop_locate(self, image: str) -> "desktop.DesktopMatch":
        """在屏幕上找这张模板图（找不到会等一会儿再试）。"""
        path = self._resolve_image_path(Locator(type="image", value=image))
        return desktop.locate(path, wait_s=DESKTOP_IMAGE_WAIT_S, log=self.log)

    def _desktop_click(self, step: Step):
        """桌面点击：屏幕上找模板 → 按坐标点（可双击）。"""
        self._require_desktop(step, "点击（截图）")
        image = (step.locator.value if step.locator else "").strip()
        if not image:
            raise ValueError(
                "桌面场景的「点击」必须选一张模板图。\n"
                "   双击这一步，在「图片模板」那一行点【截屏取模板…】框一个控件。"
            )
        m = self._desktop_locate(image)
        times = 2 if int(step.click_times or 1) >= 2 else 1
        self.log(f"  在屏幕 ({m.x:.0f},{m.y:.0f}) "
                 f"{'双击' if times == 2 else '单击'}（置信度 {m.confidence:.2f}）")
        desktop.click(m.x, m.y, times)

    def _desktop_fill(self, step: Step):
        """桌面输入：先按模板图点一下输入位置，再输入文字。

        模板图留空＝直接往当前焦点里输入（常配在「点击」之后）。
        """
        self._require_desktop(step, "输入文字")
        text = self._resolve_value(step.value)
        image = (step.locator.value if step.locator else "").strip()
        if image:
            m = self._desktop_locate(image)
            self.log(f"  先点一下输入位置 ({m.x:.0f},{m.y:.0f})"
                     f"（置信度 {m.confidence:.2f}）")
            desktop.click(m.x, m.y)
            time.sleep(0.2)
        else:
            self.log("  没配模板图 → 直接往当前焦点里输入")
        desktop.clear_field(log=self.log)
        desktop.type_text(text, log=self.log)
        self.log(f"  已输入 {len(text)} 个字符")

    def _win_activate(self, step: Step):
        """把目标窗口切到最前面（桌面流程的第一步几乎都是它）。"""
        self._require_desktop(step, "激活窗口")
        keyword = self._resolve_value(step.win_title).strip()
        desktop.activate_window(keyword, log=self.log)

    def _hotkey(self, step: Step):
        """按一个键或一组快捷键。"""
        self._require_desktop(step, "按键")
        keys = self._resolve_value(step.keys).strip()
        self.log(f"  按键：{keys}")
        desktop.hotkey(keys)

    def _delay(self, step: Step):
        """纯等待（等窗口画出来、等保存完）。"""
        secs = float(step.wait_seconds or 0)
        if secs <= 0:
            self.log("  「等待」没填秒数，跳过")
            return
        self.log(f"  等 {secs:g}s")
        time.sleep(secs)

    def _note(self, step: Step):
        """「提示 / 日志」节点：把写在卡片上的话（含 {{变量}}）打进运行日志。

        不碰浏览器、不碰桌面，纯粹是给自己看的：卡在哪一步、某个变量到底取到了
        什么，插一个这种节点就知道了。画布上它也是一张说明卡片。
        """
        text = self._resolve_value(step.text or "").strip()
        if not text:
            return
        for line in text.splitlines():
            self.log(f"  {line}")

    def _read_data(self, step: Step):
        """「读取数据」节点：读文件 / 文件夹，把结果放进 output_var。

        产出的是一个「列表变量」：每一项是一个文件（或表格的一行），
        字段名就是节点里勾选的那些；循环节点写 {{这个变量}} 就能逐项遍历，
        循环体里用 {{loop.item.字段}} 取字段。
        """
        var = (step.output_var or "").strip()
        if not var:
            raise ValueError(
                "「读取数据」节点还没填产出变量名。\n"
                "   双击这个节点，填一个名字（如 文章列表），循环节点里就能引用它。"
            )
        cfg = DataSourceConfig.from_dict(step.data_cfg or {})
        if not cfg.configured:
            raise ValueError(
                f"「读取数据」节点还没选好文件 / 文件夹（它要产出 {{{{{var}}}}}）。\n"
                "   双击这个节点，选好路径并【读取预览】勾选字段。"
            )
        cfg.path = self._resolve_value(cfg.path).strip()
        # 换机器 / 项目被搬走之后，写死的路径可能失效：按文件名在项目里找同名文件
        data_sources.set_project_dir(self.project_dir)
        found, note = data_sources.resolve_path(cfg.path)
        if note:
            self.log("  " + note)
            cfg.path = str(found)
        rows = load_rows(cfg)
        if not rows:
            self.log(f"  没读到数据（{Path(cfg.path).name}），变量 {{{{{var}}}}} 是空的。")
        self.variables[var] = _to_text(rows)
        fields: List[str] = []
        for r in rows[:1]:
            fields = list(r.keys())
        self.log(f"  读取到 {len(rows)} 项 → 变量 {{{{{var}}}}}")
        if fields:
            names = "、".join(f"{{{{loop.item.{f}}}}}" for f in fields[:6])
            self.log(f"  每个文件的字段：{names}" + ("…" if len(fields) > 6 else ""))

    # ------------------------------
    # 采集数据：把页面上的东西取下来（落盘 + 进变量）
    # ------------------------------
    def _collect(self, step: Step):
        """「采集数据」节点：按字段清单把页面上的东西取下来。

        - 一条记录模式：当前页面采一条 → 每个字段进一个变量 `{{产出.字段}}`
        - 列表模式：页面上的多行各采一条 → `{{产出}}` 是 JSON 数组，
          配「循环」节点逐行遍历，循环体里用 `{{loop.item.字段}}`

        数据一律落到项目的 `data/`：结构化数据追加进 `records.jsonl`，
        图片 / 附件 / 截图存进 `data/files/`。每条记录自动带 `_time` / `_url` / `_step`。
        """
        if self.project_dir is None:
            raise ValueError("「采集数据」要把数据存进项目目录，但当前没有项目目录")
        fields = collect_fields(step)
        if not fields:
            raise ValueError(
                "「采集数据」节点还没有要采集的字段。\n"
                "   双击这个节点，点【＋ 添加字段】选「取什么」（文字 / 属性 / 链接 / "
                "图片 / 文件 / 截图）。"
            )
        var = (step.output_var or "").strip()
        label = self._labels.get(id(step), step.id)
        if (step.collect_mode or "page") == "list":
            self._collect_list(step, fields, var, label)
            return
        record = self._collect_record(step, fields, None, 0, label, None)
        for item in fields:
            name = str(item.get("name") or "").strip()
            if var:
                value = record.get(name, "")
                self.variables[f"{var}.{name}"] = "" if value is None else str(value)
        tip = "；变量 {{" + var + ".字段}} 可以引用" if var else ""
        self.log(f"  采集完成：1 条记录 → data/{datastore.RECORDS_NAME}" + tip)

    def _collect_list(self, step: Step, fields: List[Dict[str, str]],
                      var: str, label: Any):
        """列表模式：先定位每一行，再在行内取字段。"""
        row_xpath = self._resolve_value(step.collect_row or "").strip()
        if not row_xpath:
            raise ValueError(
                "「采集数据」是列表模式，但没填「每行的定位」。\n"
                "   双击节点，在「每行的定位」里填一个能命中多行的 XPath，"
                "比如 //div[@class='item']；字段的定位就在每一行里面找。"
            )
        rows = self._page.locator(f"xpath={row_xpath}")
        try:
            total = rows.count()
        except Exception as e:
            raise ValueError(
                f"列表定位「{row_xpath}」用不了：{_first_line(e)}\n"
                "   注意这里只能填 XPath。"
            ) from None
        self.log(f"  列表采集：{row_xpath} 命中 {total} 行")
        if total == 0:
            self.log("  一行都没命中：确认页面已经加载出来、定位也写对了。")
        stats: Dict[str, Any] = {"problems": {}, "skip": set()}
        records: List[Dict] = []
        for i in range(total):
            records.append(self._collect_record(step, fields, rows.nth(i),
                                                i + 1, label, stats))
            if total > 20 and (i + 1) % 20 == 0:
                self.log(f"    已采 {i + 1}/{total} 行…")
        if var:
            self.variables[var] = json.dumps(records, ensure_ascii=False)
        self.log(f"  采集完成：{len(records)} 条记录 → data/{datastore.RECORDS_NAME}"
                 + (f"；变量 {{{{{var}}}}} 可配「循环」逐行遍历" if var else ""))
        for name, (count, reason) in stats["problems"].items():
            self.log(f"    字段「{name}」有 {count} 条没取到（{reason}）")

    def _collect_record(self, step: Step, fields: List[Dict[str, str]], row,
                        index: int, label: Any,
                        stats: Optional[Dict[str, Any]]) -> Dict:
        """采一条记录并写盘；stats 是列表模式的汇总（problems / skip）。"""
        record: Dict[str, Any] = {
            "_time": datastore.now_text(),
            "_url": self._current_url(),
            "_step": label,
        }
        for item in fields:
            name = str(item.get("name") or "").strip()
            if stats is not None and name in stats["skip"]:
                record[name] = ""
                continue
            try:
                value = self._collect_value(item, row, index)
            except Exception as e:
                value = ""
                self._collect_problem(name, _first_line(e), stats)
            record[name] = value
        datastore.append_record(self.project_dir, record)
        return record

    def _collect_problem(self, name: str, reason: str,
                         stats: Optional[Dict[str, Any]]):
        """某个字段没取到：第一次立刻写日志，连续失败 3 行就不再试它了。

        列表采集是「每行 × 每字段」都要查一次，一个定位写错就会被行数放大
        （1000 行 × 3 秒＝50 分钟），所以：第一条立刻告诉你，之后不再空耗。
        """
        if stats is None:
            self.log(f"    字段「{name}」没取到：{reason}")
            return
        problems = stats["problems"]
        slot = problems.setdefault(name, [0, reason])
        slot[0] += 1
        if slot[0] == 1:
            self.log(f"    字段「{name}」没取到：{reason}")
        elif slot[0] >= 3 and name not in stats["skip"]:
            stats["skip"].add(name)
            self.log(f"    字段「{name}」连续 3 行都没取到，后面的行不再试它"
                     "（先检查定位写对没有）")

    def _locator_for(self, locator: str, row):
        """列表模式在「当前行」里找（Playwright 的嵌套 XPath 就是元素内定位）。"""
        if not locator:
            return None
        if row is not None:
            return row.locator(f"xpath={locator}")
        return self._page.locator(f"xpath={locator}")

    def _collect_value(self, item: Dict[str, str], row, index: int) -> str:
        """取一个字段的值（返回能写进记录 / 变量的文本）。"""
        kind = str(item.get("kind") or "text").lower()
        locator = self._resolve_value(str(item.get("locator") or "")).strip()
        extra = str(item.get("extra") or "").strip()
        if kind == "shot":
            return self._collect_shot(item, locator, extra, row, index)
        target = self._locator_for(locator, row)
        if target is None:
            raise ValueError("没填定位（XPath）")
        el = target.first
        if kind == "text":
            return (el.inner_text(timeout=COLLECT_TIMEOUT_MS) or "").strip()
        if kind == "html":
            return el.inner_html(timeout=COLLECT_TIMEOUT_MS)
        if kind == "attr":
            if not extra:
                raise ValueError("取属性时要填属性名（如 src / title）")
            return el.get_attribute(extra, timeout=COLLECT_TIMEOUT_MS) or ""
        if kind == "link":
            href = el.get_attribute(extra or "href",
                                    timeout=COLLECT_TIMEOUT_MS) or ""
            return urljoin(self._current_url(), href) if href else ""
        if kind in ("image", "file"):
            attr = extra or ("src" if kind == "image" else "href")
            src = el.get_attribute(attr, timeout=COLLECT_TIMEOUT_MS) or ""
            if not src:
                raise ValueError(f"这个元素没有 {attr} 属性")
            return self._download(urljoin(self._current_url(), src), item, index)
        raise ValueError(f"不认识的采集方式：{kind}")

    def _collect_shot(self, item: Dict[str, str], locator: str, extra: str,
                      row, index: int) -> str:
        """截图：元素（默认）/ 整页（附加写「整页」）/ 区域（附加写 x,y,宽,高）。"""
        stem = (f"{time.strftime('%Y%m%d_%H%M%S')}_{index or 1}_"
                f"{datastore.safe_stem(item.get('name'))}")
        if extra in ("", "元素", "element"):
            target = self._locator_for(locator, row)
            if target is None:
                raise ValueError("截元素要填定位；想截整页就在「附加」里写「整页」")
            data = target.first.screenshot(timeout=COLLECT_TIMEOUT_MS)
        elif extra in ("整页", "全页", "page", "full"):
            data = self._page.screenshot(full_page=True)
            if "整页" not in stem and "全页" not in stem:
                stem += "_整页"
        else:
            data = self._page.screenshot(clip=self._parse_area(extra))
        return datastore.save_bytes(self.project_dir, stem, ".png", data)

    @staticmethod
    def _parse_area(text: str) -> Dict[str, float]:
        """「x,y,宽,高」→ Playwright 的 clip 参数。"""
        parts = [p.strip() for p in text.replace("，", ",").split(",")]
        if len(parts) != 4:
            raise ValueError(
                "区域截图要在「附加」里填 x,y,宽,高（如 0,120,800,600），"
                "或留空＝截那个元素、写「整页」＝截整页")
        try:
            x, y, w, h = (float(p) for p in parts)
        except ValueError:
            raise ValueError("区域坐标得是数字：x,y,宽,高") from None
        if w <= 0 or h <= 0:
            raise ValueError("区域的宽 / 高要大于 0")
        return {"x": x, "y": y, "width": w, "height": h}

    def _download(self, url: str, item: Dict[str, str], index: int) -> str:
        """把图片 / 附件下载进 data/files/，返回相对项目的路径。"""
        if url.startswith("data:"):
            raise ValueError("这是内嵌的 data: 图片，用「截图」方式取它更稳")
        try:
            resp = self._page.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
        except Exception as e:
            raise ValueError(f"下载失败：{_first_line(e)}") from None
        if not resp.ok:
            raise ValueError(f"下载失败（HTTP {resp.status}）")
        body = resp.body()
        if not body:
            raise ValueError("下载到的是空文件")
        ext = datastore.guess_ext(url, resp.headers.get("content-type", ""))
        stem = (f"{time.strftime('%Y%m%d_%H%M%S')}_{index or 1}_"
                f"{datastore.safe_stem(item.get('name'))}")
        return datastore.save_bytes(self.project_dir, stem, ext, body)

    def _navigate(self, step: Step):
        """打开网页。

        只等到 DOM 解析完成（domcontentloaded）：慢站点上图片、统计脚本之类
        会把 load 事件拖到几十秒，等它很容易一步就把整个流程卡死。
        页面「是否稳定」交给这一步的「步骤后等待」（页面加载完成 / 元素出现…），
        那些等待是轮询实现，超时也只记一条日志、不会中断流程。

        等多久由这一步的「打开超时」决定（默认 NAV_TIMEOUT_DEFAULT_S 秒）。
        """
        secs = int(step.nav_timeout or NAV_TIMEOUT_DEFAULT_S)
        url = self._resolve_value(step.url or "").strip()
        missing = [n for n in VAR_PATTERN.findall(url) if n not in self.variables]
        if missing or not url:
            names = "、".join(f"{{{{{n}}}}}" for n in missing)
            raise ValueError(
                f"「打开网页」的网址现在填不出有效地址：{url or '（空）'}\n"
                + (f"    变量 {names} 还没有值（循环里的 {{loop.item.字段}} 只能在循环体里用）。"
                   if missing else "    请双击这一步填写网址。")
            )
        try:
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=secs * 1000)
        except PlaywrightTimeout as e:
            raise TimeoutError(
                f"打开网页超过 {secs}s 还没响应：{url}\n"
                f"   当前页面：{self._current_url() or '（空白页）'}\n"
                f"   可以双击这一步，把「打开超时」调大（现在 {secs} 秒）。"
            ) from e
        # 带登录态跑的话，趁第一次打开网页做一次「登录态体检」
        self._maybe_check_auth()

    def _click(self, step: Step):
        if not step.locator:
            raise ValueError("click 步骤缺少 locator")
        if step.locator.type == "image":
            m = self._locate_by_image(step.locator)
            self._mouse_click(m.x, m.y)
            return
        loc = self._resolve_xpath(step.locator)
        try:
            if self.real_mouse:
                self._click_by_real_mouse(loc)
            else:
                loc.click(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            if self._retry_by_image(step, f"点击没成功（{_first_line(e)}）"):
                return
            raise TimeoutError(
                f"等不到可点击的元素（{CLICK_TIMEOUT_MS // 1000}s）：{step.locator.value}\n"
                f"   当前页面：{self._current_url() or '（正在跳转中）'}\n"
                "   多半是上一步之后没跳到你以为的页面，或这个 XPath 属于另一个页面。"
            ) from e

    # ------------------------------
    # 鼠标：普通（CDP 合成）/ 真实（OS 级）
    # ------------------------------
    def _mouse_click(self, x: float, y: float):
        """在 viewport 坐标 (x, y) 点一下鼠标。

        默认走 Playwright 的合成事件；开了「真实鼠标」就交给 pyautogui 发
        OS 级输入（个别站点只认真输入）。用不了就记一条日志、退回普通点击，
        不让整个流程因为它挂掉。
        """
        if self.real_mouse:
            try:
                if self._real_mouse is None:
                    self._real_mouse = real_mouse_mod.RealMouse(
                        self._page, log=self.log)
                self._real_mouse.click(x, y)
                return
            except real_mouse_mod.RealMouseUnavailable as e:
                self.log(f"  真实鼠标用不了：{_first_line(e)}")
                self.real_mouse = False        # 本次运行不再试
            except Exception as e:
                if type(e).__name__ == "FailSafeException":
                    self.log("  真实鼠标急停（你把鼠标甩到屏幕角落了）→ 这一步改用普通点击")
                else:
                    self.log(f"  真实鼠标出错：{_first_line(e)} → 改用普通点击")
        self._page.mouse.click(x, y)

    def _click_by_real_mouse(self, loc, timeout_ms: int = CLICK_TIMEOUT_MS):
        """真实鼠标模式下点一个元素：等它可见 → 滚进视野 → 取中心发真点击。

        真点击绕过了 Playwright 的可点击性检查，所以这里自己补上「等可见 +
        滚进视野」，免得点到别的东西上。
        """
        loc.wait_for(state="visible", timeout=timeout_ms)
        try:
            loc.scroll_into_view_if_needed(timeout=FOCUS_CLICK_TIMEOUT_MS)
        except Exception:
            pass                    # 滚不动就算了，box 拿得到就行
        box = loc.bounding_box()
        if not box:
            raise TimeoutError("元素没有可见位置，算不出真实点击的坐标")
        self._mouse_click(box["x"] + box["width"] / 2,
                          box["y"] + box["height"] / 2)

    # ------------------------------
    # 兜底：XPath 不行就改用元素截图定位
    # ------------------------------
    @staticmethod
    def _fallback_image(step: Step) -> str:
        """这个步骤配的兜底截图（没配返回空串）。"""
        loc = step.locator
        if loc is None or loc.type != "xpath":
            return ""
        return (loc.image or "").strip()

    def _retry_by_image(self, step: Step, why: str) -> bool:
        """XPath 动作失败时，若配了兜底截图就改用截图定位点一下。

        返回是否成功；没配截图或截图也没匹配上返回 False，
        交给调用方抛它原来那个错误（报错信息更准确）。
        """
        image = self._fallback_image(step)
        if not image:
            return False
        self.log(f"  {why} → 改用兜底截图定位：{Path(image).name}")
        try:
            m = self._locate_by_image(Locator(type="image", value=image))
        except Exception as e:
            self.log(f"  兜底截图也没匹配上：{_first_line(e)}")
            return False
        self._mouse_click(m.x, m.y)
        self.log(f"  已按截图坐标点击（置信度 {m.confidence:.2f}）")
        return True

    def _fill_by_image(self, locator: Locator, text: str):
        """截图定位没有 DOM 句柄：点击聚焦 → 全选清空 → 插入文本。

        insertText 直接走输入法通道，中文等非 ASCII 字符也能可靠写入。
        """
        m = self._locate_by_image(locator)
        self._mouse_click(m.x, m.y)
        self._page.keyboard.press("Control+A")
        self._page.keyboard.press("Delete")
        self._page.keyboard.insert_text(text)
        self.log(f"  已按截图坐标填入 {len(text)} 个字符（置信度 {m.confidence:.2f}）")

    def _fill(self, step: Step):
        if not step.locator:
            raise ValueError("fill 步骤缺少 locator")
        text = self._resolve_value(step.value)
        if step.locator.type == "image":
            self._fill_by_image(step.locator, text)
            return
        loc = self._resolve_xpath(step.locator)
        try:
            self._ensure_fillable(loc)
            # 先点一下元素中心拿焦点，再填入：
            # 富文本框、需要激活才可写的框，直接 fill 会填不进去。
            self._focus_by_click(loc)
            loc.fill(text, timeout=20000)
        except Exception as e:
            image = self._fallback_image(step)
            if not image:
                raise
            self.log(f"  填入失败（{_first_line(e)}）→ 改用兜底截图定位")
            try:
                self._fill_by_image(Locator(type="image", value=image), text)
                return
            except Exception as e2:
                self.log(f"  兜底截图也没成功：{_first_line(e2)}")
                raise e
        self._verify_filled(loc, text)

    def _ensure_fillable(self, loc) -> None:
        """填入前确认目标是「fill 真能填的元素」，否则给一句看得懂的中文提示。

        手写 XPath 很容易指到外层容器（例如把标题框写成 #edit-slug-box 这个 div），
        Playwright 只会抛一大段英文报错；这里提前拦下来，顺便说清该换哪个动作。
        元素还没出现时不在这里干等，交给 Playwright 报它自己的错。
        """
        try:
            if loc.count() == 0:
                return
            info = loc.first.evaluate(
                "el => ({tag: el.tagName.toLowerCase(), id: el.id,"
                "        type: (el.getAttribute('type') || '').toLowerCase(),"
                "        editable: el.isContentEditable})"
            )
        except Exception:
            return              # 读不到信息就不拦
        if not info:
            return
        tag = info.get("tag") or ""
        itype = info.get("type") or ""
        if info.get("editable"):
            return
        if tag in FILLABLE_TAGS and itype not in UNFILLABLE_INPUT_TYPES:
            return
        who = f"<{tag}{(' id=' + info['id']) if info.get('id') else ''}>"
        hint = ACTION_HINT.get(tag if tag == "select" else itype)
        raise ValueError(
            f"这个 XPath 定位到的是 {who}，不能直接填。"
            + (f"\n   {hint}" if hint
               else "\n   只有 input / textarea / 可编辑区域能填，"
                    "请检查这个 XPath 是不是指到了外层容器或不可填的控件。")
        )

    def _verify_filled(self, loc, expected: str) -> bool:
        """填完读回一次，并把「实际填到了哪个元素」写进日志。

        这样填入失败、被页面脚本清空、或者 XPath 指到了别的输入框
        （日志里的 id 不是你以为的那个），都能一眼看出来。
        """
        ident = self._element_ident(loc)
        try:
            actual = loc.input_value(timeout=2000)
        except Exception:
            self.log(f"  已填入 {len(expected)} 个字符{ident}")
            return True          # 富文本等读不回来，跳过校验
        if actual == expected:
            self.log(f"  填入校验通过：{len(expected)} 个字符{ident}")
            return True
        if not actual:
            self.log(f"  填入校验失败：读回是空的！请检查这个 XPath{ident}")
        else:
            self.log(f"  填入校验：读回内容不一致，实际是 {actual[:40]!r}{ident}")
        return False

    @staticmethod
    def _element_ident(loc) -> str:
        """定位到的元素是谁（id / name），便于确认有没有填错框。"""
        for attr in ("id", "name"):
            try:
                val = loc.get_attribute(attr, timeout=2000)
            except Exception:
                return ""
            if val:
                return f"（目标元素 {attr}={val}）"
        return ""

    def _focus_by_click(self, loc) -> bool:
        """点击元素中心获取焦点。

        点不到（被遮挡、不可点、元素已消失等）不算失败，退回直接 fill，
        让 Playwright 自己报真正的错误原因。
        """
        try:
            if self.real_mouse:
                self._click_by_real_mouse(loc, FOCUS_CLICK_TIMEOUT_MS)
            else:
                loc.click(timeout=FOCUS_CLICK_TIMEOUT_MS)
            self.log("  已点击元素中心获取焦点")
            return True
        except Exception as e:
            self.log(f"  点击获取焦点没成功（改为直接填入）：{_first_line(e)}")
            return False

    def _select(self, step: Step):
        if not step.locator:
            raise ValueError("select 步骤缺少 locator")
        if step.locator.type == "image":
            raise ValueError("select 动作暂不支持截图定位，请使用 XPath")
        value = self._resolve_value(step.value)
        self.log(f"  下拉选择：{value}")
        self._resolve_xpath(step.locator).select_option(value, timeout=20000)

    def _pause_for_human(self, step: Step):
        """暂停等待人工操作（验证码/人机验证）。

        恢复方式三选一，先到先得：
        1. 人工信号：UI 点"继续/终止"（PauseHandle）
        2. 自动信号：resume_condition 满足（URL 变化 / DOM 元素 / 双重信号）
        3. 超时：resume_timeout 秒后抛 TimeoutError
        """
        msg = step.prompt or "请人工操作（如验证码），完成后程序将自动继续"
        cond = step.resume_condition or "manual"
        if self.desktop:
            cond = "manual"      # 桌面没有 URL / DOM，只能人工点「继续」
        timeout = max(1, int(step.resume_timeout or 300))
        self.log(f"  [暂停] {msg}")
        self.log(
            f"  恢复方式: {self._describe_condition(step)}，超时 {timeout}s"
        )

        handle = self.on_pause(step) if self.on_pause else None

        # CLI 模式 + 仅人工：保留回车交互
        if handle is None and cond == "manual":
            input("  按回车继续...")
            self._notify_resume("manual")
            return

        deadline = time.monotonic() + timeout
        last_log = time.monotonic()
        while True:
            # 1) 停止请求（含 UI 点"终止"）
            if self._stop or (handle is not None and handle.manual_abort.is_set()):
                self._stop = True
                self.log("  人工终止执行。")
                self._notify_resume("abort")
                return
            # 2) 人工点"继续"
            if handle is not None and handle.manual_continue.is_set():
                self.log("  人工确认继续。")
                self._notify_resume("manual")
                return
            # 3) 页面自动信号
            if cond != "manual":
                ok, reason = self._check_resume_condition(step, cond)
                if ok:
                    self.log(f"  检测到人工操作完成（{reason}），自动继续。")
                    self._notify_resume("auto")
                    return
            # 4) 超时
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"暂停等待人工操作超时（{timeout}s），"
                    f"步骤 {self._labels.get(id(step), step.id)} 未满足恢复条件："
                    f"{self._describe_condition(step)}"
                )
            # 5) 节流日志：让用户知道仍在等待
            if time.monotonic() - last_log >= PAUSE_LOG_INTERVAL:
                self.log(f"  仍在等待人工操作，剩余 {int(remaining)}s ...")
                last_log = time.monotonic()
            time.sleep(PAUSE_POLL_INTERVAL)

    def _notify_resume(self, reason: str):
        """通知外部暂停已结束（GUI 用于复位按钮状态）。"""
        if self.on_resume:
            try:
                self.on_resume(reason)
            except Exception:
                pass

    @staticmethod
    def _describe_condition(step: Step) -> str:
        """把恢复条件翻译成人话，用于日志。"""
        cond = step.resume_condition or "manual"
        if cond == "manual":
            return "仅人工继续"
        parts = []
        if step.resume_url:
            parts.append(f"URL 包含 {step.resume_url}")
        if step.resume_element:
            parts.append(f"元素出现 {step.resume_element}")
        label = {
            "url_changed": "URL 变化",
            "element_present": "DOM 元素出现",
            "url_and_element": "URL 变化 + DOM 元素双重确认",
        }.get(cond, cond)
        return f"{label}（{'，'.join(parts)}）" if parts else label

    # ------------------------------
    # 人工暂停恢复信号检测
    # ------------------------------
    def _current_url(self) -> str:
        """实时获取当前 URL。

        注意：不能用 page.url 属性——Playwright sync API 在 time.sleep 阻塞
        期间不分发事件，page.url 会是过期缓存值。必须发一次 evaluate IPC
        实时查询（同时泵事件循环），否则暂停轮询永远检测不到跳转。
        导航提交瞬间 evaluate 可能抛错（执行上下文销毁），返回空串等下轮重试。
        """
        try:
            return self._page.evaluate("() => location.href") or ""
        except Exception:
            return ""

    def _url_matches(self, pattern: str) -> bool:
        """当前 URL 是否匹配：含 * 走 fnmatch 通配，否则子串包含。检测本身不抛异常。"""
        if not pattern:
            return False
        url = self._current_url()
        if not url:
            return False
        if "*" in pattern:
            return fnmatch.fnmatch(url, pattern)
        return pattern in url

    def _element_present(self, xpath: str) -> bool:
        """XPath 元素存在且可见。count()/is_visible() 均为即时检查，不等待、不抛异常。"""
        if not xpath:
            return False
        try:
            loc = self._page.locator(f"xpath={xpath}")
            return loc.count() > 0 and loc.first.is_visible()
        except Exception:
            return False

    def _check_resume_condition(self, step: Step, cond: str):
        """检查恢复条件，返回 (是否满足, 原因描述)。

        url_and_element 要求两个字段都配置且同时满足——这是防误判的关键：
        仅 URL 跳转（重定向中间页）或仅元素残留都不会触发恢复。
        """
        # 恢复条件里也能写 {{变量}}（元素定位同样适用）
        pattern = self._resolve_value(step.resume_url or "").strip()
        element = self._resolve_value(step.resume_element or "").strip()
        url_ok = self._url_matches(pattern) if pattern else False
        elem_ok = self._element_present(element) if element else False

        if cond == "url_changed":
            if not pattern:
                return False, "未配置 resume_url"
            return url_ok, f"URL 已包含 {pattern}"
        if cond == "element_present":
            if not element:
                return False, "未配置 resume_element"
            return elem_ok, f"元素已出现 {element}"
        if cond == "url_and_element":
            if not (pattern and element):
                return False, "双重信号需同时配置 resume_url 与 resume_element"
            if url_ok and elem_ok:
                return True, "URL 与目标元素均已就绪"
            return False, "双重信号未同时满足"
        return False, f"未知恢复条件 {cond}"

    # ------------------------------
    # 步骤后等待
    # ------------------------------
    def _wait_after(self, step: Step):
        if not step.wait_after:
            return
        target = self._resolve_value(step.wait_target or "").strip()
        if self.desktop:
            self._wait_after_desktop(step.wait_after, target)
            return
        if step.wait_after == "element_present":
            if not target:
                return
            self.log(f"  等待元素出现: {target}")
            self._wait_element(target, WAIT_ELEMENT_TIMEOUT_MS)
        elif step.wait_after == "url_changed":
            if not target:
                return
            self.log(f"  等待 URL 变化: {target}")
            self._wait_url(target, WAIT_URL_TIMEOUT_MS)
        elif step.wait_after == "page_load":
            self.log("  等待页面跳转 / 加载完成")
            self._wait_page_ready(WAIT_PAGE_TIMEOUT_MS)
        elif step.wait_after == "network_idle":
            self.log("  等待网络空闲")
            try:
                self._page.wait_for_load_state("networkidle", timeout=WAIT_PAGE_TIMEOUT_MS)
            except Exception as e:
                self.log(f"  等待网络空闲超时（继续执行）：{e}")
        elif step.wait_after == "manual":
            pass

    def _wait_after_desktop(self, mode: str, target: str):
        """桌面场景的「步骤后等待」：等图片出现 / 等图片消失。

        桌面没有 URL、也没有 DOM，所以网页那些等待方式在这里不适用。
        """
        if mode == "manual":
            return
        if not target:
            self.log("  桌面场景的「步骤后等待」要填一张图片模板，这一步跳过等待")
            return
        path = self._resolve_image_path(Locator(type="image", value=target))
        if mode == "element_present":
            self.log(f"  等待图片出现：{Path(target).name}")
            desktop.locate(path, wait_s=WAIT_ELEMENT_TIMEOUT_MS / 1000,
                           log=self.log)
            return
        if mode == "image_gone":
            self.log(f"  等待图片消失：{Path(target).name}")
            desktop.wait_gone(path, wait_s=WAIT_ELEMENT_TIMEOUT_MS / 1000,
                              log=self.log)
            return
        self.log(f"  桌面场景不支持「{mode}」这种等待，已跳过（可改用「等待图片出现」）")

    def _wait_element(self, target: str, timeout_ms: int):
        """等元素可见，能扛住跳转/刷新。

        点击后常常整页跳转：执行上下文被销毁、文档重建，这些都只是
        「还没好」，不能当成错误中断整个流程，一直轮询到超时为止。
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                loc = self._page.locator(f"xpath={target}")
                if loc.count() > 0 and loc.first.is_visible():
                    self.log("  目标元素已出现")
                    return
            except Exception as e:
                msg = str(e)
                if _looks_invalid_selector(msg):
                    raise ValueError(
                        f"等待目标不是合法的 XPath：{target}\n"
                        f"    这里只能填 XPath（例如 //*[@id='wpadminbar']），"
                        f"说明文字请写到步骤的【备注】里"
                    ) from e
                # 正在跳转/上下文销毁 → 下一轮再试
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"等待元素出现超时（{timeout_ms / 1000:.0f}s）：{target}"
                )
            time.sleep(WAIT_POLL_INTERVAL)

    def _wait_url(self, pattern: str, timeout_ms: int):
        """等 URL 命中 pattern（含 * 走通配，否则子串包含）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self._url_matches(pattern):
                self.log(f"  网址已变为：{self._current_url()}")
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"等待 URL 变化超时（{timeout_ms / 1000:.0f}s）：当前 {self._current_url()}"
                )
            time.sleep(WAIT_POLL_INTERVAL)

    def _wait_page_ready(self, timeout_ms: int):
        """等页面跳转/刷新结束：readyState 为 complete 且 URL 连续几轮不再变化。

        用「连续稳定」而不是固定睡几秒来判断，既不会在没跳转时白等，
        也能覆盖「点击后过一瞬才开始跳转」的情况。
        """
        deadline = time.monotonic() + timeout_ms / 1000
        stable, last_url = 0, self._current_url()
        while time.monotonic() < deadline:
            try:
                state = self._page.evaluate("() => document.readyState")
            except Exception:
                state = ""            # 正在跳转，上下文已销毁
            url = self._current_url()
            if state == "complete" and url:
                if url == last_url:
                    stable += 1
                    if stable >= PAGE_STABLE_CHECKS:
                        self.log(f"  页面已加载完成：{url}")
                        return
                else:
                    stable = 0
                    last_url = url
            else:
                stable = 0
                if url:
                    last_url = url
            time.sleep(WAIT_POLL_INTERVAL)
        self.log(f"  等待页面加载完成超时（{timeout_ms / 1000:.0f}s），继续执行")
