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
    QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from rpa_page_agent.core import project_context
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

        # ---- 项目资料（自动读，运行时一并交给 AI）----
        ctx_row = QWidget()
        ctx_layout = QHBoxLayout(ctx_row)
        ctx_layout.setContentsMargins(0, 0, 0, 0)
        self.context_box = QPlainTextEdit()
        self.context_box.setReadOnly(True)
        self.context_box.setFixedHeight(104)
        self.context_box.setToolTip(
            "程序自动从本项目读出来的：变量清单 / 图片库 / 登录态 / 采集数据 / 函数库。\n"
            "跑这一步时会连同任务描述一起交给页面里的 AI，所以你可以直接说\n"
            "「用 {{标题}} 当标题」，不用再解释这些名字是什么。\n"
            "这段文字会随任务发给模型（你配的 DeepSeek），但**只给名字不给值**：\n"
            "变量值、采集到的记录内容都不进模型 —— AI 写下 {{名字}}，"
            "程序在真正输入的那一刻才替换成真值。")
        self.context_box.setStyleSheet("color: #475569;")
        ctx_layout.addWidget(self.context_box, 1)
        btn_row = QWidget()
        btn_col = QVBoxLayout(btn_row)
        btn_col.setContentsMargins(0, 0, 0, 0)
        self.btn_context = QPushButton("刷新")
        self.btn_context.setToolTip("重新读一遍项目里的这些资料")
        self.btn_context.clicked.connect(self._refresh_context)
        btn_col.addWidget(self.btn_context)
        btn_col.addStretch()
        ctx_layout.addWidget(btn_row)
        root.addRow("项目资料：", ctx_row)

        self.allow_values_box = QCheckBox(
            "把变量值也给 AI 看（默认不给：只给名字，值由程序在输入时替换）")
        self.allow_values_box.setToolTip(
            "不勾（推荐）：资料里只列变量名字，AI 要填值就写 {{名字}}，"
            "程序在真正敲进输入框之前才替换成真值 —— 密码、采集到的资料不会发给模型。\n"
            "勾上：这一步把真值也交给 AI（任务描述里的 {{变量}} 也会先替换）。\n"
            "什么时候需要它：要 AI 读懂内容本身，比如「把 {{文章.内容}} 改写成 100 字」、"
            "「按 {{订单状态}} 决定点哪个按钮」。\n"
            "注意：勾上之后这些值会随任务发给 DeepSeek（也就是你的 API 密钥那边）。")
        self.allow_values_box.toggled.connect(lambda _=False: self._refresh_context())
        root.addRow("", self.allow_values_box)

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

    def _refresh_context(self):
        """重读项目资料（变量清单 / 图片库 / 登录态 / 采集数据 / 函数库）。

        这里给的是**清单里的静态值**（编辑时看不到运行时的真实数据），
        跑流程时执行器会拿真值重读一遍 —— 两份内容是一套代码出的，格式一致。
        """
        if not self.project_dir:
            self.context_box.setPlainText("（还没打开项目）")
            return
        try:
            # 勾了「给 AI 看值」→ 预览里也带真值，所见即 AI 所得
            live = self.allow_values_box.isChecked()
            data = project_context.collect(self.project_dir, with_values=live)
            counts = "、".join(f"{k} {v}" for k, v in (data.get("counts") or {}).items()
                               if v)
            self.context_box.setPlainText(
                (data.get("text") or "（这个项目里还没有可读的资料）")
                + (f"\n\n—— 小结：{counts}" if counts else ""))
        except Exception as e:                 # 读资料失败不该拦住编辑节点
            self.context_box.setPlainText(f"（读不出来：{type(e).__name__}: {e}）")

    def reload(self):
        """刷「项目资料」和「当前 AI」（新建节点、切项目、打开设置回来时都会调）。"""
        self._refresh_context()
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
        self.allow_values_box.setChecked(bool(getattr(step, "agent_allow_values", False)))
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
        step.agent_allow_values = bool(self.allow_values_box.isChecked())

    def validate(self) -> Optional[str]:
        if not self.task_edit.toPlainText().strip():
            return ("「智能页面任务」要写一句任务描述（双击节点，在「任务」里写，"
                    "例如：在标题框填「今天天气」，然后点发布）")
        return None

    # ------------------------------------------------------------------
    def _emit_changed(self):
        self.changed.emit()
