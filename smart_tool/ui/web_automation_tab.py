# -*- coding: utf-8 -*-
"""浏览器自动化标签页：项目管理 + 画布编排 + 运行/停止 + 日志。"""
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from smart_tool.core import blocks
from smart_tool.core.project_store import ProjectStore, Step, list_projects
from smart_tool.core.step_executor import (
    PauseHandle, StepExecutor, available_variables, check_variables,
)
from smart_tool.ui.flow_canvas import (
    DEFAULT_PLACEHOLDER, LAYOUT_VERSION, NO_PROJECT_PLACEHOLDER, FlowCanvas,
)
from smart_tool.ui.flow_editor_dialog import FlowEditorDialog
from smart_tool.ui.project_manager_dialog import ProjectManagerDialog
from smart_tool.ui.project_picker_dialog import (
    NewProjectDialog, ProjectPickerDialog,
)
from smart_tool.ui.step_editor_dialog import StepEditDialog


# 画布上方的常驻提示（连线操作时会被临时替换成提示）
CANVAS_HINT = "双击节点编辑｜右键增删改｜Ctrl+滚轮缩放｜框内节点：选中后点右边小箭头可连线"


class ExecutorWorker(QThread):
    """在后台线程执行 StepExecutor。"""
    log_signal = pyqtSignal(str)
    pause_signal = pyqtSignal(str, int)        # (提示, step_id)
    pause_resolved = pyqtSignal(str)           # auto/manual/abort，用于复位按钮

    def __init__(
        self,
        steps: List[Step],
        variables: dict,
        project_dir=None,
        headless: bool = False,
    ):
        super().__init__()
        self._pause_handle: Optional[PauseHandle] = None
        self._executor = StepExecutor(
            steps=steps,
            variables=variables,
            headless=headless,
            project_dir=project_dir,
            log=self.log_signal.emit,
            on_pause=self._on_pause,
            on_resume=self.pause_resolved.emit,
        )

    def _on_pause(self, step: Step) -> PauseHandle:
        """暂停回调：立即返回句柄，执行器自行轮询人工信号与页面信号。"""
        self._pause_handle = PauseHandle()
        self.pause_signal.emit(
            step.prompt or "请人工操作（如验证码），检测到完成后会自动继续", step.id
        )
        return self._pause_handle

    def resolve_pause(self, continue_run: bool):
        """主线程调用：人工下发 继续/终止。"""
        handle = self._pause_handle
        if not handle:
            return
        if continue_run:
            handle.manual_continue.set()
        else:
            handle.manual_abort.set()

    def stop(self):
        """请求停止（含解除可能的暂停）。"""
        self._executor.stop()
        self.resolve_pause(False)

    def run(self):
        try:
            self._executor.run()
        except Exception as e:
            self.log_signal.emit(f"执行异常: {e}")


