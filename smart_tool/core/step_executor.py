# -*- coding: utf-8 -*-
"""Playwright 步骤执行器：按 steps.json 顺序执行，支持 XPath 定位与人工暂停。

纯 Python 实现，不依赖 PyQt6，可在命令行或 QThread 中运行。
"""
import fnmatch
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.sync_api import (
    Page, TimeoutError as PlaywrightTimeout, sync_playwright,
)

from smart_tool.core import blocks, desktop, image_locator
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
# Playwright 页面默认超时（毫秒）。脚本节点会临时改小，用完恢复到这个值。
DEFAULT_PAGE_TIMEOUT_MS = 30000
# 打开网页的默认等待上限（秒）。站点慢的时候「load」事件迟迟不触发，
# 所以只等到 DOM 解析完成就算打开，页面是否稳定交给步骤里的「步骤后等待」。
# 每条【打开网页】可以在步骤编辑器里单独改这个秒数。
NAV_TIMEOUT_DEFAULT_S = 120
# 点击元素的等待上限（毫秒）
CLICK_TIMEOUT_MS = 20000
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


def step_var_fields(step: Step) -> List[str]:
    """步骤里可能出现 {{变量}} 的文本字段。"""
    texts = [step.url, step.value, step.wait_target,
             step.resume_url, step.resume_element, step.prompt,
             step.loop_expr, step.cond_expr,
             step.win_title, step.keys]
    # 「读取数据」的路径可以写日期变量，如 D:\输出数据\{{年}}\{{月}}
    path = (step.data_cfg or {}).get("path")
    if isinstance(path, str):
        texts.append(path)
    # 条件分支的匹配值也允许写 {{变量}}（运行时先渲染再比）
    texts += [m.get("values", "") for m in (step.cond_branches or [])]
    return [t for t in texts if t]


def collect_variables(steps: List[Step]) -> List[str]:
    """收集步骤中引用到的全部 {{变量}}（按出现顺序去重）。"""
    found: List[str] = []
    for s in steps:
        for text in step_var_fields(s):
            for name in VAR_PATTERN.findall(text):
                if name not in found:
                    found.append(name)
    return found


def produced_variables(steps: List[Step]) -> Dict[str, Step]:
    """「读取数据」节点产出的变量 → 那个节点。"""
    out: Dict[str, Step] = {}
    for s in steps:
        if s.action == "read_data":
            name = (s.output_var or "").strip()
            if name:
                out[name] = s
    return out


