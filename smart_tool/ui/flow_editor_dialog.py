# -*- coding: utf-8 -*-
"""流程编辑对话框：用列表方式编排步骤（新增/插入/编辑/删除/上移/下移）。

设计要点：
- 列表从上到下就是执行顺序
- 「循环开始/结束」「条件/分支/条件结束」「组合/组合结束」是成对出现的结构节点：
  新增时系统一起创建，配置只存在「循环开始」「条件」这些节点上（点配对的另一端
  也是编辑同一份配置）
- 块可以互相嵌套，按层级缩进显示（循环体、条件下的分支、分支里的循环……）
- **块都能展开 / 收起**：点块标记左边的 ▾ / ▸ 就行（循环、条件、组合都支持）
- **合并成组合**：按住 Ctrl / Shift 多选几行 → 右键 → 「合并选中节点」，起个名字；
  之后画布上就只显示这一张卡片。取消组合、改名也在右键菜单里
- 每个「循环体」「分支」「组合」的末尾都有一行「＋ 点击创建新节点」
- 每次改动（增/插/改/删/移/合并）立即写盘，并重新编号（从 1 开始），无需手动保存
- 结构不合法时照样保存（不丢改动），但状态栏给出提醒
"""
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from smart_tool.core import blocks, project_store, step_executor
from smart_tool.core.project_store import ProjectStore, Step
from smart_tool.ui.flow_canvas import ACTION_META, step_summary
from smart_tool.ui.help_tip import help_row
from smart_tool.ui.step_editor_dialog import StepEditDialog

CARD_H = 56          # 卡片固定高度，避免列表项显示不全
ADD_ROW_H = 30       # 「＋ 点击创建新节点」这一行的高度
LOOP_INDENT = 22     # 每层缩进像素
SUMMARY_MAX = 78     # 摘要最大字符数（手动截断，不依赖字体度量）
#: 能展开 / 收起的块（分支不支持：它藏在条件里，跟着条件一起收）
COLLAPSIBLE_KINDS = ("loop", "condition", "group")

#: 【?】里的完整说明（界面上只留一句摘要，其余收进弹窗）
FLOW_EDITOR_HELP = (
    "列表从上到下就是执行顺序；双击某行可以编辑它。\n"
    "\n"
    "【结构节点】\n"
    "「循环开始 / 循环结束」「条件 / 分支 / 条件结束」「组合 / 组合结束」都是成对的\n"
    "结构节点：新增时系统一起创建，配置只有一份——点配对的另一端，编辑的也是同一个块。\n"
    "块可以互相嵌套（循环里放条件、分支里放循环都行），按缩进分层显示。\n"
    "\n"
    "【展开 / 收起】\n"
    "块标记左边的 ▾ / ▸ 点一下就能收起或展开（循环、条件、组合都支持），\n"
    "收起时那一行会写着「（已收起 N 个步骤）」。\n"
    "\n"
    "【合并成组合】\n"
    "按住 Ctrl / Shift 多选几行 → 右键 →「合并选中节点」，起个名字：\n"
    "画布上就只显示这一张卡片了。取消组合、改名、展开也在同一个右键菜单里。\n"
    "注意：只能合并挨着的几行，而且不能把「循环 / 条件」从中间切开——\n"
    "要么整个块一起选上，要么只选它里面的步骤（组合可以嵌在块里面）。\n"
    "\n"
    "【标记「登录用」】\n"
    "把「打开登录页 → 填账号 → 填密码 → 点登录」这几步收成一个组合，\n"
    "右键把它标记为「登录用」：运行时带着有效登录态就整块跳过，不用每次重登。\n"
    "要配合【项目管理…】→【登录态】使用（那边要配好登录态和「登录后才有的元素」）。\n"
    "\n"
    "【增删改】\n"
    "每个循环体、每个分支、每个组合的末尾都有一行「＋ 点击创建新节点」，\n"
    "点它新增的节点会留在那个块里面。删除块的标记＝整块删掉（会先问一次）；\n"
    "块里的普通步骤只删自己。所有改动立即保存，不需要手动存。"
)


