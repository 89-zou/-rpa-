# -*- coding: utf-8 -*-
"""流程编辑对话框：用列表方式编排步骤（新增/插入/编辑/删除/上移/下移）。

设计要点：
- 列表从上到下就是执行顺序
- 「循环开始 / 循环结束」是一对节点：新增「循环」时系统一起创建；
  两者的设置合并成一份（配置存在循环开始节点上，点循环结束也是编辑它）
- 循环体（两个节点之间的步骤）缩进显示，末尾有一行「＋ 点击创建新节点」，
  点一下就能往这个循环里加步骤，不用先算插入位置
- 每次改动（增/插/改/删/移）立即写盘，并重新编号（从 1 开始），无需手动保存
- 循环标记没配对时照样保存（不丢改动），但状态栏给出提醒
"""
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from smart_tool.core.project_store import (
    ProjectStore, Step, loop_block_at, loop_ranges, loop_start_index,
)
from smart_tool.core.step_executor import build_segments
from smart_tool.ui.flow_canvas import ACTION_META, step_summary
from smart_tool.ui.step_editor_dialog import StepEditDialog

CARD_H = 56          # 卡片固定高度，避免列表项显示不全
ADD_ROW_H = 30       # 「＋ 点击创建新节点」这一行的高度
LOOP_INDENT = 22     # 循环体缩进像素
SUMMARY_MAX = 78     # 摘要最大字符数（手动截断，不依赖字体度量）


def _loop_flags(steps: List[Step]) -> List[int]:
    """标出每个步骤是否位于循环体内（1=在循环体内，用于缩进）。"""
    flags, depth = [], 0
    for s in steps:
        if s.action == "loop_start":
            flags.append(0)
            depth = 1
        elif s.action == "loop_end":
            depth = 0
            flags.append(0)
        else:
            flags.append(depth)
    return flags