def available_variables(steps: List[Step],
                        project_variables: Optional[dict] = None) -> List[str]:
    """步骤里可以插入的变量名（变量下拉 / 提示用）。

    顺序：自定义变量 → 读取节点产出的列表变量 → 运行时变量
    （{{loop.index}} 与各读取节点的 {{loop.item.字段}}）。
    """
    names: List[str] = list(project_variables or {})
    produced = produced_variables(steps)
    names.extend(produced)
    names.extend(LOOP_VARS)
    for node in produced.values():
        for item in (node.data_cfg or {}).get("field_map") or []:
            var = (item.get("var") or "").strip() if isinstance(item, dict) else ""
            if var:
                names.append(f"loop.item.{var}")
    out: List[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def loop_fields(steps: List[Step], start: Step) -> List[str]:
    """这个循环遍历的数据有哪些字段（供 {{loop.item.字段}} 使用）。

    循环内容写的是 {{A}}、而 A 是某个「读取数据」节点产出的 → 返回该节点
    配置的字段名；否则返回空（列表项是普通文本，只有 {{loop.item}} 本身）。
    """
    raw = (start.loop_expr or "").strip()
    m = VAR_PATTERN.fullmatch(raw)
    if not m:
        return []
    node = produced_variables(steps).get(m.group(1))
    if node is None:
        return []
    fields: List[str] = []
    for item in (node.data_cfg or {}).get("field_map") or []:
        if isinstance(item, dict):
            var = (item.get("var") or "").strip()
            if var and var not in fields:
                fields.append(var)
    return fields


def check_variables(steps: List[Step],
                    project_variables: Optional[dict] = None) -> List[str]:
    """运行前检查变量是否有来源，返回问题清单（空 = 没问题）。

    现在的变量只有三种来源：
    1. 项目变量（手工加的账号密码之类）；
    2. 「读取数据」节点产出的列表变量（循环用它遍历）；
    3. 循环体内自动有的 {{loop.item}} / {{loop.item.字段}} / {{loop.index}}。

    重点盯「跑起来才发现是空的」：循环外引用了 loop.*、字段名写错、
    变量根本没来源。
    """
    proj_vars = set(project_variables or ())
    produced = produced_variables(steps)

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

    # 先看「读取数据」节点自身配全了没有
    for name, node in produced.items():
        if not (node.data_cfg or {}).get("type") or not (node.data_cfg or {}).get("path"):
            add(node.id, f"「读取数据」节点还没选好文件 / 文件夹（它要产出 {{{{{name}}}}}）")

    for s in steps:
        if s.action == "read_data" and not (s.output_var or "").strip():
            add(s.id, "「读取数据」节点还没填产出变量名（双击节点填写，如 文章列表）")
        if s.action == "loop_start" and not (s.loop_expr or "").strip():
            add(s.id, "「循环」节点还没填循环内容（双击节点填写：数字＝跑几次，"
                      "或 {{变量}}＝按它的长度跑）")
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
                          "要么用「读取数据」节点产出它")
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
        # 显示编号：组合节点不占编号（显示成 2-4 这种范围），所以日志里不能直接用
        # step.id，否则跟画布上看到的数字对不上
        self._labels = {id(s): n for s, n
                        in zip(steps, blocks.step_numbers(steps)) if n}
        # 用户在小窗上点【暂停】时置位：执行器在每个步骤开始前停住等它清掉
        self._pause_requested = threading.Event()

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
        """启动浏览器并按块树执行步骤（循环 / 条件可互相嵌套）。"""
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
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context()
            self._page = context.new_page()
            # 页面弹窗一律「确定」：Playwright 默认是「取消」，
            # 于是「确定要发布/离开吗」被点成了取消，操作会静默失败
            self._page.on("dialog", self._on_dialog)
            try:
                self._run_nodes(nodes)
            finally:
                self.log("执行结束，关闭浏览器。")
                browser.close()
                self._page = None

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
            # 分支、组合：结构壳子，按顺序把里面的节点跑一遍就行
            self._run_nodes(block.nodes)

    def _run_condition(self, block: Block):
        """条件节点：先算出结果，再走第一个匹配的分支；都不匹配就整块跳过。"""
        step = block.start
        result, is_bool = self._condition_result(step)
        mode = "表达式" if (step.cond_mode or "equal") == "expr" else "变量"
        self.log(f"[条件] {mode} {step.cond_expr} → 结果「{result}」")
        for i, br in enumerate(block.branches):
            if self._branch_hit(step, i, result, is_bool):
                self.log(f"  走分支「{blocks.condition_branch_name(step, i)}」")
                self._run_nodes(br.nodes)
                return
        self.log("  没有分支匹配，跳过条件体。")

    def _condition_result(self, step: Step):
        """算出条件的结果，返回 (文本, 是否布尔值)；布尔值 None 表示按值匹配分支。"""
        raw = (step.cond_expr or "").strip()
        if not raw:
            raise ValueError("条件节点还没填判断内容，请点开条件节点填写")
        if (step.cond_mode or "equal") != "expr":
            return self._resolve_value(raw).strip(), None
        code = VAR_PATTERN.sub(
            lambda m: _literal(self.variables.get(m.group(1), "")), raw
        )
        scope = {"vars": self.variables, "log": self.log}
        try:
            value = eval(compile(code, "<条件>", "eval"), scope)   # noqa: S307
        except Exception as e:
            raise ValueError(f"条件表达式「{raw}」算不出来：{e}")
        if isinstance(value, bool):
            return ("是" if value else "否"), value
        return str(value).strip(), None

    def _branch_hit(self, step: Step, index: int, result: str,
                    is_bool: Optional[bool]) -> bool:
        """第 index 个分支是否命中。"""
        if is_bool is not None:
            # 表达式结果是真/假：真走第 1 个分支，假走第 2 个
            return index == (0 if is_bool else 1)
        values = [self._resolve_value(v).strip()
                  for v in blocks.condition_branch_values(step, index)]
        return result in values

    def _run_loop(self, block: Block):
        """按循环内容逐项执行循环体。

        循环表达式（循环节点里唯一的那个输入框）：
        - 数字 10        → 跑 10 次，loop.item = 当前索引（0 起）
        - {{变量}}       → 变量值是列表/多行文本就逐项遍历、是数字就跑那么多次
        - 其他文本       → 按行 / 逗号拆成多项
        每一轮注入 {{loop.item}}（当前项，对象会展开成 {{loop.item.字段}}）
        与 {{loop.index}}（第几轮，从 1 开始）。
        """
        start_step = block.start
        records, source_label = self._loop_records(start_step)
        if records is None:
            return

        base_vars = dict(self.variables)
        total = len(records)
        self.log(f"[循环开始] {source_label}，共 {total} 项")
        for i, rec in enumerate(records, start=1):
            if self._stop:
                self.log("已停止。")
                break
            self.log(f"──── 循环 {i}/{total} ────")
            self.variables = {**base_vars, **rec, "loop.index": str(i)}
            self._run_nodes(block.nodes)
        self.log(f"[循环结束] 完成 {total} 项")
        # 恢复循环外的变量；但保留脚本在循环里新造的变量（累加器之类）
        carried = {
            k: v for k, v in self.variables.items()
            if not k.startswith(LOOP_PREFIX) and k not in base_vars
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
            elif step.action == "win_activate":
                self._win_activate(step)
            elif step.action == "hotkey":
                self._hotkey(step)
            elif step.action == "delay":
                self._delay(step)
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
            elif step.action in ("loop_start", "loop_end", "condition_start",
                                 "condition_end", "branch"):
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
        """XPath → Playwright 定位器。"""
        return self._page.locator(f"xpath={locator.value}")

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
    # 自由代码节点（script）
    # ------------------------------
    def _script_scope(self, step: Step) -> Dict[str, str]:
        """按 script_vars 过滤传入脚本的变量；未指定则传全部。"""
        raw = (step.script_vars or "").replace("，", ",")
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names:
            return dict(self.variables)
        return {n: self.variables.get(n, "") for n in names}

    def _run_script(self, step: Step):
        """执行自由代码节点；脚本对 vars 的修改会写回流程变量。"""
        code = step.script_code or ""
        if not code.strip():
            self.log("  脚本为空，跳过。")
            return
        lang = (step.script_lang or "python").lower()
        scope = self._script_scope(step)
        timeout = max(1, int(step.script_timeout or 30))
        label = "JavaScript" if lang == "javascript" else "Python"
        self.log(f"  执行 {label} 脚本，传入变量 {len(scope)} 个")

        if lang == "javascript":
            updated, logs = self._exec_js(code, scope, timeout)
        else:
            updated, logs = self._exec_python(step, code, scope, timeout)

        for line in logs:
            self.log(f"  [脚本] {line}")

        changed = []
        for k, v in updated.items():
            text = "" if v is None else str(v)
            if self.variables.get(k) != text:
                changed.append(str(k))
            self.variables[str(k)] = text
        if changed:
            self.log(f"  脚本更新变量：{', '.join(sorted(changed))}")

    def _exec_python(self, step: Step, code: str, scope: Dict[str, str],
                     timeout: int):
        """进程内 exec。可用对象：vars（可修改）/ log / page / current_url / project_dir。

        注意：Python 脚本无法强制中断，超时只提示（请自行避免死循环）。
        """
        try:
            compiled = compile(code, f"<脚本节点{step.id}>", "exec")
        except SyntaxError as e:
            raise ValueError(f"Python 脚本语法错误（第 {e.lineno} 行）：{e.msg}")

        logs: List[str] = []
        ns: Dict[str, Any] = {
            "vars": dict(scope),
            "log": lambda m: logs.append(str(m)),
            "page": self._page,
            "current_url": self._current_url(),
            "project_dir": str(self.project_dir) if self.project_dir else "",
        }
        started = time.monotonic()
        exec(compiled, ns)          # noqa: S102 - 运行用户自己的本地脚本
        used = time.monotonic() - started
        if used > timeout:
            self.log(f"  提示：脚本耗时 {used:.1f}s，已超过设定 {timeout}s")

        out = dict(ns.get("vars") or {})
        # 脚本可写 result = {...} 直接输出一组新变量
        result = ns.get("result")
        if isinstance(result, dict):
            out.update({str(k): v for k, v in result.items()})
        return out, logs

    def _exec_js(self, code: str, scope: Dict[str, str], timeout: int):
        """在页面上下文执行 JS（可直接操作 DOM）。返回 (更新后的 vars, 日志)。"""
        if self._page is None:
            raise RuntimeError("JavaScript 节点需要浏览器页面，但浏览器未启动")
        wrapper = (
            "(arg) => {\n"
            "  const logs = [];\n"
            "  const log = (m) => logs.push(String(m));\n"
            "  const vars = arg.vars;\n"
            "  const url = arg.url;\n"
            "  let ret;\n"
            "  ret = (function(){\n"
            + code +
            "\n  }).call(null);\n"
            "  return { vars: vars, logs: logs, ret: ret };\n"
            "}"
        )
        old_timeout = DEFAULT_PAGE_TIMEOUT_MS
        self._page.set_default_timeout(timeout * 1000)
        try:
            res = self._page.evaluate(
                wrapper, {"vars": dict(scope), "url": self._current_url()}
            )
        except Exception as e:
            raise RuntimeError(f"JavaScript 脚本执行失败：{e}")
        finally:
            self._page.set_default_timeout(old_timeout)

        res = res or {}
        out = dict(res.get("vars") or {})
        logs = [str(x) for x in (res.get("logs") or [])]
        ret = res.get("ret")
        if isinstance(ret, dict):
            out.update({str(k): v for k, v in ret.items()})
        elif ret is not None:
            logs.append(f"返回值：{ret}")
        return out, logs

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

    def _navigate(self, step: Step):
        """打开网页。

        只等到 DOM 解析完成（domcontentloaded）：慢站点上图片、统计脚本之类
        会把 load 事件拖到几十秒，等它很容易一步就把整个流程卡死。
        页面「是否稳定」交给这一步的「步骤后等待」（页面加载完成 / 元素出现…），
        那些等待是轮询实现，超时也只记一条日志、不会中断流程。

        等多久由这一步的「打开超时」决定（默认 NAV_TIMEOUT_DEFAULT_S 秒）。
        """
        secs = int(step.nav_timeout or NAV_TIMEOUT_DEFAULT_S)
        try:
            self._page.goto(step.url, wait_until="domcontentloaded",
                            timeout=secs * 1000)
        except PlaywrightTimeout as e:
            raise TimeoutError(
                f"打开网页超过 {secs}s 还没响应：{step.url}\n"
                f"   当前页面：{self._current_url() or '（空白页）'}\n"
                f"   可以双击这一步，把「打开超时」调大（现在 {secs} 秒）。"
            ) from e

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
        self._resolve_xpath(step.locator).select_option(
            self._resolve_value(step.value), timeout=20000
        )

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
        url_ok = self._url_matches(step.resume_url) if step.resume_url else False
        elem_ok = self._element_present(step.resume_element) if step.resume_element else False

        if cond == "url_changed":
            if not step.resume_url:
                return False, "未配置 resume_url"
            return url_ok, f"URL 已包含 {step.resume_url}"
        if cond == "element_present":
            if not step.resume_element:
                return False, "未配置 resume_element"
            return elem_ok, f"元素已出现 {step.resume_element}"
        if cond == "url_and_element":
            if not (step.resume_url and step.resume_element):
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
        target = (step.wait_target or "").strip()
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
