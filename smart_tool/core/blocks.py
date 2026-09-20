# -*- coding: utf-8 -*-
"""步骤结构解析：把线性的 steps 列表解析成「块树」，执行与界面共用同一套规则。

容器（块）有三种：
- 循环   `loop_start` … `loop_end`            配置存在「循环开始」节点上
- 条件   `condition_start` … `condition_end`  条件节点只提供「判断的数据」，
         块里**直属的动作节点**各自带一条规则（判断方式 + 值）：
         从上往下第一个成立的执行，所以节点的先后就是优先级；
         判断方式留空＝兜底（相当于 else）。一个动作想要多步，就把它收成「组合」。
- 组合   `group_start` … `group_end`          把连着的一串步骤收成一个、起个名字，
         画布上只显示一张卡片；名字存在「组合开始」节点的 title 上

块可以互相嵌套（条件里放循环、循环里放条件、组合里放条件……）。

本模块只做结构，不依赖 PyQt；执行器用 `parse()` 得到块树，界面用 `spans()` 得到
「哪个块占了哪几行」以及每行该缩进几级。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

from smart_tool.core.project_store import Step

LOOP_START, LOOP_END = "loop_start", "loop_end"
COND_START, COND_END = "condition_start", "condition_end"
GROUP_START, GROUP_END = "group_start", "group_end"

# 容器：开始标记 → (块类型, 结束标记)
CONTAINERS = {
    LOOP_START: ("loop", LOOP_END),
    COND_START: ("condition", COND_END),
    GROUP_START: ("group", GROUP_END),
}
# 结束标记 → 块类型
END_KINDS = {LOOP_END: "loop", COND_END: "condition", GROUP_END: "group"}
# 所有结构标记（不是真正的动作步骤）
MARKERS = (LOOP_START, LOOP_END, COND_START, COND_END, GROUP_START, GROUP_END)
# 成对标记的「结束端」：画布上不画卡片（流程编辑里能看到）
END_MARKERS = tuple(END_KINDS)
#: 不占「顺序编号」的结构标记：结束端（循环/条件/组合结束）只是收尾，组合开始也只是壳
NO_NUMBER = (GROUP_START,) + END_MARKERS
# 可以套虚线框、能整块拖动的块类型（组合不套框：它本身就是一张卡片）
REGION_KINDS = ("loop", "condition")

ACTION_CN = {
    LOOP_START: "循环开始", LOOP_END: "循环结束",
    COND_START: "条件", COND_END: "条件结束",
    GROUP_START: "组合", GROUP_END: "组合结束",
}

#: 组合节点没起名时显示的占位名字
DEFAULT_GROUP_NAME = "组合"


class StructureError(ValueError):
    """步骤结构不合法（标记不配对、嵌套放错位置等）。"""


@dataclass
class Block:
    """一个块（循环 / 条件 / 组合）。"""
    kind: str                                  # loop | condition | group
    start: Step                                # 起始标记
    end: Optional[Step] = None                 # 结束标记
    nodes: List["Node"] = field(default_factory=list)
    start_idx: int = -1                        # 起始标记在 steps 里的下标
    end_idx: int = -1                          # 结束标记下标

    @property
    def steps(self) -> List[Step]:
        """块内所有步骤（含嵌套块，按出现顺序）。"""
        out: List[Step] = []
        for n in self.nodes:
            if isinstance(n, Block):
                out.append(n.start)
                out.extend(n.steps)
                if n.end is not None:
                    out.append(n.end)
            else:
                out.append(n)
        return out


Node = Union[Step, Block]


# ------------------------------
# 解析成块树
# ------------------------------
def parse(steps: List[Step]) -> List[Node]:
    """解析成块树；结构不合法抛 StructureError。"""
    nodes, i = _parse_nodes(steps, 0, ())
    if i < len(steps):
        s = steps[i]
        raise StructureError(
            f"步骤 {s.id} 的「{ACTION_CN.get(s.action, s.action)}」没有对应的开始标记"
        )
    return nodes


def validate(steps: List[Step]) -> Optional[str]:
    """结构检查：没问题返回 None，否则返回中文错误信息。"""
    try:
        parse(steps)
    except StructureError as e:
        return str(e)
    return None


def _parse_nodes(steps: List[Step], i: int,
                 stops: Tuple[str, ...]) -> Tuple[List[Node], int]:
    """从 i 开始解析，遇到 stops 里的标记或列表结束就停下。"""
    nodes: List[Node] = []
    while i < len(steps):
        s = steps[i]
        a = s.action
        if a in stops:
            return nodes, i
        if a in CONTAINERS:
            kind, end_marker = CONTAINERS[a]
            inner, j = _parse_nodes(steps, i + 1, (end_marker,))
            if j >= len(steps):
                raise StructureError(
                    f"步骤 {s.id} 的「{ACTION_CN[a]}」缺少配对的"
                    f"「{ACTION_CN[end_marker]}」"
                )
            nodes.append(Block(kind, s, steps[j], inner, i, j))
            i = j + 1
        elif a in END_KINDS:
            raise StructureError(
                f"步骤 {s.id} 的「{ACTION_CN.get(a, a)}」没有对应的开始标记"
                "（是不是被挪到外面了？）"
            )
        else:
            nodes.append(s)
            i += 1
    return nodes, i


# ------------------------------
# 平坦视角：每个块占了哪几行
# ------------------------------
@dataclass
class Span:
    """平坦 steps 列表里的一个块范围。"""
    kind: str            # loop | condition | group
    start: int           # 起始标记下标
    end: int             # 结束标记下标
    depth: int           # 嵌套层级，最外层 0
    inner_lo: int = 0    # 内部范围（左闭右开）：缩进、拆分单元用
    inner_hi: int = 0

    def contains(self, index: int) -> bool:
        """index 是否落在这个块里（含首尾标记）。"""
        return self.start <= index <= self.end

    @property
    def insert_pos(self) -> int:
        """往这个块末尾插步骤的位置（插到结束标记之前）。"""
        return self.end


def spans(steps: List[Step]) -> List[Span]:
    """所有块的范围；结构不合法时返回空列表（界面照样能显示，只是不高亮）。"""
    try:
        tree = parse(steps)
    except StructureError:
        return []
    out: List[Span] = []
    _collect(tree, 0, out)
    return out


def _collect(nodes: List[Node], depth: int, out: List[Span]):
    for n in nodes:
        if not isinstance(n, Block):
            continue
        out.append(Span(n.kind, n.start_idx, n.end_idx, depth,
                        n.start_idx + 1, n.end_idx))
        _collect(n.nodes, depth + 1, out)


def direct_children(all_spans: List[Span], sp: Span) -> List[int]:
    """块 sp 的**直属**子节点行号（嵌套块整个算作一个节点）。"""
    nested = {x.start: x for x in all_spans if x.depth == sp.depth + 1}
    out: List[int] = []
    i = sp.inner_lo
    while i < sp.inner_hi:
        inner = nested.get(i)
        out.append(i)
        i = (inner.end + 1) if inner is not None else i + 1
    return out


def rule_owner_span(steps: List[Step], index: int) -> Optional[Span]:
    """index 作为「条件里的一个动作节点」时返回那个条件块，否则 None。

    只认直属子节点：嵌在循环 / 组合 / 更里层条件里的节点不带自己的判断方式。
    """
    all_spans = spans(steps)
    for sp in all_spans:
        if sp.kind == "condition" and index in direct_children(all_spans, sp):
            return sp
    return None


def span_by_marker(all_spans: List[Span], index: int) -> Optional[Span]:
    """index 正好是某个块的起始/结束标记 → 返回那个块。"""
    for sp in all_spans:
        if index == sp.start or index == sp.end:
            return sp
    return None


def enclosing_span(all_spans: List[Span], index: int,
                   kinds: Optional[Tuple[str, ...]] = None) -> Optional[Span]:
    """包含 index 的最内层块（kinds 可限定只看某几类）。"""
    best: Optional[Span] = None
    for sp in all_spans:
        if kinds and sp.kind not in kinds:
            continue
        if sp.contains(index) and (best is None or sp.depth > best.depth):
            best = sp
    return best


def marker_owner_index(steps: List[Step], index: int) -> int:
    """标记节点对应的「配置节点」下标，普通步骤返回它自己。

    循环结束 → 循环开始；条件结束 → 条件。
    （设置只有一份，点哪一端都是编辑同一个块）
    """
    sp = span_by_marker(spans(steps), index)
    return index if sp is None else sp.start


def move_bounds(steps: List[Step], index: int) -> Tuple[int, int]:
    """该步骤允许上下移动到的下标范围（不许跨出自己所在的块）。"""
    sp = enclosing_span(spans(steps), index)
    if sp is None:
        return 0, len(steps) - 1
    return sp.inner_lo, sp.inner_hi - 1


def depths(steps: List[Step]) -> List[int]:
    """每个步骤该缩进几级（界面列表用）。

    容器的开始/结束标记与它所在层对齐；容器内部的步骤缩进一层。
    """
    result = [0] * len(steps)
    for sp in spans(steps):
        result[sp.start] = sp.depth
        result[sp.end] = sp.depth
        for k in range(sp.inner_lo, sp.inner_hi):
            result[k] = max(result[k], sp.depth + 1)
    return result


def descendant_ids(block: Block) -> List[int]:
    """块内所有步骤的 id（含嵌套块）。"""
    return [s.id for s in block.steps]


# ------------------------------
# 组合（把连着的一串步骤收成一个节点）
# ------------------------------
def group_spans(all_spans: List[Span]) -> List[Span]:
    """所有组合块的范围。"""
    return [sp for sp in all_spans if sp.kind == "group"]


def card_hidden_indices(steps: List[Step]) -> set:
    """画布上**不画卡片**的行号。

    两类：成对标记的结束端（循环结束 / 条件结束 / 组合结束），
    以及组合体内部的所有行——组合在画布上只留「组合开始」那一张卡片。
    """
    hidden = {i for i, s in enumerate(steps) if s.action in END_MARKERS}
    for sp in spans(steps):
        if sp.kind != "group":
            continue
        hidden.update(range(sp.inner_lo, sp.end + 1))
    return hidden


def inner_count(sp: Span) -> int:
    """块里有几个步骤（写「已收起 N 个步骤」用）。"""
    return max(0, sp.inner_hi - sp.inner_lo)


def step_numbers(steps: List[Step]) -> List[str]:
    """每个步骤对外显示的编号（列表，和 steps 一一对应；空字符串＝不显示编号）。

    **结构标记的结束端不占编号**（循环结束 / 条件结束 / 组合结束）：它们只是块的
    收尾，不参与列表上「1、2、3…」的顺序编号，后面的节点接着往下数。
    组合本身也只是它里面那几步的「壳」，所以显示成范围（如 `2-4`）。

    注意：这里只管「显示出来的编号」。`Step.id` 是每一行的身份（画布、选中、
    改某一步都靠它），必须唯一且随行号走，不能跳号。
    """
    labels = [""] * len(steps)
    n = 0
    for i, s in enumerate(steps):
        if s.action in NO_NUMBER:
            continue
        n += 1
        labels[i] = str(n)
    for sp in group_spans(spans(steps)):
        inner = [labels[k] for k in range(sp.inner_lo, sp.inner_hi) if labels[k]]
        if not inner:
            continue        # 空组合不写编号，免得跟后面那个节点撞号
        labels[sp.start] = (inner[0] if inner[0] == inner[-1]
                            else f"{inner[0]}-{inner[-1]}")
    return labels


def number_of(steps: List[Step], index: int) -> str:
    """第 index 个步骤的显示编号（越界返回空串）。"""
    if not (0 <= index < len(steps)):
        return ""
    return step_numbers(steps)[index]


def last_number(steps: List[Step]) -> str:
    """显示编号里最大的那个数字（写「编号 1~N」用；组合的范围、结束标记都不参与）。"""
    return next((n for n in reversed(step_numbers(steps)) if n.isdigit()), "")


def can_group(steps: List[Step], lo: int, hi: int) -> Optional[str]:
    """[lo, hi] 这几行能不能合成一个组合：能返回 None，否则返回中文原因。"""
    if lo > hi:
        return "请先选中要合并的节点。"
    if hi - lo < 1:
        return "至少要选中两个节点才能合并。"
    problem = validate(steps)
    if problem:
        return f"当前步骤结构不完整（{problem}），先修好循环 / 条件的配对标记再合并。"
    all_spans = spans(steps)
    if any(s.action in (GROUP_START, GROUP_END) for s in steps[lo:hi + 1]):
        return ("选中的范围里已经有组合节点了：\n"
                "请先对它【取消组合】，或者只选组合外面的节点。")
    for sp in all_spans:
        overlap = sp.start <= hi and sp.end >= lo
        inside = lo <= sp.start and sp.end <= hi      # 选中的范围把整个块包住
        nested = sp.start <= lo and hi <= sp.end      # 选中的范围整个缩在块里面
        # 两种都不算「切开」：整个包住＝块被收进去；缩在里面＝新组合嵌在这个块里
        # （循环里、条件里都能再合并出一个组合）
        if overlap and not (inside or nested):
            name = {"loop": "循环", "condition": "条件",
                    "group": "组合"}.get(sp.kind, sp.kind)
            return (f"选中的范围把一个「{name}」切成了两半：\n"
                    "要么把整个块一起选上，要么只选它里面的步骤。")
    return None


def make_group(steps: List[Step], lo: int, hi: int, name: str) -> str:
    """把 [lo, hi] 这几行包成一个组合，返回最终用的名字。

    两个标记就地插进去：`group_start`（带名字）在 lo 前面、`group_end` 在 hi 后面。
    坐标继承第一 / 最后一个被包住的步骤，这样画布上不会因为「缺位置」而整片重排。
    """
    name = (name or "").strip() or DEFAULT_GROUP_NAME
    first_pos = list(steps[lo].pos) if steps[lo].pos else None
    last_pos = list(steps[hi].pos) if steps[hi].pos else None
    start = Step(id=0, action=GROUP_START, title=name, pos=first_pos)
    end = Step(id=0, action=GROUP_END, pos=last_pos)
    steps[lo:lo] = [start]
    steps.insert(hi + 2, end)       # 前面插了一个，原来的 hi 往后挪了一位
    return name


def ungroup(steps: List[Step], index: int) -> Optional[str]:
    """拆掉 index 处的组合：只去掉两个标记，里面的步骤原样留着。

    index 可以是组合的开始或结束标记（点哪一端都行）。返回组合原来的名字。
    """
    sp = span_by_marker(spans(steps), index)
    if sp is None or sp.kind != "group":
        return None
    name = steps[sp.start].title
    gpos = steps[sp.start].pos
    inner = list(range(sp.inner_lo, sp.inner_hi))
    if gpos and inner:
        if all(steps[k].pos == gpos for k in inner):
            # 里面几步的坐标跟组合卡片完全重合＝排版时留下的占位，
            # 直接清掉，让画布重新给它们找空位（不然拆开后几张卡片叠成一摞）
            for k in inner:
                steps[k].pos = None
        elif not steps[inner[0]].pos:
            # 组合被拖动过：坐标交给里面第一个步骤，画布上位置看着不变
            steps[inner[0]].pos = list(gpos)
    del steps[sp.end]
    del steps[sp.start]
    return name


# ------------------------------
# 条件里动作节点的「规则」（判断方式 + 值）
# ------------------------------
#: 判断方式（key, 界面上的中文名）。
#: 空串＝兜底：不判断、无条件成立（相当于 else），必须放在最后。
COND_OPS = [
    ("", "兜底（上面都不成立时走这里）"),
    ("contains", "包含"),
    ("not_contains", "不包含"),
    ("eq", "等于"),
    ("ne", "不等于"),
    ("gt", "大于"),
    ("lt", "小于"),
    ("ge", "大于等于"),
    ("le", "小于等于"),
]
COND_OP_CN = dict(COND_OPS)
#: 这几个判断方式的「值」支持逗号分隔多个：命中任意一个就算成立
COND_MULTI_OPS = ("contains", "not_contains", "eq", "ne")
#: 需要按数字比较的判断方式
COND_NUMBER_OPS = ("gt", "lt", "ge", "le")


def rule_op(step: Step) -> str:
    """这个动作节点的判断方式（空串＝兜底）。"""
    return str(getattr(step, "cond_op", "") or "").strip()


def rule_value(step: Step) -> str:
    """这个动作节点要比较的值（原文，可含 {{变量}}）。"""
    return str(getattr(step, "cond_value", "") or "")


def rule_values(step: Step) -> List[str]:
    """值清单（逗号分隔，已去空）——表达式模式下用它做「按值匹配」。"""
    return [x.strip() for x in rule_value(step).replace("，", ",").split(",")
            if x.strip()]


def rule_summary(step: Step, index: int, mode: str = "rule") -> str:
    """动作节点在流程编辑 / 画布上的规则摘要（没填规则时返回空串）。"""
    if mode == "expr":
        values = "、".join(rule_values(step))
        if values:
            return f"匹配 {values}"
        if index == 0:
            return "真"
        if index == 1:
            return "假"
        return ""
    op = rule_op(step)
    if not op:
        return "兜底"
    value = rule_value(step).strip()
    return f"{COND_OP_CN.get(op, op)} {value}".strip()


def rule_mode_at(steps: List[Step], pos: int) -> Optional[str]:
    """在 pos 这个位置插入节点时，它会不会成为某个「条件」的**直属**动作节点。

    是的话返回那个条件的判断方式（rule / expr），否则 None —— 两个界面用它决定
    「新建节点」的对话框要不要显示「条件判断」那一栏。

    注意要排除「落在条件里、但嵌在更里层块里」的位置（比如条件里的循环体内部）：
    那些节点没有自己的判断方式，规则挂在循环 / 组合那个块本身上。
    """
    all_spans = spans(steps)
    for sp in all_spans:
        if sp.kind != "condition" or not (sp.inner_lo <= pos <= sp.end):
            continue
        nested = any(
            x is not sp and sp.start < x.start and x.end < sp.end
            and x.start < pos <= x.end
            for x in all_spans
        )
        if nested:
            continue        # 落在这个条件的某个嵌套块里面，不是它的直属节点
        return steps[sp.start].cond_mode or "rule"
    return None


def apply_default_rule(steps: List[Step], index: int) -> None:
    """新插进来的节点如果正好落在「条件」里，给它补一条默认规则。

    默认给「包含」（值留空＝暂时不成立），等用户去填。**不能默认成兜底**：
    兜底无条件成立，摆在前面的兜底会把后面的动作全挡住。
    表达式模式不用判断方式，这里不碰。
    """
    if not (0 <= index < len(steps)):
        return
    step = steps[index]
    # 结束端标记不可能是条件里的动作节点；别的（含循环/组合/条件的开始标记，
    # 它们整个块算一个动作节点）都该拿到默认规则
    if step.action in END_MARKERS or step.cond_op or step.cond_value:
        return
    owner = rule_owner_span(steps, index)
    if owner is None:
        return
    if (steps[owner.start].cond_mode or "rule") == "rule":
        step.cond_op = "contains"


# ------------------------------
# 循环：次数 / 列表 还是 条件（while）
# ------------------------------
#: 循环方式（key, 界面上的中文名）
LOOP_MODES = [
    ("each", "次数 / 列表（先算好一份清单，挨个跑完就结束）"),
    ("cond", "条件（每轮先判断，判断不出次数——while）"),
]
#: 条件模式里「判断什么」（key, 界面上的中文名）
LOOP_COND_KINDS = [
    ("element", "网页上有这个元素（填 XPath）"),
    ("image", "屏幕上找到这张图（选项目里的图片）"),
    ("var", "变量满足条件（变量 + 判断方式 + 值）"),
    ("expr", "自定义表达式（Python，能用 元素存在(...) / 图片存在(...)）"),
]
LOOP_COND_CN = dict(LOOP_COND_KINDS)


def loop_summary(step: Step) -> str:
    """循环节点在流程列表 / 画布上的一句话摘要。"""
    if (step.loop_mode or "each") != "cond":
        expr = (step.loop_expr or "").strip()
        return f"遍历 {expr}" if expr else "（还没填循环内容）"

    kind = step.loop_cond_kind or "element"
    arg = (step.loop_cond_arg or "").strip()
    if kind == "var":
        op = COND_OP_CN.get((step.loop_cond_op or "").strip(), "")
        what = f"{arg} {op} {step.loop_cond_value or ''}".strip()
    elif kind == "image":
        what = arg.replace("\\", "/").rsplit("/", 1)[-1] or "（还没选图片）"
    else:
        what = arg or "（还没填）"
    if len(what) > 40:
        what = what[:39] + "…"
    return f"一直等到：{what}" if step.loop_cond_stop else f"只要「{what}」就一直跑"