def _branch_text(steps: List[Step], index: int) -> str:
    """分支标记的摘要：从所属条件的分支清单里取名字与匹配值。"""
    for sp in blocks.spans(steps):
        if sp.kind != "condition" or not sp.contains(index):
            continue
        order = [k for k in range(sp.inner_lo, sp.inner_hi)
                 if steps[k].action == "branch"]
        if index not in order:
            return ""
        bi = order.index(index)
        cond = steps[sp.start]
        name = blocks.condition_branch_name(cond, bi)
        values = "、".join(blocks.condition_branch_values(cond, bi))
        return f"{name}：{values}" if values else name
    return ""


class _StepCard(QWidget):
    """列表里的一行节点卡片：色条 + 中文动作名 + 摘要。

    `on_toggle` 不为 None 时（循环 / 条件 / 组合的**开始**标记），
    最左边多一个 ▾ / ▸，点一下把这个块收起来或展开。
    """

    def __init__(self, step: Step, indent: int = 0,
                 branch_text: str = "", collapsed: bool = False,
                 hidden_count: int = 0, on_toggle=None, number: str = "",
                 parent=None):
        super().__init__(parent)
        self.setFixedHeight(CARD_H)
        name, color = ACTION_META.get(step.action, (step.action, "#888888"))

        root = QHBoxLayout(self)
        root.setContentsMargins(8 + indent, 5, 8, 5)
        root.setSpacing(9)

        if on_toggle is not None:
            btn = QPushButton("▸" if collapsed else "▾")
            btn.setFixedSize(18, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("展开这个块" if collapsed else "收起这个块")
            btn.setStyleSheet(
                "QPushButton {border:none; color:#555555; font-size:12px;"
                " padding:0px;}"
                "QPushButton:hover {color:#111111;}"
            )
            # 延后一拍：点一下会重建整个列表，这个按钮当场就被销毁了，
            # 直接在事件里重建会访问到已析构的控件（闪退）
            btn.clicked.connect(lambda _=False: QTimer.singleShot(0, on_toggle))
            root.addWidget(btn)

        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background:{color}; border-radius:2px;")
        root.addWidget(bar)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        text_box.addStretch()

        prefix = ""
        if step.action in ("loop_start", "condition_start", "group_start"):
            prefix = "⤵ "
        elif step.action in ("loop_end", "condition_end", "group_end"):
            prefix = "⤴ "
        elif step.action == "branch":
            prefix = "⑂ "
        # 自定义名称优先，后面跟上类型名（如「登录页（打开网页）」）；
        # 编号用显示编号：组合是「2-4」这种范围，结束标记干脆没有编号
        shown = f"{step.title}（{name}）" if step.title else name
        head = f"{number}. " if number else ""
        title = QLabel(f"{prefix}{head}{shown}")
        title_font = QFont()
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color:{color};")
        text_box.addWidget(title)

        summary = " ｜ ".join(
            x for x in step_summary(step, branch_text) if x
        )
        if not summary:
            summary = "（无参数）"
        if len(summary) > SUMMARY_MAX:
            summary = summary[:SUMMARY_MAX] + "…"
        if collapsed and hidden_count:
            summary += f"　（已收起 {hidden_count} 个步骤）"
        sub = QLabel(summary)
        sub.setStyleSheet("color:#666666;")
        text_box.addWidget(sub)

        text_box.addStretch()
        root.addLayout(text_box, 1)


class _AddInLoopRow(QWidget):
    """块末尾那一行「＋ 点击创建新节点」，点一下往这个块里加步骤。"""

    def __init__(self, on_click, indent: int = 0, parent=None):
        super().__init__(parent)
        self.setFixedHeight(ADD_ROW_H)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8 + indent, 2, 8, 2)
        self.btn = QPushButton("＋ 点击创建新节点")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setToolTip("新建的步骤会放进这个循环里（循环结束之前）")
        self.btn.setStyleSheet(
            "QPushButton {text-align:left; padding:3px 8px; color:#7a4fb5;"
            " border:1px dashed #b9a6d8; border-radius:4px;"
            " background:#f8f5fd;}"
            "QPushButton:hover {background:#efe7fb;}"
        )
        self.btn.clicked.connect(lambda _=False: on_click())
        layout.addWidget(self.btn, 1)


