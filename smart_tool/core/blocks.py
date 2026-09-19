# -*- coding: utf-8 -*-
"""步骤结构解析：把线性的 steps 列表解析成「块树」，执行与界面共用同一套规则。

容器（块）有四种：
- 循环   `loop_start` … `loop_end`            配置存在「循环开始」节点上
- 条件   `condition_start` … `condition_end`  配置存在「条件」节点上（判断方式 + 分支清单）
- 分支   `branch` 是条件块内部的段落标记：从它到下一个 `branch`（或条件结束）
         之间的步骤属于这个分支
- 组合   `group_start` … `group_end`          把连着的一串步骤收成一个、起个名字，
         画布上只显示一张卡片；名字存在「组合开始」节点的 title 上

块可以互相嵌套（分支里放循环、循环里放条件……）。

本模块只做结构，不依赖 PyQt；执行器用 `parse()` 得到块树，界面用 `spans()` 得到
「哪个块占了哪几行」以及每行该缩进几级。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from smart_tool.core.project_store import Step

LOOP_START, LOOP_END = "loop_start", "loop_end"
COND_START, COND_END = "condition_start", "condition_end"
BRANCH = "branch"
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
MARKERS = (LOOP_START, LOOP_END, COND_START, COND_END, BRANCH,
           GROUP_START, GROUP_END)
# 成对标记的「结束端」：画布上不画卡片（流程编辑里能看到）
END_MARKERS = tuple(END_KINDS)
#: 不占「顺序编号」的结构标记：结束端（循环/条件/组合结束）只是收尾，组合开始也只是壳
NO_NUMBER = (GROUP_START,) + END_MARKERS
# 可以套虚线框、能整块拖动的块类型（组合不套框：它本身就是一张卡片）
REGION_KINDS = ("loop", "condition")

ACTION_CN = {
    LOOP_START: "循环开始", LOOP_END: "循环结束",
    COND_START: "条件", COND_END: "条件结束", BRANCH: "分支",
    GROUP_START: "组合", GROUP_END: "组合结束",
}

#: 组合节点没起名时显示的占位名字
DEFAULT_GROUP_NAME = "组合"


class StructureError(ValueError):
    """步骤结构不合法（标记不配对、嵌套放错位置等）。"""


@dataclass
class Block:
    """一个块（循环 / 条件 / 分支）。"""
    kind: str                                  # loop | condition | branch
    start: Step                                # 起始标记
    end: Optional[Step] = None                 # 结束标记（分支没有）
    nodes: List["Node"] = field(default_factory=list)
    start_idx: int = -1                        # 起始标记在 steps 里的下标
    end_idx: int = -1                          # 结束标记下标；分支＝内部最后一个步骤下标

    @property
    def branches(self) -> List["Block"]:
        """条件块内的分支（按顺序）。"""
        return [n for n in self.nodes if isinstance(n, Block) and n.kind == "branch"]

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
        if a == LOOP_START:
            inner, j = _parse_nodes(steps, i + 1, (LOOP_END,))
            if j >= len(steps):
                raise StructureError(
                    f"步骤 {s.id} 的「循环开始」缺少配对的「循环结束」"
                )
            nodes.append(Block("loop", s, steps[j], inner, i, j))
            i = j + 1
        elif a == GROUP_START:
            inner, j = _parse_nodes(steps, i + 1, (GROUP_END,))
            if j >= len(steps):
                raise StructureError(
                    f"步骤 {s.id} 的「组合」缺少配对的「组合结束」"
                )
            nodes.append(Block("group", s, steps[j], inner, i, j))
            i = j + 1
        elif a == COND_START:
            block, j = _parse_condition(steps, i)
            nodes.append(block)
            i = j + 1
        elif a in (LOOP_END, COND_END, BRANCH, GROUP_END):
            raise StructureError(
                f"步骤 {s.id} 的「{ACTION_CN.get(a, a)}」没有对应的开始标记"
                "（是不是被挪到外面了？）"
            )
        else:
            nodes.append(s)
            i += 1
    return nodes, i


def _parse_condition(steps: List[Step],
                     i: int) -> Tuple[Block, int]:
    """解析一个条件块：内部按「分支」标记切成若干分支。"""
    start = steps[i]
    branches: List[Block] = []
    pending: Optional[int] = None          # 当前分支标记的下标
    j = i + 1
    while True:
        inner, j = _parse_nodes(steps, j, (COND_END, BRANCH))
        if pending is None:
            if inner:
                raise StructureError(
                    f"步骤 {start.id} 的条件里，「分支」之前不能放步骤"
                    "（请在前面先加一个分支）"
                )
        else:
            branches.append(Block("branch", steps[pending], None, inner,
                                  pending, j - 1))
        if j >= len(steps):
            raise StructureError(
                f"步骤 {start.id} 的「条件」缺少配对的「条件结束」"
            )
        if steps[j].action == COND_END:
            return Block("condition", start, steps[j], branches, i, j), j
        pending = j                        # 下一个分支标记
        j += 1


# ------------------------------
# 平坦视角：每个块占了哪几行
# ------------------------------
@dataclass
class Span:
    """平坦 steps 列表里的一个块范围。"""
    kind: str            # loop | condition | branch
    start: int           # 起始标记下标
    end: int             # 结束标记下标；分支＝内部最后一个步骤下标（空分支＝起始标记下标）
    depth: int           # 嵌套层级，最外层 0
    inner_lo: int = 0    # 内部范围（左闭右开）：缩进、拆分单元用
    inner_hi: int = 0

    def contains(self, index: int) -> bool:
        """index 是否落在这个块里（含首尾标记）。"""
        return self.start <= index <= self.end

    @property
    def is_branch(self) -> bool:
        return self.kind == "branch"

    @property
    def insert_pos(self) -> int:
        """往这个块末尾插步骤的位置（插到结束标记之前）。"""
        return self.end + 1 if self.is_branch else self.end


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
        if n.kind == "branch":
            inner_lo, inner_hi = n.start_idx + 1, n.end_idx + 1
        else:
            inner_lo, inner_hi = n.start_idx + 1, n.end_idx
        out.append(Span(n.kind, n.start_idx, n.end_idx, depth,
                        inner_lo, inner_hi))
        _collect(n.nodes, depth + 1, out)


def span_by_marker(all_spans: List[Span], index: int) -> Optional[Span]:
    """index 正好是某个块的起始/结束标记 → 返回那个块。"""
    for sp in all_spans:
        if index == sp.start:
            return sp
        if not sp.is_branch and index == sp.end:
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

    循环结束 → 循环开始；条件结束 / 分支 → 条件。
    （设置只有一份，点哪一端都是编辑同一个块）
    """
    all_spans = spans(steps)
    sp = span_by_marker(all_spans, index)
    if sp is None:
        return index
    if sp.kind == "branch":
        cond = enclosing_span(all_spans, sp.start, ("condition",))
        return cond.start if cond is not None else index
    return sp.start