class WebAutomationTab(QWidget):
    """浏览器自动化标签页。"""

    def __init__(self):
        super().__init__()
        self._worker: Optional[ExecutorWorker] = None
        self._current_store: Optional[ProjectStore] = None
        self._steps: List[Step] = []
        self._init_ui()
        # 启动不自动载入项目：避免读盘/排版拖慢界面，由用户点【载入项目…】
        self._clear_project()
        self._append_log("请点【新建项目…】创建，或【载入项目…】选择已有项目。")

    # ------------------------------
    # UI 构建
    # ------------------------------
    def _init_ui(self):
        layout = QVBoxLayout(self)

        # 项目栏：不自动载入项目（避免启动时卡界面），由用户显式选择
        proj_layout = QHBoxLayout()
        self.project_label = QLabel("当前项目：（未载入）")
        self.project_label.setStyleSheet("color: #444;")
        proj_layout.addWidget(self.project_label, 1)
        self.btn_new = QPushButton("新建项目…")
        self.btn_new.setToolTip("填名称和起始网址，创建后立即载入")
        self.btn_new.clicked.connect(self._open_new_project)
        proj_layout.addWidget(self.btn_new)
        self.btn_load = QPushButton("载入项目…")
        self.btn_load.setToolTip("从项目文件夹中选择要运行的项目")
        self.btn_load.clicked.connect(self._open_project_picker)
        proj_layout.addWidget(self.btn_load)
        self.btn_manage = QPushButton("项目管理…")
        self.btn_manage.setToolTip(
            "项目列表与变量清单都在这里管（「读取数据」节点负责变量从哪来）"
        )
        self.btn_manage.clicked.connect(self._open_project_manager)
        proj_layout.addWidget(self.btn_manage)
        layout.addLayout(proj_layout)

        # 编辑按钮条
        edit_layout = QHBoxLayout()
        self.btn_flow_edit = QPushButton("流程编辑…")
        self.btn_flow_edit.clicked.connect(self._open_flow_editor)
        edit_layout.addWidget(self.btn_flow_edit)
        self.btn_layout = QPushButton("自动排版")
        self.btn_layout.setToolTip("按横向蛇形重新排列所有节点（超出宽度自动换行）")
        self.btn_layout.clicked.connect(self._auto_layout)
        edit_layout.addWidget(self.btn_layout)
        edit_layout.addStretch()
        self.hint_label = QLabel(CANVAS_HINT)
        self.hint_label.setStyleSheet("color: #888;")
        edit_layout.addWidget(self.hint_label)
        layout.addLayout(edit_layout)

        # 编排画布
        self.canvas = FlowCanvas()
        self.canvas.selection_changed.connect(self._update_edit_buttons)
        self.canvas.node_activated.connect(self._edit_step_by_id)
        self.canvas.positions_changed.connect(self._persist_positions_only)
        self.canvas.auto_layout_applied.connect(self._on_auto_layout_applied)
        self.canvas.context_menu_requested.connect(self._show_canvas_menu)
        self.canvas.edges_changed.connect(self._persist_canvas_edges)
        self.canvas.connect_status.connect(self._on_connect_status)
        layout.addWidget(self.canvas, 3)

        # 运行栏
        run_layout = QHBoxLayout()
        self.btn_run = QPushButton("运行")
        self.btn_run.clicked.connect(self._run)
        run_layout.addWidget(self.btn_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        run_layout.addWidget(self.btn_stop)
        self.btn_continue = QPushButton("继续（人工处理完）")
        self.btn_continue.setEnabled(False)
        self.btn_continue.clicked.connect(self._resolve_pause_continue)
        run_layout.addWidget(self.btn_continue)
        self.btn_abort = QPushButton("终止（暂停中）")
        self.btn_abort.setEnabled(False)
        self.btn_abort.clicked.connect(self._resolve_pause_abort)
        run_layout.addWidget(self.btn_abort)
        run_layout.addStretch()
        # 日志折叠开关（︿ 收起 / ﹀ 展开）：默认收起，不占画布地方
        self._log_collapsed = True
        self.btn_log_toggle = QPushButton("﹀ 日志")
        self.btn_log_toggle.setToolTip("展开日志面板")
        self.btn_log_toggle.setFixedWidth(84)
        self.btn_log_toggle.clicked.connect(self._toggle_log)
        run_layout.addWidget(self.btn_log_toggle)
        layout.addLayout(run_layout)

        # 日志
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(160)
        self.log_text.setVisible(False)      # 默认收起
        layout.addWidget(self.log_text, 1)

    def _toggle_log(self):
        """收起/展开日志面板，按钮符号在 ︿ 与 ﹀ 之间切换。"""
        self._log_collapsed = not self._log_collapsed
        self.log_text.setVisible(not self._log_collapsed)
        if self._log_collapsed:
            self.btn_log_toggle.setText("﹀ 日志")
            self.btn_log_toggle.setToolTip("展开日志面板")
        else:
            self.btn_log_toggle.setText("︿ 日志")
            self.btn_log_toggle.setToolTip("收起日志面板")

    # ------------------------------
    # 项目新建 / 载入
    # ------------------------------
    def _open_new_project(self):
        """新建项目，建好后立即载入。"""
        if self._worker is not None:
            QMessageBox.warning(self, "提示", "执行进行中，请先停止再新建项目。")
            return
        dlg = NewProjectDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.created_store:
            self._append_log(f"已创建项目【{dlg.created_store.name}】")
            self._load_project(path=dlg.created_store.dir)

    def _open_project_picker(self):
        """从项目文件夹里选一个载入（也可浏览到别的文件夹）。"""
        if self._worker is not None:
            QMessageBox.warning(self, "提示", "执行进行中，请先停止再切换项目。")
            return
        current = self._current_store.dir if self._current_store else None
        dlg = ProjectPickerDialog(current, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen_path:
            self._load_project(path=dlg.chosen_path)

    def _load_project(self, name: Optional[str] = None, path=None) -> bool:
        """载入项目：path 优先（任意文件夹），否则按项目名在项目文件夹里找。"""
        store: Optional[ProjectStore] = None
        if path is not None:
            d = Path(path)
            if (d / "steps.json").exists():
                store = ProjectStore(d)
        elif name:
            store = next((p for p in list_projects() if p.name == name), None)

        if store is None:
            QMessageBox.warning(self, "提示", f"找不到项目：{path or name}")
            return False

        self._current_store = store
        self._steps = store.load_steps()
        # 旧版纵向排版、或新项目还没排过版 → 请求横向蛇形排版。
        # 画布尚未显示时它会挂起，等拿到真实宽度再排（避免列数过窄）。
        need_layout = (
            store.load_layout_version() != LAYOUT_VERSION
            or any(s.pos is None for s in self._steps)
        )
        self.canvas.load_steps(self._steps,
                               auto_layout=False, keep_view=False)
        # 画布上手动连的箭头（纯展示，随项目保存）
        self.canvas.set_manual_edges(store.load_canvas_edges())
        if need_layout:
            self.canvas.request_layout_when_ready()
        self.canvas.set_placeholder_text(DEFAULT_PLACEHOLDER)
        self.project_label.setText(f"当前项目：{store.name}")
        self._append_log(f"已载入项目【{store.name}】（{len(self._steps)} 步）")
        self._update_edit_buttons()
        return True

    def _on_connect_status(self, message: str):
        """画布连线操作的状态提示（已选起点 / 已连上 / 已删除…）。"""
        self.hint_label.setText(message)

    def _persist_canvas_edges(self):
        """手动连线改了 → 落盘。纯展示数据，不影响执行。"""
        if self._current_store:
            self._current_store.save_canvas_edges(self.canvas.manual_edges())

    def _clear_project(self):
        """卸载当前项目（未载入状态）。"""
        self._current_store = None
        self._steps = []
        self.canvas.set_manual_edges([])
        self.canvas.set_placeholder_text(NO_PROJECT_PLACEHOLDER)
        self.canvas.load_steps([])
        self.project_label.setText("当前项目：（未载入，请点【载入项目…】）")
        self._update_edit_buttons()

    def _on_auto_layout_applied(self):
        """自动排版完成（含延迟到首次显示后的那次）：写盘并记录排版版本。"""
        if self._current_store:
            self._current_store.save(self._steps,
                                     layout_version=LAYOUT_VERSION)

    def _open_project_manager(self):
        """打开【项目管理】：项目列表 + 变量清单。"""
        if self._worker is not None:
            QMessageBox.warning(self, "提示", "执行进行中，请先停止再管理项目。")
            return
        current = self._current_store.name if self._current_store else ""
        dlg = ProjectManagerDialog(current, self)
        dlg.exec()
        if dlg.open_project_name:
            self._load_project(name=dlg.open_project_name)
        elif self._current_store and not self._current_store.steps_file.exists():
            # 当前项目刚被删掉了
            self._append_log("当前项目已被删除，请重新载入项目。")
            self._clear_project()

    # ------------------------------
    # 选择 / 按钮状态
    # ------------------------------
    def _selected_row(self) -> int:
        sid = self.canvas.selected_step_id()
        if sid is None:
            return -1
        for i, s in enumerate(self._steps):
            if s.id == sid:
                return i
        return -1

    def _update_edit_buttons(self):
        """按项目/运行状态切换按钮。"""
        editable = self._worker is None and self._current_store is not None
        self.btn_flow_edit.setEnabled(editable)
        self.btn_layout.setEnabled(editable)
        self.btn_new.setEnabled(self._worker is None)
        self.btn_load.setEnabled(self._worker is None)
        self.btn_run.setEnabled(self._worker is None and editable)

    def _require_project(self) -> bool:
        if not self._current_store:
            QMessageBox.warning(self, "提示",
                                "请先点【载入项目…】选择项目。")
            return False
        return True

    # ------------------------------
    # 变量来源检查
    # ------------------------------
    def available_variables(self) -> List[str]:
        """当前项目可用的变量：自定义变量 + 读取节点产出的 + 循环运行时。"""
        return available_variables(
            self._steps,
            self._current_store.load_variables() if self._current_store else {},
        )

    def _check_variables_before_run(self) -> bool:
        """运行前检查变量是否有来源，避免"跑起来才发现正文是空的"。"""
        problems = check_variables(
            self._steps,
            project_variables=(self._current_store.load_variables()
                               if self._current_store else {}),
        )
        if not problems:
            return True
        for p in problems:
            self._append_log(f"变量检查：{p}")
        reply = QMessageBox.warning(
            self, "变量可能没有内容",
            "检测到以下变量当前没有来源，运行时会被替换成空值或占位文字：\n\n"
            + "\n".join(f"· {p}" for p in problems[:8])
            + ("\n…" if len(problems) > 8 else "")
            + "\n\n建议先双击相关节点补全配置（「读取数据」节点产出变量、"
              "循环里才能用 {{loop.item.字段}}），或忽略本次提示继续运行。\n"
              "仍要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    # ------------------------------
    # 流程编辑弹窗（增/插/改/删/排序）
    # ------------------------------
    def _open_flow_editor(self):
        """打开列表式流程编辑弹窗；弹窗内每一步改动都已即时落盘，这里只刷新画布。"""
        if not self._require_project():
            return
        if self._worker is not None:
            QMessageBox.warning(self, "提示", "执行进行中，请先停止再编辑流程。")
            return
        dlg = FlowEditorDialog(
            self._current_store.dir, self._steps, self,
            project_variables=self._current_store.load_variables(),
        )
        dlg.exec()
        if dlg.changed:
            self._steps = dlg.get_steps()
            self._persist(auto_layout=True)
            self._append_log("流程已更新（改动已自动保存）。")

    def _auto_layout(self):
        """手动触发横向蛇形重排（落盘由 auto_layout_applied 信号负责）。"""
        if not self._require_project() or self._worker is not None:
            return
        if not self._steps:
            return
        self.canvas.apply_auto_layout()
        self._append_log("已按横向蛇形重新排版。")

    def _make_step_dialog(self, step: Optional[Step] = None) -> StepEditDialog:
        """统一构造步骤编辑对话框，带上可用变量供变量下拉使用。"""
        return StepEditDialog(
            self._current_store.dir, step, self,
            variable_names=self.available_variables(),
        )

    # ------------------------------
    # 步骤增删改 / 排序（画布右键菜单使用）
    # ------------------------------
    def _persist(self, select_row: Optional[int] = None,
                 auto_layout: Optional[bool] = None):
        """统一保存：id 重排 → 写盘 → 重建画布。

        结构变化（增/删/移动）传 auto_layout=True，让画布重新横向排列，
        避免新节点与旧节点重叠；仅改内容时保持原有位置。
        """
        if not self._current_store:
            return
        blocks.normalize_branch_lists(self._steps)
        for i, s in enumerate(self._steps, start=1):
            s.id = i
        self._current_store.save(self._steps)
        select_id = None
        if select_row is not None and 0 <= select_row < len(self._steps):
            select_id = self._steps[select_row].id
        self.canvas.load_steps(self._steps,
                               select_id=select_id, auto_layout=auto_layout)
        self._update_edit_buttons()

    def _persist_positions_only(self):
        """拖拽结束：只写盘，不重建画布。"""
        if self._current_store:
            self._current_store.save(self._steps)

    def _add_step(self):
        """在选中步骤之后追加（右键菜单入口）；没选中则追加到末尾。"""
        if not self._require_project():
            return
        row = self._selected_row()
        dlg = self._make_step_dialog()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            step = dlg.get_step()
            pos = row + 1 if row >= 0 else len(self._steps)
            self._insert_at(pos, step)

    def _insert_step(self):
        """在选中步骤之前插入；未选中时追加（右键菜单入口）。"""
        if not self._require_project():
            return
        row = self._selected_row()
        dlg = self._make_step_dialog()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._insert_at(row if row >= 0 else len(self._steps),
                            dlg.get_step())

    def _insert_at(self, pos: int, step: Step):
        """插入步骤；新增「循环」/「条件」时自动补上配套的结构节点。"""
        new_steps = [step]
        if step.action == "loop_start":
            new_steps.append(Step(id=0, action=blocks.LOOP_END))
        elif step.action == "condition_start":
            if not step.cond_branches:
                step.cond_branches = [blocks.new_branch("分支 1"),
                                      blocks.new_branch("分支 2")]
            new_steps.append(Step(id=0, action=blocks.BRANCH))
            new_steps.append(Step(id=0, action=blocks.BRANCH))
            new_steps.append(Step(id=0, action=blocks.COND_END))
        self._steps[pos:pos] = new_steps
        self._persist(select_row=pos, auto_layout=True)

    def _edit_selected_step(self):
        row = self._selected_row()
        if row >= 0:
            self._edit_step_by_id(self._steps[row].id)

    def _edit_step_by_id(self, step_id: int):
        """双击节点/点编辑：保留画布位置。

        「循环结束」「分支」「条件结束」的设置与所属块的配置节点合并成一份，
        点它们也是编辑那个块。
        """
        row = next((i for i, s in enumerate(self._steps) if s.id == step_id), -1)
        if row < 0:
            return
        row = blocks.marker_owner_index(self._steps, row)
        old = self._steps[row]
        dlg = self._make_step_dialog(old)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_step = dlg.get_step()
        new_step.pos = old.pos
        if old.action == "condition_start" and new_step.action == "condition_start":
            note = blocks.apply_condition_edit(self._steps, row, new_step)
            self._persist(select_row=row)
            if note:
                self._append_log(f"条件改动：{note}")
            return
        self._steps[row] = new_step
        self._persist(select_row=row)

    def _delete_selected_step(self):
        row = self._selected_row()
        if row < 0:
            return
        block = blocks.span_by_marker(blocks.spans(self._steps), row)
        if block is not None:
            # 只有选中块的「标记」才整块删；块里的普通步骤只删自己
            cn = blocks.ACTION_CN.get(self._steps[row].action, "结构节点")
            reply = QMessageBox.question(
                self, f"删除{cn}",
                f"「{cn}」是配套的结构节点，删除会连同里面的 "
                f"{block.end - block.start - 1} 个步骤一起去掉。\n确定删除吗？",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            blocks.drop_branch_entry(self._steps, block.start)
            del self._steps[block.start:block.end + 1]
            self._persist(select_row=min(block.start, len(self._steps) - 1),
                          auto_layout=True)
            return
        step = self._steps[row]
        reply = QMessageBox.question(
            self, "删除步骤", f"确定删除步骤 {step.id}（{step.action}）？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._steps.pop(row)
        self._persist(select_row=min(row, len(self._steps) - 1),
                      auto_layout=True)

    def _move_bounds(self, idx: int) -> Tuple[int, int]:
        """该步骤允许上下移动到的下标范围（不许跨出自己所在的块）。"""
        return blocks.move_bounds(self._steps, idx)

    def _move_selected(self, delta: int):
        row = self._selected_row()
        if row < 0:
            return
        block = blocks.span_by_marker(blocks.spans(self._steps), row)
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
            self._persist(select_row=target, auto_layout=True)
            return
        lo, hi = self._move_bounds(row)
        target = row + delta
        if not (lo <= target <= hi):
            return
        # 交换执行顺序后重排，保证连线不再交叉
        self._steps[row], self._steps[target] = self._steps[target], self._steps[row]
        self._persist(select_row=target, auto_layout=True)

    # ------------------------------
    # 画布右键菜单
    # ------------------------------
    def _show_canvas_menu(self, step_id, global_pos):
        if not self._require_project() or self._worker is not None:
            return
        menu = QMenu(self)
        if step_id is None:
            act_add = QAction("新增步骤", self)
            act_add.triggered.connect(self._add_step)
            menu.addAction(act_add)
        else:
            row = next((i for i, s in enumerate(self._steps) if s.id == step_id), -1)
            if row < 0:
                return

            def act(text, slot, enabled=True):
                a = QAction(text, self)
                a.setEnabled(enabled)
                a.triggered.connect(slot)
                menu.addAction(a)
                return a

            act("编辑", self._edit_selected_step)
            act("在此之前插入", self._insert_step)
            act("删除", self._delete_selected_step)
            menu.addSeparator()
            block = blocks.span_by_marker(blocks.spans(self._steps), row)
            if block is not None:
                lo, hi = block.start, block.end   # 选中块的标记：整块上下移动
            else:
                lo, hi = self._move_bounds(row)
            act("上移", lambda: self._move_selected(-1), row > lo)
            act("下移", lambda: self._move_selected(1), row < hi)
        menu.exec(global_pos)

    # ------------------------------
    # 运行/停止
    # ------------------------------
    def _run(self):
        if not self._current_store:
            QMessageBox.warning(self, "提示", "请先点【载入项目…】选择项目。")
            return
        if not self._steps:
            QMessageBox.warning(self, "提示", "当前项目没有步骤。")
            return
        # 变量来源检查：避免"到时正文没有内容"
        if not self._check_variables_before_run():
            self._append_log("已取消运行（变量检查未通过）。")
            return
        # 运行前把画布上的位置等落盘
        self._current_store.save(self._steps)
        variables = self._current_store.load_variables()
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._worker = ExecutorWorker(
            self._steps, variables,
            project_dir=self._current_store.dir,
            headless=False,
        )
        self._worker.log_signal.connect(self._append_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.pause_signal.connect(self._on_pause)
        self._worker.pause_resolved.connect(self._on_pause_resolved)
        self._worker.start()
        self.canvas.set_readonly(True)
        self._update_edit_buttons()

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_finished(self):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_continue.setEnabled(False)
        self.btn_abort.setEnabled(False)
        self._worker = None
        self.canvas.set_readonly(False)
        self._update_edit_buttons()

    def _on_pause(self, prompt: str, step_id: int):
        self._append_log(
            f"暂停 [步骤 {step_id}] {prompt} —— 满足恢复条件会自动继续，"
            f"也可手动点【继续】或【终止】"
        )
        self.btn_continue.setEnabled(True)
        self.btn_abort.setEnabled(True)
        self.window().raise_()
        self.window().activateWindow()

    def _on_pause_resolved(self, reason: str):
        tip = {
            "auto": "已自动恢复",
            "manual": "人工继续",
            "abort": "已人工终止",
        }.get(reason, reason)
        self._append_log(f"暂停解除：{tip}")
        self.btn_continue.setEnabled(False)
        self.btn_abort.setEnabled(False)

    def _resolve_pause_continue(self):
        if self._worker:
            self._worker.resolve_pause(True)
        self.btn_continue.setEnabled(False)
        self.btn_abort.setEnabled(False)

    def _resolve_pause_abort(self):
        if self._worker:
            self._worker.resolve_pause(False)
        self.btn_continue.setEnabled(False)
        self.btn_abort.setEnabled(False)

    # ------------------------------
    # 日志
    # ------------------------------
    def _append_log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {message}")