class FlowEditorDialog(QDialog):
    """流程编辑：列表式编排步骤。"""

    def __init__(self, project_dir, steps: List[Step],
                 parent=None,
                 project_variables: Optional[dict] = None,
                 scene: str = "web"):
        super().__init__(parent)
        self.setWindowTitle("流程编辑")
        self.setMinimumSize(720, 580)
        self.project_dir = Path(project_dir)
        self._store = ProjectStore(self.project_dir)
        self.project_variables = project_variables or {}
        self.scene = scene
        # 本地副本：改一次就立刻写盘一次，关掉弹窗也不会丢
        self._steps: List[Step] = [
            Step.from_dict(s.to_dict()) for s in steps
        ]
        # 分支清单条数跟分支标记对齐（老项目 / 手改过文件的兜底）
        blocks.normalize_branch_lists(self._steps)
        # 列表里的行 → ("step", 步骤下标) 或 ("add", 块起始下标)
        self._rows: List[Tuple[str, int]] = []
        self._spans: list = []
        self._collapsed: set = set()      # 收起来的块（起始标记的下标）
        self._changed = False
        self._init_ui()
        self._reload()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(help_row("列表从上到下就是执行顺序；双击某行可编辑。",
                                "流程编辑", FLOW_EDITOR_HELP))

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list_widget.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._edit_step())
        self.list_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_menu)
        root.addWidget(self.list_widget, 1)

        # 操作按钮条
        btns = QHBoxLayout()
        self.btn_add = QPushButton("新增步骤")
        self.btn_add.clicked.connect(self._add_step)
        btns.addWidget(self.btn_add)
        self.btn_insert = QPushButton("在选中前插入")
        self.btn_insert.clicked.connect(self._insert_step)
        btns.addWidget(self.btn_insert)
        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self._edit_step)
        btns.addWidget(self.btn_edit)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self._delete_step)
        btns.addWidget(self.btn_delete)
        self.btn_up = QPushButton("上移")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        btns.addWidget(self.btn_up)
        self.btn_down = QPushButton("下移")
        self.btn_down.clicked.connect(lambda: self._move(1))
        btns.addWidget(self.btn_down)
        btns.addStretch()
        root.addLayout(btns)

        # 底部：状态 + 关闭（改动已实时保存，没有保存/取消之分）
        bottom = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#777777;")
        bottom.addWidget(self.count_label)
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label)
        bottom.addStretch()
        self.btn_close = QPushButton("关闭")
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_close)
        root.addLayout(bottom)

    # ------------------------------
    # 列表刷新
    # ------------------------------
    def _reload(self, select_index: Optional[int] = None):
        """重建列表；select_index 指定刷新后选中的步骤下标。

        收起来的块（`self._collapsed`）只显示它自己的开始标记，里面的行全部不画。
        """
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._rows = []
        self._spans = blocks.spans(self._steps)
        depths = blocks.depths(self._steps)
        span_by_start = {sp.start: sp for sp in self._spans}
        numbers = blocks.step_numbers(self._steps)
        # 「＋ 点击创建新节点」放在每个循环体 / 分支 / 组合的末尾
        add_at = {sp.insert_pos: sp for sp in self._spans
                  if sp.kind in ("loop", "branch", "group")}
        # 收起来的块：内部的行（含结束标记）整段不显示
        skip = set()
        for sp in self._spans:
            if sp.start in self._collapsed:
                skip.update(range(sp.inner_lo, sp.end + 1))
        for i, s in enumerate(self._steps):
            if i in skip:
                continue
            if i in add_at:
                self._append_add_row(add_at[i], depths[i])
            self._append_step_row(i, depths[i], span_by_start.get(i),
                                  numbers[i])
        self.list_widget.blockSignals(False)

        if select_index is not None:
            for row, (kind, idx) in enumerate(self._rows):
                if kind == "step" and idx == select_index:
                    self.list_widget.setCurrentRow(row)
                    break
        self._update_buttons()

    def _append_step_row(self, idx: int, depth: int, span=None, number: str = ""):
        """加一行卡片；span 只在「块的开始标记」这一行传进来（要挂展开按钮）。"""
        collapsed = False
        hidden_count = 0
        on_toggle = None
        if span is not None and span.kind in COLLAPSIBLE_KINDS:
            collapsed = span.start in self._collapsed
            hidden_count = blocks.inner_count(span)
            on_toggle = lambda start=span.start: self._toggle_collapse(start)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, CARD_H))          # 必须显式设置，否则卡片会被压扁
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(
            item, _StepCard(self._steps[idx],
                            LOOP_INDENT * depth,
                            branch_text=_branch_text(self._steps, idx),
                            collapsed=collapsed,
                            hidden_count=hidden_count,
                            on_toggle=on_toggle,
                            number=number)
        )
        self._rows.append(("step", idx))

    def _append_add_row(self, span, depth: int):
        """在块（循环体 / 分支 / 组合）末尾插一行「＋ 点击创建新节点」。"""
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, ADD_ROW_H))
        # 只保留「可用」：这一行是按钮，不该被当成步骤选中
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(
            item, _AddInLoopRow(lambda: self._create_in_block(span.start),
                                LOOP_INDENT * (depth + 1))
        )
        self._rows.append(("add", span.start))

    # ------------------------------
    # 展开 / 收起
    # ------------------------------
    def _toggle_collapse(self, start: int):
        """收起 / 展开起始标记在 start 的那个块。"""
        if start in self._collapsed:
            self._collapsed.discard(start)
        else:
            self._collapsed.add(start)
        self._reload(select_index=start)

    def _selected_row(self) -> int:
        """当前选中行 → self._steps 下标；没选中（或选的是「＋」行）返回 -1。"""
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._rows):
            kind, idx = self._rows[row]
            if kind == "step":
                return idx
        return -1

    def _selected_indices(self) -> List[int]:
        """多选出来的步骤下标（去重、按列表顺序）。"""
        out: List[int] = []
        for item in self.list_widget.selectedItems():
            row = self.list_widget.row(item)
            if 0 <= row < len(self._rows):
                kind, idx = self._rows[row]
                if kind == "step" and idx not in out:
                    out.append(idx)
        return sorted(out)

    # ------------------------------
    # 右键菜单：合并 / 取消组合 / 改名 / 展开收起
    # ------------------------------
    def _show_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is not None and not item.isSelected():
            # 右键点在某一行上：先把它选中（右键本身不会改选中项），
            # 免得「右键第 5 行、菜单里动的却是之前选中的第 2 行」
            self.list_widget.clearSelection()
            item.setSelected(True)
            self.list_widget.setCurrentItem(item)
        sel = self._selected_indices()
        group_sp = self._group_of(sel)
        block_sp = self._collapsible_of(sel)

        menu = QMenu(self)
        act_merge = QAction("合并选中节点…", menu)
        act_merge.setEnabled(len(sel) >= 2)
        act_merge.triggered.connect(self._merge_selected)
        menu.addAction(act_merge)
        act_unge = QAction("取消组合（里面的步骤都留着）", menu)
        act_unge.setEnabled(group_sp is not None)
        act_unge.triggered.connect(self._ungroup_selected)
        menu.addAction(act_unge)
        act_rename = QAction("给组合改名…", menu)
        act_rename.setEnabled(group_sp is not None)
        act_rename.triggered.connect(self._rename_group_selected)
        menu.addAction(act_rename)
        if group_sp is not None:
            marked = bool(self._steps[group_sp.start].skip_if_logged_in)
            act_login = QAction(
                "取消「登录用」标记" if marked else "标记为登录用（登录态有效时跳过）",
                menu)
            act_login.setToolTip(
                "标记后：运行时带着有效登录态就直接跳过这个组合，不用再登一遍；\n"
                "登录态失效时会自动重跑整条流程，那时它照常执行。")
            act_login.triggered.connect(
                lambda: self._toggle_login_group(group_sp.start))
            menu.addAction(act_login)
        menu.addSeparator()
        if block_sp is not None:
            folded = block_sp.start in self._collapsed
            act_fold = QAction("展开这个块" if folded else "收起这个块", menu)
            act_fold.triggered.connect(
                lambda: self._toggle_collapse(block_sp.start))
            menu.addAction(act_fold)
        if not sel:
            act_none = QAction("（先选中一行再操作）", menu)
            act_none.setEnabled(False)
            menu.addAction(act_none)
        menu.exec(self.list_widget.viewport().mapToGlobal(pos))

    def _group_of(self, indices: List[int]):
        """选中的行落在哪个组合里（含组合的两个标记）。"""
        for idx in indices:
            sp = blocks.enclosing_span(self._spans, idx, ("group",))
            if sp is not None:
                return sp
        return None

    def _collapsible_of(self, indices: List[int]):
        """选中的行落在哪个可收起的块里。"""
        for idx in indices:
            sp = blocks.enclosing_span(self._spans, idx, COLLAPSIBLE_KINDS)
            if sp is not None:
                return sp
        return None

    def _merge_selected(self):
        """把选中的连续几行合成一个组合（画布上就只显示一张卡片）。"""
        sel = self._selected_indices()
        if len(sel) < 2:
            QMessageBox.information(
                self, "合并节点",
                "请先按住 Ctrl / Shift 选中要合并的两个以上节点。")
            return
        lo, hi = sel[0], sel[-1]
        if sel != list(range(lo, hi + 1)):
            QMessageBox.information(
                self, "合并节点",
                "只能合并挨着的几行：中间别夹着没选中的行。")
            return
        problem = blocks.can_group(self._steps, lo, hi)
        if problem:
            QMessageBox.information(self, "没法合并", problem)
            return
        name, ok = QInputDialog.getText(
            self, "给组合起个名字",
            "这几步要合成一张卡片，给它起个名字（画布上显示这个名字）：",
            text=blocks.DEFAULT_GROUP_NAME)
        if not ok:
            return
        name = blocks.make_group(self._steps, lo, hi, name)
        self._commit(select_index=lo)
        self.status_label.setText(
            f"已自动保存；这几步合并成了「{name}」，画布上只显示这一张卡片")

    def _ungroup_selected(self):
        """取消组合：只去掉这层壳，里面的步骤一个都不删。"""
        sp = self._group_of(self._selected_indices())
        if sp is None:
            QMessageBox.information(self, "取消组合", "选中的行不在任何组合里。")
            return
        name = blocks.ungroup(self._steps, sp.start) or "组合"
        self._commit(select_index=min(sp.start, len(self._steps) - 1))
        self.status_label.setText(
            f"已自动保存；「{name}」已取消组合，里面的步骤都留着")

    def _rename_group_selected(self):
        sp = self._group_of(self._selected_indices())
        if sp is None:
            QMessageBox.information(self, "组合改名", "选中的行不在任何组合里。")
            return
        self._rename_group_at(sp.start)

    def _rename_group_at(self, index: int):
        """给 index 处的「组合开始」改名字。"""
        old = self._steps[index].title
        name, ok = QInputDialog.getText(
            self, "组合改名", "组合名称（画布上显示的就是它）：", text=old)
        if not ok:
            return
        self._steps[index].title = (
            (name or "").strip() or blocks.DEFAULT_GROUP_NAME)
        self._commit(select_index=index)

    def _toggle_login_group(self, index: int):
        """把某个组合标记为「登录用」／取消标记。

        标记之后：运行时带着有效登录态就直接跳过它（不用再登一遍）；
        登录态失效时会自动清掉重跑，那时它照常执行。
        """
        step = self._steps[index]
        step.skip_if_logged_in = not step.skip_if_logged_in
        self._commit(select_index=index)
        name = step.title or "组合"
        self.status_label.setText(
            f"已自动保存；「{name}」"
            + ("已标记为登录用：带着有效登录态时会自动跳过它"
               if step.skip_if_logged_in else "已取消「登录用」标记"))

    def _span_of_marker(self, idx: int):
        """idx 正好是某个块的起始/结束标记 → 返回那个块。"""
        return blocks.span_by_marker(self._spans, idx)

    def _move_bounds(self, idx: int) -> Tuple[int, int]:
        """该步骤允许上下移动到的下标范围（不许跨出自己所在的块）。"""
        return blocks.move_bounds(self._steps, idx)

    def _update_buttons(self):
        idx = self._selected_row()
        has = idx >= 0
        block = self._span_of_marker(idx) if has else None
        if block is not None:
            lo, hi = block.start, block.end      # 选中块的标记：整块上下移动
        elif has:
            lo, hi = self._move_bounds(idx)
        else:
            lo = hi = 0
        self.btn_insert.setEnabled(has)
        self.btn_edit.setEnabled(has)
        self.btn_delete.setEnabled(has)
        self.btn_up.setEnabled(has and idx > lo)
        self.btn_down.setEnabled(has and idx < hi)
        loops = sum(1 for sp in self._spans if sp.kind == "loop")
        conds = sum(1 for sp in self._spans if sp.kind == "condition")
        groups = sum(1 for sp in self._spans if sp.kind == "group")
        folded = sum(1 for sp in self._spans
                     if sp.kind in COLLAPSIBLE_KINDS and sp.start in self._collapsed)
        extra = "".join([
            f"，{loops} 个循环" if loops else "",
            f"，{conds} 个条件" if conds else "",
            f"，{groups} 个组合" if groups else "",
            f"，{folded} 个已收起" if folded else "",
        ])
        # 编号按「显示编号」数：结束标记和组合都不占号，所以不写死成行数
        top = blocks.last_number(self._steps)
        head = f"编号 1~{top}" if top else "还没有节点"
        self.count_label.setText(head + extra)

    # ------------------------------
    # 块里的「＋ 点击创建新节点」
    # ------------------------------
    def _create_in_block(self, start_idx: int):
        """点「＋」那一行：延后一拍再弹新建对话框。

        必须延后：插入后会重建整个列表，正在处理点击事件的按钮会被销毁，
        直接在事件里重建会让 Qt 访问已析构的控件（表现为无提示闪退）。
        """
        QTimer.singleShot(0, lambda: self._add_into_block(start_idx))

    def _add_into_block(self, start_idx: int):
        step = self._new_step_via_dialog()
        if step is None:
            return
        sp = next((x for x in blocks.spans(self._steps) if x.start == start_idx),
                  None)
        pos = sp.insert_pos if sp else len(self._steps)
        self._insert_at(pos, step)

    # ------------------------------
    # 增删改移
    # ------------------------------
    def _new_step_via_dialog(self) -> Optional[Step]:
        dlg = StepEditDialog(
            self.project_dir, None, self,
            variable_names=step_executor.available_variables(
                self._steps, self.project_variables,
                step_executor.library_written_vars(self.project_dir)),
            default_url=self._default_url(),
            scene=self.scene,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.get_step()
        return None

    def _default_url(self) -> str:
        """元素捕获默认打开的地址：项目里第一个「打开网页」。"""
        return next((s.url for s in self._steps
                     if s.action == "navigate" and s.url), "")

    def _add_step(self):
        """新增：插到选中行之后（在循环里就留在循环里）；没选中则追加到末尾。"""
        idx = self._selected_row()
        step = self._new_step_via_dialog()
        if step is None:
            return
        self._insert_at(idx + 1 if idx >= 0 else len(self._steps), step)

    def _insert_step(self):
        """在选中的那一行之前插入。"""
        idx = self._selected_row()
        if idx < 0:
            return
        step = self._new_step_via_dialog()
        if step is None:
            return
        self._insert_at(idx, step)

    def _insert_at(self, pos: int, step: Step):
        """插入一个步骤；新增「循环」/「条件」时自动补上配套的结构节点。"""
        new_steps = [step]
        if step.action == "loop_start":
            new_steps.append(Step(id=0, action=blocks.LOOP_END))
        elif step.action == "condition_start":
            # 条件默认给两个分支，用户可在条件节点里增删
            if not step.cond_branches:
                step.cond_branches = [blocks.new_branch("分支 1"),
                                      blocks.new_branch("分支 2")]
            new_steps.append(Step(id=0, action=blocks.BRANCH))
            new_steps.append(Step(id=0, action=blocks.BRANCH))
            new_steps.append(Step(id=0, action=blocks.COND_END))
        self._steps[pos:pos] = new_steps
        self._commit(select_index=pos)

    def _edit_step(self):
        idx = self._selected_row()
        if idx < 0:
            return
        # 设置合并：点「循环结束」「分支」「条件结束」「组合结束」都是编辑所属的那个块
        idx = blocks.marker_owner_index(self._steps, idx)
        if self._steps[idx].action == blocks.GROUP_START:
            # 组合节点没有表单，能改的只有名字（结构改动走右键菜单）
            self._rename_group_at(idx)
            return
        old = self._steps[idx]
        dlg = StepEditDialog(
            self.project_dir, old, self,
            variable_names=step_executor.available_variables(
                self._steps, self.project_variables,
                step_executor.library_written_vars(self.project_dir)),
            default_url=self._default_url(),
            scene=self.scene,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_step = dlg.get_step()
        new_step.pos = old.pos        # 保留画布坐标
        if old.action == "condition_start" and new_step.action == "condition_start":
            note = blocks.apply_condition_edit(self._steps, idx, new_step)
            self._commit(select_index=idx)
            if note:
                self.status_label.setText(f"已自动保存；{note}")
            return
        self._steps[idx] = new_step
        notes: List[str] = []
        if old.action == "read_data" and new_step.action == "read_data":
            # 读取节点改名（产出变量 / 字段名）→ 别处的引用一起跟着改
            notes = project_store.rename_field_refs(self._steps, old, new_step,
                                                    skip=idx)
        self._commit(select_index=idx)
        if notes:
            self.status_label.setText(
                self.status_label.text() + "；变量改名：" + "；".join(notes))

    def _delete_step(self):
        idx = self._selected_row()
        if idx < 0:
            return
        s = self._steps[idx]
        name = ACTION_META.get(s.action, (s.action, ""))[0]
        block = self._span_of_marker(idx)
        if block is not None and block.kind == "group":
            # 组合节点上按「删除」＝取消组合，绝不会连里面的步骤一起删掉
            gname = self._steps[block.start].title or "组合"
            reply = QMessageBox.question(
                self, "取消组合",
                f"「{gname}」是组合节点。\n"
                "取消组合只是去掉这一层壳，里面的步骤一个都不会删。\n"
                "确定取消吗？"
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            blocks.ungroup(self._steps, block.start)
            self._commit(select_index=min(block.start, len(self._steps) - 1))
            return
        if block is not None:
            # 只有选中块的「标记」才整块删；块里的普通步骤就删它自己
            body = block.end - block.start - 1
            reply = QMessageBox.question(
                self, f"删除{blocks.ACTION_CN.get(s.action, name)}",
                f"「{blocks.ACTION_CN.get(s.action, name)}」是配套的结构节点，"
                f"删除会连同它里面的 {body} 个步骤一起去掉。\n确定删除吗？"
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            blocks.drop_branch_entry(self._steps, block.start)
            del self._steps[block.start:block.end + 1]
            self._commit(select_index=min(block.start, len(self._steps) - 1))
            return
        reply = QMessageBox.question(
            self, "删除步骤", f"确定删除这个步骤（{name}）？"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._steps.pop(idx)
        self._commit(select_index=min(idx, len(self._steps) - 1))

    def _move(self, delta: int):
        idx = self._selected_row()
        if idx < 0:
            return
        block = self._span_of_marker(idx)
        if block is not None:
            # 选中块的标记：整块上下移动
            a, b = block.start, block.end
            if delta < 0:
                if a == 0:
                    return
                self._steps[a - 1:b + 1] = (
                    self._steps[a:b + 1] + [self._steps[a - 1]]
                )
                target = a - 1
            else:
                if b >= len(self._steps) - 1:
                    return
                self._steps[a:b + 2] = (
                    [self._steps[b + 1]] + self._steps[a:b + 1]
                )
                target = a + 1
            self._commit(select_index=target)
            return
        lo, hi = self._move_bounds(idx)
        target = idx + delta
        if not (lo <= target <= hi):
            return
        self._steps[idx], self._steps[target] = (
            self._steps[target], self._steps[idx]
        )
        self._commit(select_index=target)

    # ------------------------------
    # 自动保存（编号从 1 开始）
    # ------------------------------
    def _commit(self, select_index: Optional[int] = None):
        """任何改动都走这里：补齐分支清单 → 重排编号 → 写盘 → 刷新列表。"""
        blocks.normalize_branch_lists(self._steps)
        self._renumber()
        self._prune_collapsed()
        self._store.save(self._steps)
        self._changed = True
        self._reload(select_index=select_index)
        self._set_saved_tip()

    def _prune_collapsed(self):
        """增删步骤会让下标整体挪动，收起状态只保留「确实还落在某个块开头」的。

        不清理的话，收起的记录会挂到一个无关的普通步骤上（虽然不会崩，
        但点开时会莫名其妙收起一个不相干的块）。
        """
        starts = {sp.start for sp in blocks.spans(self._steps)
                  if sp.kind in COLLAPSIBLE_KINDS}
        self._collapsed &= starts

    def _renumber(self):
        """步骤编号按列表顺序从 1 重新排。"""
        for i, s in enumerate(self._steps, start=1):
            s.id = i

    def _set_saved_tip(self):
        """状态栏提示：已保存 / 结构有问题。"""
        problem = blocks.validate(self._steps)
        if problem:
            self.status_label.setText(f"已自动保存；但{problem}")
            self.status_label.setStyleSheet("color:#c62828;")
            return
        # 显示编号的最大值（结束标记不占号，所以不一定是行数）
        top = blocks.last_number(self._steps) or "0"
        self.status_label.setText(
            f"已自动保存（{len(self._steps)} 行，编号 1~{top}）"
        )
        self.status_label.setStyleSheet("color:#2e7d32;")

    @property
    def changed(self) -> bool:
        """弹窗里是否改动过（调用方据此决定要不要刷新画布）。"""
        return self._changed

    def get_steps(self) -> List[Step]:
        """返回编辑后的步骤（已按 1 起重新编号，且已写盘）。"""
        return self._steps


class GroupEditDialog(QDialog):
    """画布上的组合卡片：改名字，或者进去编辑里面某个节点。

    画布上这张卡片只是「一层壳」：能改名字、能打开里面的节点逐个编辑。
    **结构性改动（合并 / 取消组合 / 删除 / 登录用标记）只在【流程编辑…】里做**——
    免得在画布上顺手一点就把流程结构改了。

    :param build_items: 无参回调，返回 [(标题, 摘要, 行号), …]（每次刷新都重新问）
    :param on_edit: 点某一行的「编辑」时调用 on_edit(行号)；返回后弹窗自己刷新
    """

    def __init__(self, group: Step, number: str, build_items, on_edit,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("组合")
        self.setMinimumSize(600, 420)
        self._build_items = build_items
        self._on_edit = on_edit

        root = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("组合名："))
        self.name_edit = QLineEdit(group.title or blocks.DEFAULT_GROUP_NAME)
        self.name_edit.setPlaceholderText(blocks.DEFAULT_GROUP_NAME)
        row.addWidget(self.name_edit, 1)
        root.addLayout(row)
        tip = QLabel(
            "改名字只影响画布上这张卡片显示的文字"
            + (f"（它占的编号是 {number}）" if number else "")
            + "；里面的步骤照旧按顺序一个个执行。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555555;")
        root.addWidget(tip)

        root.addWidget(QLabel("里面的步骤（选中后点【编辑…】，或双击那一行）："))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.itemDoubleClicked.connect(lambda _i: self._edit_selected())
        root.addWidget(self.list_widget, 1)

        bottom = QHBoxLayout()
        self.btn_edit = QPushButton("编辑…")
        self.btn_edit.setToolTip("打开选中那一步的编辑框（改完立即生效）")
        self.btn_edit.clicked.connect(self._edit_selected)
        bottom.addWidget(self.btn_edit)
        bottom.addStretch()
        hint = QLabel("合并 / 取消组合 / 删除请在【流程编辑…】里做")
        hint.setStyleSheet("color:#888888;")
        bottom.addWidget(hint)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        bottom.addWidget(self.btn_ok)
        root.addLayout(bottom)

        self._refresh()

    def _refresh(self):
        """重列表里：里面的步骤可能刚被编辑过（摘要就变了）。"""
        items = self._build_items()
        self.list_widget.clear()
        for title, summary, index in items:
            item = QListWidgetItem(
                title if not summary else f"{title}　｜　{summary}")
            item.setSizeHint(QSize(0, 30))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(f"{title}\n双击这一行（或选中后点【编辑…】）改它")
            self.list_widget.addItem(item)
        self.btn_edit.setEnabled(bool(items))
        if items:
            self.list_widget.setCurrentRow(0)

    def _edit_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        self._on_edit(int(index))
        self._refresh()          # 编辑完可能改了摘要 / 名字，重新列一遍

    @property
    def group_name(self) -> str:
        return self.name_edit.text().strip() or blocks.DEFAULT_GROUP_NAME
