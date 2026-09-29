# -*- coding: utf-8 -*-
"""「任务」节点的编辑面板：一句话描述 + 步数/超时 + 当前 AI 一览。

跟 CaptchaPanel / CollectPanel 一个套路：`load(step)` / `save(step)` / `validate()`，
外面（步骤编辑器）只管把它塞进表单并按动作显隐。

**AI（接口地址 / 模型 / 密钥）不在这儿改** —— 那是整个程序共用的一份配置，
入口在主窗口的【AI 设置】标签页（ui/ai_settings_tab.py）。这里只显示「现在用的是谁」，
省得同一份设置有两个地方能改。
"""
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QWidget,
)

from rpa_page_agent.core.page_agent import config as pa_config
from rpa_page_agent.ui.ai_settings_tab import open_hub


class PageAgentPanel(QWidget):
    """智能页面任务节点：任务描述 + 额外提示 + 步数/超时 + 当前 AI 一览。"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_dir: Optional[Path] = None
        self._def_steps = pa_config.DEFAULT_MAX_STEPS
        self._def_timeout = pa_config.DEFAULT_TIMEOUT_S

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.task_edit = QPlainTextEdit()
        self.task_edit.setPlaceholderText(
            "用一句话说清要做什么，例如：\n"
            "· 在标题框填「今天的天气」，在正文框填 {{文章.内容}}，然后点「发布」\n"
            "· 登录 saucedemo，用户名 standard_user，密码 secret_sauce\n"
            "页面内的 AI 会自己看页面、自己决定点哪儿；可以写 {{变量}}。")
        self.task_edit.setFixedHeight(96)
        self.task_edit.textChanged.connect(self._emit_changed)
        root.addRow("任务：", self.task_edit)

        hint_row = QWidget()
        hint_layout = QHBoxLayout(hint_row)
        hint_layout.setContentsMargins(0, 0, 0, 0)
        self.hints_edit = QLineEdit()
        self.hints_edit.setPlaceholderText("可选：给 AI 的线索，如「标题框是 #title」「发布按钮是蓝色那个」")
        self.hints_edit.textChanged.connect(self._emit_changed)
        hint_layout.addWidget(self.hints_edit, 1)
        root.addRow("额外提示：", hint_row)

        num_row = QWidget()
        num_layout = QHBoxLayout(num_row)
        num_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 60)
        self.steps_spin.setValue(pa_config.DEFAULT_MAX_STEPS)
        self.steps_spin.setSuffix(" 步")
        self.steps_spin.setToolTip("AI 最多走几步（一步＝一次「看页面 → 做一个动作」）；跑飞了它会自己停。")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 1800)
        self.timeout_spin.setValue(pa_config.DEFAULT_TIMEOUT_S)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setToolTip("整个任务最多跑多久；到点会停下来并报错。")
        num_layout.addWidget(QLabel("最多"))
        num_layout.addWidget(self.steps_spin)
        num_layout.addSpacing(12)
        num_layout.addWidget(QLabel("超时"))
        num_layout.addWidget(self.timeout_spin)
        num_layout.addStretch()
        root.addRow("步数 / 时间：", num_row)

        # ---- 当前用哪个 AI（只读；要改去主窗口的【AI 设置】）----
        ai_row = QWidget()
        ai_layout = QHBoxLayout(ai_row)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_label = QLabel("")
        self.ai_label.setWordWrap(True)
        ai_layout.addWidget(self.ai_label, 1)
        self.btn_ai = QPushButton("打开 AI 设置")
        self.btn_ai.setToolTip("接口地址 / 模型 / 密钥是整个程序共用一份，在那儿改")
        self.btn_ai.clicked.connect(open_hub().open_requested.emit)
        ai_layout.addWidget(self.btn_ai)
        root.addRow("当前 AI：", ai_row)

        self.reload()

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def set_project_dir(self, project_dir):
        self.project_dir = Path(project_dir) if project_dir else None

    def reload(self):
        """刷「当前 AI」那一行（新建节点、切项目、打开设置回来时都会调）。"""
        cfg = pa_config.load(self.project_dir)
        self._def_steps = int(cfg.get("max_steps") or pa_config.DEFAULT_MAX_STEPS)
        self._def_timeout = int(cfg.get("timeout_s") or pa_config.DEFAULT_TIMEOUT_S)
        problem = pa_config.problem(cfg)
        if problem:
            self.ai_label.setText("⚠ " + problem)
            self.ai_label.setStyleSheet("color: #b45309;")
        else:
            self.ai_label.setText(f"✓ {pa_config.describe(cfg)}（所有任务节点共用）")
            self.ai_label.setStyleSheet("color: #0f766e;")

    def load(self, step):
        self.task_edit.setPlainText(str(getattr(step, "agent_task", "") or ""))
        self.hints_edit.setText(str(getattr(step, "agent_hints", "") or ""))
        self.steps_spin.setValue(int(getattr(step, "agent_max_steps", 0) or
                                     pa_config.DEFAULT_MAX_STEPS))
        self.timeout_spin.setValue(int(getattr(step, "agent_timeout", 0) or
                                       pa_config.DEFAULT_TIMEOUT_S))
        self.reload()

    def save(self, step):
        step.agent_task = self.task_edit.toPlainText().strip()
        step.agent_hints = self.hints_edit.text().strip()
        step.agent_max_steps = int(self.steps_spin.value())
        step.agent_timeout = int(self.timeout_spin.value())

    def validate(self) -> Optional[str]:
        if not self.task_edit.toPlainText().strip():
            return ("「智能页面任务」要写一句任务描述（双击节点，在「任务」里写，"
                    "例如：在标题框填「今天天气」，然后点发布）")
        return None

    # ------------------------------------------------------------------
    def _emit_changed(self):
        self.changed.emit()
