# -*- coding: utf-8 -*-
"""流程编辑对话框：用列表方式编排步骤（新增/插入/编辑/删除/上移/下移）。

设计要点：
- 列表从上到下就是执行顺序
- 「循环开始/结束」「条件/分支/条件结束」是成对出现的结构节点：新增时系统一起创建，
  配置只存在「循环开始」「条件」这些节点上（点配对的另一端也是编辑同一份配置）
- 块可以互相嵌套，按层级缩进显示（循环体、条件下的分支、分支里的循环……）
- 每个「循环体」和「分支」的末尾都有一行「＋ 点击创建新节点」，点一下把步骤加进去
- 每次改动（增/插/改/删/移）立即写盘，并重新编号（从 1 开始），无需手动保存
- 结构不合法时照样保存（不丢改动），但状态栏给出提醒
"""
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from smart_tool.core import blocks, step_executor
from smart_tool.core.project_store import ProjectStore, Step
from smart_tool.ui.flow_canvas import ACTION_META, step_summary
from smart_tool.ui.step_editor_dialog import StepEditDialog

CARD_H = 56          # 卡片固定高度，避免列表项显示不全
ADD_ROW_H = 30       # 「＋ 点击创建新节点」这一行的高度
LOOP_INDENT = 22     # 每层缩进像素
SUMMARY_MAX = 78     # 摘要最大字符数（手动截断，不依赖字体度量）


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
    """列表里的一行节点卡片：色条 + 中文动作名 + 摘要。"""

    def __init__(self, step: Step, indent: int = 0,
                 branch_text: str = "", parent=None):
        super().__init__(parent)
        self.setFixedHeight(CARD_H)
        name, color = ACTION_META.get(step.action, (step.action, "#888888"))

        root = QHBoxLayout(self)
        root.setContentsMargins(8 + indent, 5, 8, 5)
        root.setSpacing(9)

        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background:{color}; border-radius:2px;")
        root.addWidget(bar)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        text_box.addStretch()

        prefix = ""
        if step.action in ("loop_start", "condition_start"):
            prefix = "⤵ "
        elif step.action in ("loop_end", "condition_end"):
            prefix = "⤴ "
        elif step.action == "branch":
            prefix = "⑂ "
        # 自定义名称优先，后面跟上类型名（如「登录页（打开网页）」）
        shown = f"{step.title}（{name}）" if step.title else name
        title = QLabel(f"{prefix}{step.id}. {shown}")
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
        sub = QLabel(summary)
        sub.setStyleSheet("color:#666666;")
        text_box.addWidget(sub)

        text_box.addStretch()
        root.addLayout(text_box, 1)


class _AddInLoopRow(QWidget):
    """循环体末尾那一行「＋ 点击创建新节点」，点一下往这个循环里加步骤。"""

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
                 project_variables: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("流程编辑")
        self.setMinimumSize(720, 580)
        self.project_dir = Path(project_dir)
        self._store = ProjectStore(self.project_dir)
        self.project_variables = project_variables or {}
        # 本地副本：改一次就立刻写盘一次，关掉弹窗也不会丢
        self._steps: List[Step] = [
            Step.from_dict(s.to_dict()) for s in steps
        ]
        # 分支清单条数跟分支标记对齐（老项目 / 手改过文件的兜底）
        blocks.normalize_branch_lists(self._steps)
        # 列表里的行 → ("step", 步骤下标) 或 ("add", 块起始下标)
        self._rows: List[Tuple[str, int]] = []
        self._spans: list = []
        self._changed = False
        self._init_ui()
        self._reload()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)

        tip = QLabel(
            "列表从上到下就是执行顺序；双击某行可编辑。\n"
            "「循环开始/结束」「条件/分支/条件结束」都是系统一起创建的结构节点，"
            "设置只有一份（点配对的另一端也是编辑同一个块）；块可以嵌套，按缩进分层。\n"
            "每个循环体 / 分支末尾都有「＋ 点击创建新节点」；所有改动立即保存。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#777777;")
        root.addWidget(tip)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.list_widget.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._edit_step())
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
        """重建列表；select_index 指定刷新后选中的步骤下标。"""
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self._rows = []
        self._spans = blocks.spans(self._steps)
        depths = blocks.depths(self._steps)
        # 「＋ 点击创建新节点」放在每个循环体 / 分支的末尾
        add_at = {sp.insert_pos: sp for sp in self._spans
                  if sp.kind in ("loop", "branch")}
        for i, s in enumerate(self._steps):
            if i in add_at:
                self._append_add_row(add_at[i], depths[i])
            self._append_step_row(i, depths[i])
        self.list_widget.blockSignals(False)

        if select_index is not None:
            for row, (kind, idx) in enumerate(self._rows):
                if kind == "step" and idx == select_index:
                    self.list_widget.setCurrentRow(row)
                    break
        self._update_buttons()

    def _append_step_row(self, idx: int, depth: int):
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, CARD_H))          # 必须显式设置，否则卡片会被压扁
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(
            item, _StepCard(self._steps[idx],
                            LOOP_INDENT * depth,
                            branch_text=_branch_text(self._steps, idx))
        )
        self._rows.append(("step", idx))

    def _append_add_row(self, span, depth: int):
        """在块（循环体 / 分支）末尾插一行「＋ 点击创建新节点」。"""
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

    def _selected_row(self) -> int:
        """当前选中行 → self._steps 下标；没选中（或选的是「＋」行）返回 -1。"""
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._rows):
            kind, idx = self._rows[row]
            if kind == "step":
                return idx
        return -1

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
        extra = "".join([
            f"，{loops} 个循环" if loops else "",
            f"，{conds} 个条件" if conds else "",
        ])
        self.count_label.setText(f"共 {len(self._steps)} 个步骤" + extra)

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
                self._steps, self.project_variables),
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.get_step()
        return None

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
        # 设置合并：点「循环结束」「分支」「条件结束」都是编辑所属的那个块
        idx = blocks.marker_owner_index(self._steps, idx)
        old = self._steps[idx]
        dlg = StepEditDialog(
            self.project_dir, old, self,
            variable_names=step_executor.available_variables(
                self._steps, self.project_variables),
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
        self._commit(select_index=idx)

    def _delete_step(self):
        idx = self._selected_row()
        if idx < 0:
            return
        s = self._steps[idx]
        name = ACTION_META.get(s.action, (s.action, ""))[0]
        block = self._span_of_marker(idx)
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
        self._store.save(self._steps)
        self._changed = True
        self._reload(select_index=select_index)
        self._set_saved_tip()

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
        self.status_label.setText(
            f"已自动保存（{len(self._steps)} 个步骤，编号 1~{len(self._steps)}）"
        )
        self.status_label.setStyleSheet("color:#2e7d32;")

    @property
    def changed(self) -> bool:
        """弹窗里是否改动过（调用方据此决定要不要刷新画布）。"""
        return self._changed

    def get_steps(self) -> List[Step]:
        """返回编辑后的步骤（已按 1 起重新编号，且已写盘）。"""
        return self._steps