def move_bounds(steps: List[Step], index: int) -> Tuple[int, int]:
    """该步骤允许上下移动到的下标范围（不许跨出自己所在的块）。"""
    sp = enclosing_span(spans(steps), index)
    if sp is None:
        return 0, len(steps) - 1
    return sp.inner_lo, sp.inner_hi - 1


def depths(steps: List[Step]) -> List[int]:
    """每个步骤该缩进几级（界面列表用）。

    容器的开始/结束标记与它所在层对齐；容器内部的步骤缩进一层；
    条件里的「分支」标记也缩进一层（它属于条件内部）。
    """
    result = [0] * len(steps)
    for sp in spans(steps):
        result[sp.start] = sp.depth
        if not sp.is_branch:
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
        # （循环里、条件分支里都能再合并出一个组合）
        if overlap and not (inside or nested):
            name = {"loop": "循环", "condition": "条件",
                    "branch": "分支", "group": "组合"}.get(sp.kind, sp.kind)
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


def condition_branch_values(step: Step, index: int) -> List[str]:
    """条件节点第 index 个分支的匹配值清单（逗号分隔，已去空）。"""
    if index < 0 or index >= len(step.cond_branches or []):
        return []
    raw = (step.cond_branches[index].get("values") or "").replace("，", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def condition_branch_name(step: Step, index: int) -> str:
    """条件节点第 index 个分支的显示名。"""
    if index < 0 or index >= len(step.cond_branches or []):
        return f"分支 {index + 1}"
    name = (step.cond_branches[index].get("name") or "").strip()
    return name or f"分支 {index + 1}"


def new_branch(name: str = "", values: str = "") -> Dict[str, str]:
    """新建一条分支定义（界面用）。"""
    return {"name": name, "values": values}


# ------------------------------
# 条件分支清单与分支标记的同步
# ------------------------------
def normalize_branch_lists(steps: List[Step]) -> bool:
    """让每个条件节点的「分支清单」条数＝它的分支标记个数（多了截断、少了补空）。

    单一数据源：分支个数由结构（分支标记）决定，清单只负责存名字与匹配值。
    返回是否改动过。
    """
    changed = False
    for sp in spans(steps):
        if sp.kind != "condition":
            continue
        cond = steps[sp.start]
        count = sum(1 for k in range(sp.inner_lo, sp.inner_hi)
                    if steps[k].action == BRANCH)
        items = [dict(m) for m in (cond.cond_branches or [])]
        if len(items) == count:
            continue
        if len(items) > count:
            items = items[:count]
        else:
            items += [new_branch() for _ in range(count - len(items))]
        cond.cond_branches = items
        changed = True
    return changed


def apply_condition_edit(steps: List[Step], cond_index: int,
                         new_step: Step) -> str:
    """把条件节点的改动落到步骤结构上（原地改 steps），返回提示信息。

    - `new_step.cond_dropped`（界面记下的、被删掉的分支行号）→ 删掉对应的分支
      标记连同分支里的步骤；
    - 剩下的按「分支清单条数＝分支标记个数」补齐或截断。
    """
    dropped = sorted({i for i in (getattr(new_step, "cond_dropped", []) or [])},
                     reverse=True)
    notes: List[str] = []
    for i in dropped:
        sp = _span_at(steps, cond_index, "condition")
        if sp is None:
            break
        marks = [k for k in range(sp.inner_lo, sp.inner_hi)
                 if steps[k].action == BRANCH]
        if i >= len(marks):
            continue          # 行号早就变了（比如同时删了两个），跳过
        m = marks[i]
        end = _branch_end(steps, m)
        notes.append(f"已删掉第 {i + 1} 个分支（含 {end - m} 个步骤）")
        del steps[m:end + 1]
    nxt = steps[cond_index]
    nxt.cond_mode = new_step.cond_mode
    nxt.cond_expr = new_step.cond_expr
    nxt.cond_branches = [dict(m) for m in (new_step.cond_branches or [])]
    normalize_branch_lists(steps)
    return "；".join(notes)


def drop_branch_entry(steps: List[Step], branch_index: int) -> None:
    """删掉某个分支标记时，顺手去掉条件节点里对应的那条分支定义。

    不做这一步的话，剩下的分支会顶着上一分支的名字与匹配值（清单条数还是对的，
    但对应关系错了）。
    """
    all_spans = spans(steps)
    sp = span_by_marker(all_spans, branch_index)
    if sp is None or sp.kind != "branch":
        return
    cond = enclosing_span(all_spans, sp.start, ("condition",))
    if cond is None:
        return
    order = [k for k in range(cond.inner_lo, cond.inner_hi)
             if steps[k].action == BRANCH]
    if branch_index not in order:
        return
    bi = order.index(branch_index)
    cond_step = steps[cond.start]
    items = [dict(m) for m in (cond_step.cond_branches or [])]
    if 0 <= bi < len(items):
        del items[bi]
        cond_step.cond_branches = items


def _span_at(steps: List[Step], index: int, kind: str) -> Optional[Span]:
    for sp in spans(steps):
        if sp.kind == kind and sp.start == index:
            return sp
    return None


def _branch_end(steps: List[Step], branch_idx: int) -> int:
    """分支标记之后到下一个分支/条件结束之前的位置（含）。"""
    sp = _span_at(steps, branch_idx, "branch")
    return sp.end if sp else branch_idx
