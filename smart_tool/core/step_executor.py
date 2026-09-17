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
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.sync_api import (
    Page, TimeoutError as PlaywrightTimeout, sync_playwright,
)

from smart_tool.core import blocks, image_locator
from smart_tool.core.blocks import Block
from smart_tool.core.data_sources import (
    DataSourceConfig, DataSourceError, list_columns, load_rows,
)
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

# 暂停轮询间隔（秒）：兼顾响应速度与 CPU 占用
PAUSE_POLL_INTERVAL = 0.5
# 等待期间日志节流（秒），避免刷屏
PAUSE_LOG_INTERVAL = 10
# Playwright 页面默认超时（毫秒）。脚本节点会临时改小，用完恢复到这个值。
DEFAULT_PAGE_TIMEOUT_MS = 30000
# 打开网页的等待上限（毫秒）。站点慢的时候「load」事件迟迟不触发，
# 所以只等到 DOM 解析完成就算打开，页面是否稳定交给步骤里的「步骤后等待」。
NAV_TIMEOUT_MS = 60000
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

    对象（dict）会把键展开成 row.<键>，和表格/文件数据源的写法一致；
    标量则放进 {{loop.item}}。
    """
    if isinstance(item, dict):
        rec = {f"row.{k}": _to_text(v) for k, v in item.items()}
        rec["loop.item"] = _to_text(item)
        return rec
    return {"loop.item": _to_text(item)}


# {{变量}} 占位符
VAR_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
# 循环「索引范围」写法：一个整数或 {{变量}}，可写成「起-止」（首尾都算）
# 只写一项时它是「次数/长度」，索引从 0 开始
LOOP_RANGE_RE = re.compile(
    r"^\s*(\d+|\{\{[^{}]+\}\})\s*(?:-\s*(\d+|\{\{[^{}]+\}\}))?\s*$"
)
# 循环自动注入的变量
LOOP_VARS = ("loop.index", "loop.zero_index", "loop.item")
# 数据源产出的变量前缀
DATA_VAR_PREFIXES = ("row.", "file.")


def step_var_fields(step: Step) -> List[str]:
    """步骤里可能出现 {{变量}} 的文本字段。"""
    texts = [step.url, step.value, step.wait_target,
             step.resume_url, step.resume_element, step.prompt,
             step.loop_items, step.loop_range, step.cond_expr]
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


def check_variables(steps: List[Step], data_columns: Optional[List[str]] = None,
                    project_variables: Optional[dict] = None) -> List[str]:
    """运行前检查变量是否有来源，返回问题清单（空 = 没问题）。

    重点盯两种情况，避免"跑起来才发现没内容"：
    1. 循环体内引用了 row./file. 变量，但数据源压根不产出它
       （例如正文变量没配字段映射，运行时会填进占位说明文字）
    2. 引用的变量既不在数据源、也不在项目变量里
    """
    data_cols = set(data_columns or ())
    proj_vars = set(project_variables or ())

    # 分清三种循环：数据源循环里 row./file. 由数据源产出；
    # 列表循环里 row.* 来自列表项（对象），静态看不出有哪些字段，不误报；
    # 索引范围循环根本没有行数据，引用了 row./file. 要如实提示。
    # 条件块里的步骤跟随外层循环（缩进再深也一样）。
    data_loop_ids: set = set()
    list_loop_ids: set = set()
    range_loop_ids: set = set()
    try:
        tree = blocks.parse(steps)
    except blocks.StructureError:
        tree = []       # 结构问题由别处报错，这里不做重复提示

    def walk(nodes: List[Any]):
        for n in nodes:
            if not isinstance(n, Block):
                continue
            if n.kind == "loop":
                src = n.start.loop_source or "data"
                target = {"list": list_loop_ids,
                          "range": range_loop_ids}.get(src, data_loop_ids)
                target.update(s.id for s in n.steps)
            walk(n.nodes)

    walk(tree)

    problems: List[str] = []
    seen = set()

    def add(step_id: int, name: str, reason: str):
        key = (name, reason)
        if key in seen:
            return
        seen.add(key)
        problems.append(f"步骤 {step_id} 引用了 {{{{{name}}}}}：{reason}")

    for s in steps:
        for text in step_var_fields(s):
            for name in VAR_PATTERN.findall(text):
                if name in LOOP_VARS:
                    continue
                is_data_var = name.startswith(DATA_VAR_PREFIXES)
                if name in data_cols:
                    # 数据源字段是「逐行注入」的：只有在「数据源」循环里才有值。
                    # 如果这些步骤套在「索引范围 / 变量列表」循环里，运行时取不到，
                    # 却既不报错也不提示，最后填进空值——所以这里要如实提示。
                    if s.id in data_loop_ids or not (s.id in range_loop_ids
                                                     or s.id in list_loop_ids):
                        continue
                    add(s.id, name,
                        "这个循环不是「数据源」循环，取不到数据源的字段"
                        "（要按行取数据请把循环方式改成「数据源」）")
                    continue
                if is_data_var and s.id in list_loop_ids:
                    continue        # 来自列表项，交给运行时
                in_project = name in proj_vars
                if is_data_var and s.id in range_loop_ids:
                    reason = ("循环方式是「索引范围」，没有行数据"
                              "（要按行取数据请把循环方式改成「数据源」或「变量 / 手动列表」）")
                elif is_data_var and s.id in data_loop_ids:
                    reason = "数据源没有产出这个变量（可在【数据源…】里配置字段映射）"
                elif is_data_var:
                    reason = "数据源没有产出这个变量"
                elif not in_project:
                    reason = "找不到这个变量的来源（可加到【项目管理…】的变量里）"
                else:
                    continue
                add(s.id, name, reason)

    # 循环的「索引范围 / 循环项」在循环开始之前就要算出结果，
    # 那一刻数据源还没读、行变量还不存在（典型写法：索引范围填 {{file.total}}）。
    for s in steps:
        if s.action != blocks.LOOP_START:
            continue
        src = s.loop_source or "data"
        if src == "data":
            continue
        text = s.loop_range if src == "range" else s.loop_items
        for name in VAR_PATTERN.findall(text or ""):
            if name in data_cols and s.id not in data_loop_ids:
                add(s.id, name,
                    "循环开始前还没有值（它由数据源按行产出）；"
                    "要按文件逐个循环，请把循环方式改成「数据源」")
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
        data_source: Optional[dict] = None,
    ):
        """
        :param project_dir: 项目目录，用于解析 locator.value 中相对路径的截图
                            （如 img/step_03.png）。
        :param on_pause: 暂停开始回调，返回 PauseHandle（GUI 模式）；
                         返回 None 表示调用方不支持人工信号（CLI 模式），
                         此时仅靠 resume_condition 自动检测，manual 条件则等回车。
        :param on_resume: 暂停结束回调，参数为原因：
                          auto（信号自动检测）/manual（人工继续）/abort（人工终止）。
        :param data_source: 数据源配置 dict（loop_start/loop_end 之间按行循环）。
        """
        self.steps = steps
        self.variables = variables or {}
        self.headless = headless
        self.project_dir = Path(project_dir) if project_dir else None
        self.log = log
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.data_source = data_source or {}
        self._stop = False
        self._page: Optional[Page] = None

    # ------------------------------
    # 对外控制
    # ------------------------------
    def stop(self):
        """请求停止（线程安全，主线程调用）。"""
        self._stop = True
        self.log("收到停止请求，将在当前步骤完成后退出。")

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

    def _run_nodes(self, nodes: List[Any]):
        """顺序执行同一层里的节点（普通步骤或嵌套的块）。"""
        for node in nodes:
            if self._stop:
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
            self._run_nodes(block.nodes)      # 分支（正常只出现在条件里）

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
        """按「循环方式」逐项执行循环体。

        来源三种：
        - 数据源：文件/表格的每一行（产出 row.* / file.*）
        - 变量或手动列表：每项注入 loop.item；项是对象时，其键按 row.* 注入
        - 索引范围：按次数/索引循环，loop.item = 当前索引
        三者都会注入 loop.index（1 起）与 loop.zero_index；
        项变量只覆盖本次循环，静态变量（如项目变量里的手工测试值）作兜底。
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
            self.variables = {
                **base_vars, **rec,
                "loop.index": str(i),
                "loop.zero_index": str(i - 1),
            }
            self._run_nodes(block.nodes)
        self.log(f"[循环结束] 完成 {total} 项")
        # 恢复循环外的变量；但保留脚本在循环里新造的变量（累加器之类）
        row_keys = set()
        for rec in records:
            row_keys.update(rec.keys())
        carried = {
            k: v for k, v in self.variables.items()
            if k not in row_keys and k not in LOOP_VARS and k not in base_vars
        }
        self.variables = {**base_vars, **carried}

    def _loop_records(self, start_step: Step):
        """准备每一项的记录，返回 (记录列表, 来源说明)；None 表示没有可循环的数据。"""
        src = start_step.loop_source or "data"
        if src == "range":
            numbers = self._resolve_loop_range(start_step)
            if not numbers:
                self.log("[循环] 索引范围是空的，跳过循环体。")
                return None, ""
            label = (f"索引范围 {numbers[0]}-{numbers[-1]}"
                     if len(numbers) > 1 else f"索引范围 {numbers[0]}")
            return [{"loop.item": _to_text(n)} for n in numbers], label
        if src == "list":
            items = self._resolve_loop_items(start_step)
            if not items:
                self.log("[循环] 列表来源是空的，跳过循环体。")
                return None, ""
            return [_item_record(x) for x in items], "循环项列表"
        cfg = DataSourceConfig.from_dict(self.data_source)
        if not cfg.configured:
            raise DataSourceError(
                "存在「循环」节点，但既没配置数据源，也没填循环项。\n"
                "请在循环节点里把循环方式改成「变量 / 手动列表」或「索引范围」，"
                "或点【数据源】配置文件路径"
            )
        rows = load_rows(cfg)
        if not rows:
            self.log(f"[循环] 数据源 {Path(cfg.path).name} 没有数据行，跳过循环体。")
            return None, ""
        return rows, f"数据源：{Path(cfg.path).name}"

    def _resolve_loop_items(self, start_step: Step) -> List[Any]:
        """解析「循环项」输入框。

        - 整框只写了一个 {{变量}}：直接取该变量的值当列表
          （值可以是真列表、JSON 数组，或换行/逗号分隔的文本）
        - 否则按行拆成多项，行内的 {{变量}} 做文本替换
        """
        raw = (start_step.loop_items or "").strip()
        if not raw:
            return []
        m = VAR_PATTERN.fullmatch(raw)
        if m:
            name = m.group(1)
            if name in self.variables:
                items = _as_list(self.variables[name])
                if items is not None:
                    self.log(f"  循环来源变量 {{{{{name}}}}} → {len(items)} 项")
                    return items
        text = self._resolve_value(raw)
        return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]

    def _resolve_loop_range(self, start_step: Step) -> List[int]:
        """解析「索引范围」循环要跑的数字序列，返回 [起, ..., 止]。

        写法与含义（首尾都算）：
        - `10`          → 索引 0~9（共 10 次）
        - `0-10`        → 索引 0~10（共 11 次）
        - `5-10`        → 索引 5~10（共 6 次）
        - `{{变量}}`     → 按变量的「长度」跑：列表 / 多行文本按项数（3 项 → 0~2），
                          取值为整数则按该数（5 → 0~4）
        - `0-{{列表}}`   → 从 0 跑到列表最后一项的索引
        """
        raw = (start_step.loop_range or "").strip()
        if not raw:
            raise ValueError(
                "「循环」的循环方式选了「索引范围」，但没填范围。\n"
                "例如 10（跑 10 次）、0-10、5-10、{{次数}}、0-{{列表}}。"
            )
        m = LOOP_RANGE_RE.match(raw)
        if m is None:
            raise ValueError(
                f"索引范围「{raw}」格式不对：只能写数字、数字-数字，或 {{变量}}。\n"
                "例如 10、0-10、5-10、{{次数}}、0-{{列表}}。"
            )
        kind1, value1 = self._range_term(m.group(1), raw)
        if m.group(2) is None:
            # 只写一项：它是「次数 / 变量长度」，数字从 0 开始数
            if value1 <= 0:
                self.log(f"  索引范围 {raw} 算出来是 0 次，本次不执行循环体。")
                return []
            begin, end = 0, value1 - 1
        else:
            kind2, value2 = self._range_term(m.group(2), raw)
            begin = value1 if kind1 == "num" else 0
            end = value2 if kind2 == "num" else value2 - 1
        if begin > end:
            self.log(f"  索引范围 {begin}-{end}：起始索引大于结束索引，本次不执行循环体。")
            return []
        return list(range(begin, end + 1))

    def _range_term(self, term: str, raw: str) -> Tuple[str, int]:
        """解析索引范围里的一项 → (kind, 值)。

        kind="num"：当数字用（字面数字，或取值为整数的变量）；
        kind="len"：当变量长度用（列表 / JSON 数组 / 换行·逗号分隔文本的项数）。
        """
        term = term.strip()
        m = VAR_PATTERN.fullmatch(term)
        if m is None:
            return "num", int(term)
        name = m.group(1)
        if name not in self.variables:
            # 说清楚"为什么没有"和"该怎么办"：数据源字段是逐行注入的，
            # 循环还没开始当然取不到（典型写法：索引范围填 {{file.total}}）
            hint = ""
            cols = (list_columns(DataSourceConfig.from_dict(self.data_source))
                    if self.data_source else [])
            if name in cols:
                hint = ("\n它是数据源按行产出的字段（每行一个文件），"
                        "循环还没开始所以取不到。\n"
                        "要按文件逐个循环，请把循环方式改成「数据源」。")
            raise ValueError(
                f"索引范围「{raw}」引用了变量 {{{{{name}}}}}，但当前没有这个变量的值。{hint}"
            )
        value = self.variables[name]
        text = str(value).strip()
        if text.isdigit():
            return "num", int(text)
        items = _as_list(value)
        if not items:
            raise ValueError(
                f"索引范围「{raw}」里的变量 {{{{{name}}}}} 是空的，算不出长度。"
            )
        return "len", len(items)

    # ------------------------------
    # 步骤分发
    # ------------------------------
    def _execute_step(self, step: Step):
        self.log(f"[步骤 {step.id}] {step.action}")
        try:
            if step.action == "navigate":
                self._navigate(step)
            elif step.action == "click":
                self._click(step)
            elif step.action == "fill":
                self._fill(step)
            elif step.action == "select":
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
            # pause_for_human 的恢复信号本身就是验证条件，不再重复 wait_after
            if step.action != "pause_for_human":
                self._wait_after(step)
            # 这个步骤自己设的额外等待（秒）：慢站点、点了没反应时加大它
            if step.wait_seconds and step.wait_seconds > 0:
                self.log(f"  再固定等 {step.wait_seconds:g}s")
                time.sleep(float(step.wait_seconds))
        except Exception as e:
            self.log(f"  步骤出错: {e}")
            raise

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
    # 动作
    # ------------------------------
    def _navigate(self, step: Step):
        """打开网页。

        只等到 DOM 解析完成（domcontentloaded）：慢站点上图片、统计脚本之类
        会把 load 事件拖到几十秒，等它很容易一步就把整个流程卡死。
        页面「是否稳定」交给这一步的「步骤后等待」（页面加载完成 / 元素出现…），
        那些等待是轮询实现，超时也只记一条日志、不会中断流程。
        """
        try:
            self._page.goto(step.url, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout as e:
            raise TimeoutError(
                f"打开网页超过 {NAV_TIMEOUT_MS // 1000}s 还没响应：{step.url}\n"
                f"   当前页面：{self._current_url() or '（空白页）'}\n"
                "   站点慢的话，可以在这条【打开网页】上设「额外等待」，"
                "或调大 step_executor.py 里的 NAV_TIMEOUT_MS。"
            ) from e

    def _click(self, step: Step):
        if not step.locator:
            raise ValueError("click 步骤缺少 locator")
        if step.locator.type == "image":
            m = self._locate_by_image(step.locator)
            self._page.mouse.click(m.x, m.y)
            return
        loc = self._resolve_xpath(step.locator)
        try:
            loc.click(timeout=CLICK_TIMEOUT_MS)
        except PlaywrightTimeout as e:
            raise TimeoutError(
                f"等不到可点击的元素（{CLICK_TIMEOUT_MS // 1000}s）：{step.locator.value}\n"
                f"   当前页面：{self._current_url() or '（正在跳转中）'}\n"
                "   多半是上一步之后没跳到你以为的页面，或这个 XPath 属于另一个页面。"
            ) from e

    def _fill(self, step: Step):
        if not step.locator:
            raise ValueError("fill 步骤缺少 locator")
        text = self._resolve_value(step.value)
        if step.locator.type == "image":
            # 截图定位没有 DOM 句柄：点击聚焦 → 全选清空 → 插入文本。
            # insertText 直接走输入法通道，中文等非 ASCII 字符也能可靠写入。
            m = self._locate_by_image(step.locator)
            self._page.mouse.click(m.x, m.y)
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Delete")
            self._page.keyboard.insert_text(text)
        else:
            loc = self._resolve_xpath(step.locator)
            self._ensure_fillable(loc)
            # 先点一下元素中心拿焦点，再填入：
            # 富文本框、需要激活才可写的框，直接 fill 会填不进去。
            self._focus_by_click(loc)
            loc.fill(text, timeout=20000)
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
            loc.click(timeout=FOCUS_CLICK_TIMEOUT_MS)
            self.log("  已点击元素中心获取焦点")
            return True
        except Exception as e:
            first = str(e).strip().splitlines()[0][:120] if str(e).strip() else e
            self.log(f"  点击获取焦点没成功（改为直接填入）：{first}")
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
                    f"暂停等待人工操作超时（{timeout}s），步骤 {step.id} 未满足恢复条件："
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
