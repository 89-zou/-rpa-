# -*- coding: utf-8 -*-
"""「任务」节点的编辑面板：一句话描述 + 步数/超时 + 项目级 LLM 设置。

跟 CaptchaPanel / CollectPanel 一个套路：`load(step)` / `save(step)` / `validate()`，
外面（步骤编辑器）只管把它塞进表单并按动作显隐。

LLM 的三件套（接口地址 / 模型 / 密钥）是**项目级**的：所有任务节点共用一份，
存在项目 steps.json 的 page_agent 键里（见 core/page_agent/config.py）。
密钥只在本机项目文件里 —— 页面内 agent 发 LLM 请求时会交回 Python 进程，
所以它永远不进页面。
"""
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QWidget,
)

from rpa_page_agent.core.page_agent import config as pa_config


class _PingThread(QThread):
    """后台试一次 LLM 接口（别把界面冻住）。"""

    done = pyqtSignal(bool, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)

    def run(self):
        from rpa_page_agent.core.page_agent import llm
        try:
            ok, message = llm.ping(self.cfg)
        except Exception as e:                       # 兜底：别让线程里抛出去
            ok, message = False, f"{type(e).__name__}: {e}"
        self.done.emit(ok, message)


class PageAgentPanel(QWidget):
    """智能页面任务节点：任务描述 + 额外提示 + 步数/超时 + LLM 设置。"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_dir: Optional[Path] = None
        self._lang = pa_config.DEFAULT_LANGUAGE
        self._def_steps = pa_config.DEFAULT_MAX_STEPS
        self._def_timeout = pa_config.DEFAULT_TIMEOUT_S
        self._ping: Optional[_PingThread] = None

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

        # ---- LLM 设置（项目级共用）----
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("选一个常用服务商…", "")
        for name, base, model in pa_config.PRESETS:
            self.preset_combo.addItem(f"{name}（{model}）", f"{base}|{model}")
        self.preset_combo.activated.connect(self._on_preset)
        root.addRow("服务商：", self.preset_combo)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.base_url_edit.textChanged.connect(self._emit_changed)
        root.addRow("接口地址：", self.base_url_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("qwen3.5-plus / deepseek-v4-flash / gpt-5.4-mini …")
        self.model_edit.textChanged.connect(self._emit_changed)
        root.addRow("模型：", self.model_edit)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…（只存在本项目里，不会进页面、不会进日志）")
        self.key_edit.setToolTip("存在项目目录的 steps.json 里；页面内 agent 发 LLM 请求时会交回本程序，"
                                 "所以密钥不会出现在网页上下文里。")
        self.key_edit.textChanged.connect(self._emit_changed)
        self.btn_ping = QPushButton("试一下")
        self.btn_ping.setToolTip("发一次最小请求，确认地址 / 模型 / 密钥能通")
        self.btn_ping.clicked.connect(self._on_ping)
        key_layout.addWidget(self.key_edit, 1)
        key_layout.addWidget(self.btn_ping)
        root.addRow("API 密钥：", key_row)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: #64748b;")
        root.addRow("", self.state_label)
        self._refresh_state()

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def set_project_dir(self, project_dir):
        self.project_dir = Path(project_dir) if project_dir else None

    def reload(self):
        """只把「项目级 LLM 设置」刷进界面（新建节点、切项目时用）。"""
        if self.project_dir:
            cfg = pa_config.load(self.project_dir)
        else:                       # 没有项目目录（理论上有）→ 给一份预设，别让界面空着
            _name, base, model = pa_config.PRESETS[0]
            cfg = {"base_url": base, "api_key": "", "model": model,
                   "language": pa_config.DEFAULT_LANGUAGE,
                   "max_steps": pa_config.DEFAULT_MAX_STEPS,
                   "timeout_s": pa_config.DEFAULT_TIMEOUT_S}
        self.base_url_edit.setText(str(cfg.get("base_url") or ""))
        self.model_edit.setText(str(cfg.get("model") or ""))
        self.key_edit.setText(str(cfg.get("api_key") or ""))
        self._lang = str(cfg.get("language") or pa_config.DEFAULT_LANGUAGE)
        self._def_steps = int(cfg.get("max_steps") or pa_config.DEFAULT_MAX_STEPS)
        self._def_timeout = int(cfg.get("timeout_s") or pa_config.DEFAULT_TIMEOUT_S)
        self._refresh_state()

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
        self._save_llm()

    def validate(self) -> Optional[str]:
        if not self.task_edit.toPlainText().strip():
            return ("「智能页面任务」要写一句任务描述（双击节点，在「任务」里写，"
                    "例如：在标题框填「今天天气」，然后点发布）")
        return None

    def current_config(self) -> dict:
        return {"base_url": self.base_url_edit.text().strip(),
                "api_key": self.key_edit.text().strip(),
                "model": self.model_edit.text().strip(),
                "language": self._lang,
                "max_steps": self._def_steps,
                "timeout_s": self._def_timeout}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _save_llm(self):
        if self.project_dir is None:
            return
        cfg = self.current_config()
        try:
            pa_config.save(self.project_dir, **cfg)
        except Exception:
            pass                    # 存配置失败不该拦住节点保存

    def _on_preset(self, index: int):
        data = self.preset_combo.itemData(index) or ""
        if "|" in data:
            base, model = data.split("|", 1)
            self.base_url_edit.setText(base)
            self.model_edit.setText(model)
            self._refresh_state()

    def _on_ping(self):
        cfg = self.current_config()
        problem = pa_config.problem(cfg)
        if problem:
            QMessageBox.warning(self, "先补全设置", problem)
            return
        self._save_llm()
        self.btn_ping.setEnabled(False)
        self.btn_ping.setText("测试中…")
        self._ping = _PingThread(cfg, self)
        self._ping.done.connect(self._on_ping_done)
        self._ping.start()

    def _on_ping_done(self, ok: bool, message: str):
        self.btn_ping.setEnabled(True)
        self.btn_ping.setText("试一下")
        self._refresh_state()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "接口测试", message)

    def _emit_changed(self):
        self._refresh_state()
        self.changed.emit()

    def _refresh_state(self):
        cfg = self.current_config()
        problem = pa_config.problem(cfg)
        if problem:
            self.state_label.setText("⚠ " + problem)
            self.state_label.setStyleSheet("color: #b45309;")
        else:
            self.state_label.setText(f"✓ 接口就绪（{cfg['model']}）——所有智能节点共用这份设置")
            self.state_label.setStyleSheet("color: #0f766e;")