class _StepCard(QWidget):
    """列表里的一行节点卡片：色条 + 中文动作名 + 摘要。"""

    def __init__(self, step: Step, data_source: dict, indent: int = 0,
                 parent=None):
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
        if step.action == "loop_start":
            prefix = "⤵ "
        elif step.action == "loop_end":
            prefix = "⤴ "
        title = QLabel(f"{prefix}{step.id}. {name}")
        title_font = QFont()
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color:{color};")
        text_box.addWidget(title)

        summary = " ｜ ".join(x for x in step_summary(step, data_source) if x)
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
                 data_source: Optional[dict] = None, parent=None,
                 project_variables: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("流程编辑")
        self.setMinimumSize(720, 580)
        self.project_dir = Path(project_dir)
        self._store = ProjectStore(self.project_dir)
        self.data_source = data_source or {}
        self.project_variables = project_variables or {}
        # 本地副本：改一次就立刻写盘一次，关掉弹窗也不会丢
        self._steps: List[Step] = [
            Step.from_dict(s.to_dict()) for s in steps
        ]
        # 列表里的行 → ("step", 步骤下标) 或 ("add", 循环开始下标)
        self._rows: List[Tuple[str, int]] = []
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
            "「循环开始 / 循环结束」是系统一起创建的一对节点，设置只存一份"
            "（点循环结束也是编辑这个循环的配置）；循环体缩进显示。\n"
            "循环体末尾有「＋ 点击创建新节点」，点一下就往该循环里加步骤；所有改动立即保存。"
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
        flags = _loop_flags(self._steps)
        add_before = {b: a for a, b in loop_ranges(self._steps)}
        for i, s in enumerate(self._steps):
            if i in add_before:
                self._append_add_row(add_before[i])
            self._append_step_row(i, flags[i])
        self.list_widget.blockSignals(False)

        if select_index is not None:
            for row, (kind, idx) in enumerate(self._rows):
                if kind == "step" and idx == select_index:
                    self.list_widget.setCurrentRow(row)
                    break
        self._update_buttons()

    def _append_step_row(self, idx: int, indent: int):
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, CARD_H))          # 必须显式设置，否则卡片会被压扁
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(
            item, _StepCard(self._steps[idx], self.data_source,
                            LOOP_INDENT * indent)
        )
        self._rows.append(("step", idx))

    def _append_add_row(self, start_idx: int):
        """在「循环结束」之前插一行「＋ 点击创建新节点」。"""
        start_id = self._steps[start_idx].id
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, ADD_ROW_H))
        # 只保留「可用」：这一行是按钮，不该被当成步骤选中
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(
            item, _AddInLoopRow(lambda: self._create_in_loop(start_id),
                                LOOP_INDENT)
        )
        self._rows.append(("add", start_idx))

    def _selected_row(self) -> int:
        """当前选中行 → self._steps 下标；没选中（或选的是「＋」行）返回 -1。"""
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._rows):
            kind, idx = self._rows[row]
            if kind == "step":
                return idx
        return -1

    def _move_bounds(self, idx: int) -> Tuple[int, int]:
        """该步骤允许上下移动到的下标范围。

        循环体里的步骤只能在循环体内挪；循环外的步骤不许跨过循环块
        （要整体挪循环，请选中「循环」那一行）。
        """
        block = loop_block_at(self._steps, idx)
        if block and block[0] < idx:
            return block[0] + 1, block[1] - 1
        lo, hi = 0, len(self._steps) - 1
        for a, b in loop_ranges(self._steps):
            if b < idx:
                lo = max(lo, b + 1)
            elif a > idx:
                hi = min(hi, a - 1)
        return lo, hi

    def _update_buttons(self):
        idx = self._selected_row()
        has = idx >= 0
        block = loop_block_at(self._steps, idx) if has else None
        if block:
            lo, hi = block
        elif has:
            lo, hi = self._move_bounds(idx)
        else:
            lo = hi = 0
        self.btn_insert.setEnabled(has)
        self.btn_edit.setEnabled(has)
        self.btn_delete.setEnabled(has)
        self.btn_up.setEnabled(has and idx > lo)
        self.btn_down.setEnabled(has and idx < hi)
        loops = len(loop_ranges(self._steps))
        self.count_label.setText(
            f"共 {len(self._steps)} 个步骤"
            + (f"，{loops} 个循环体" if loops else "")
        )

    # ------------------------------
    # 循环体里「＋ 点击创建新节点」
    # ------------------------------
    def _create_in_loop(self, start_id: int):
        """点「＋」那一行：延后一拍再弹新建对话框。

        必须延后：插入后会重建整个列表，正在处理点击事件的按钮会被销毁，
        直接在事件里重建会让 Qt 访问已析构的控件（表现为无提示闪退）。
        """
        QTimer.singleShot(0, lambda: self._add_into_loop(start_id))

    def _add_into_loop(self, start_id: int):
        step = self._new_step_via_dialog()
        if step is None:
            return
        start_idx = next((i for i, s in enumerate(self._steps)
                          if s.id == start_id), -1)
        block = loop_block_at(self._steps, start_idx) if start_idx >= 0 else None
        pos = block[1] if block else len(self._steps)   # 插到「循环结束」之前
        self._insert_at(pos, step)

    # ------------------------------
    # 增删改移
    # ------------------------------
    def _new_step_via_dialog(self) -> Optional[Step]:
        dlg = StepEditDialog(
            self.project_dir, None, self,
            data_source=self.data_source,
            project_variables=self.project_variables,
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
        """插入一个步骤；新增「循环」时自动补上配对的结束标记。"""
        if step.action == "loop_start" and self._in_loop_body(pos):
            QMessageBox.warning(
                self, "不支持嵌套循环",
                "「循环」里面不能再放「循环」。\n"
                "如果想改成别的循环，请先选中原来那个「循环」把它删掉。"
            )
            return
        new_steps = [step]
        if step.action == "loop_start":
            end = Step(id=0, action="loop_end")
            new_steps.append(end)        # 结束标记自动生成，由画布排版给位置
        self._steps[pos:pos] = new_steps
        self._commit(select_index=pos)

    def _in_loop_body(self, pos: int) -> bool:
        """在 pos 处插入，是否会落进某个循环体内。"""
        return any(a < pos <= b for a, b in loop_ranges(self._steps))

    def _edit_step(self):
        idx = self._selected_row()
        if idx < 0:
            return
        # 设置合并：点「循环结束」也是编辑同一个循环的配置（配置存在循环开始上）
        if self._steps[idx].action == "loop_end":
            start_idx = loop_start_index(self._steps, idx)
            if start_idx >= 0:
                idx = start_idx
        old = self._steps[idx]
        dlg = StepEditDialog(
            self.project_dir, old, self,
            data_source=self.data_source,
            project_variables=self.project_variables,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_step = dlg.get_step()
            new_step.pos = old.pos        # 保留画布坐标
            self._steps[idx] = new_step
            self._commit(select_index=idx)

    def _delete_step(self):
        idx = self._selected_row()
        if idx < 0:
            return
        s = self._steps[idx]
        name = ACTION_META.get(s.action, (s.action, ""))[0]
        block = loop_block_at(self._steps, idx)
        if block:
            a, b = block
            body = b - a - 1
            reply = QMessageBox.question(
                self, "删除循环",
                f"「循环」是一个容器节点，删除会连同里面的 {body} 个步骤一起去掉。\n"
                f"确定删除这个循环吗？"
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            del self._steps[a:b + 1]
            self._commit(select_index=min(a, len(self._steps) - 1))
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
        block = loop_block_at(self._steps, idx)
        if block and idx in (block[0], block[1]):
            # 选中循环的任一端：整块上下移动
            a, b = block
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
        """任何改动都走这里：重排编号 → 写盘 → 刷新列表。"""
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
        """状态栏提示：已保存 / 循环没配对。"""
        try:
            build_segments(self._steps)
        except ValueError as e:
            self.status_label.setText(f"已自动保存；但{e}")
            self.status_label.setStyleSheet("color:#c62828;")
            return
        self.status_label.setText(
            f"已自动保存（{len(self._rows)} 个步骤，编号 1~{len(self._rows)}）"
        )
        self.status_label.setStyleSheet("color:#2e7d32;")

    @property
    def changed(self) -> bool:
        """弹窗里是否改动过（调用方据此决定要不要刷新画布）。"""
        return self._changed

    def get_steps(self) -> List[Step]:
        """返回编辑后的步骤（已按 1 起重新编号，且已写盘）。"""
        return self._steps
