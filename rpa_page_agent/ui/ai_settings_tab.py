# -*- coding: utf-8 -*-
"""主窗口的【AI 设置】标签页：填一个 DeepSeek 的 API 密钥就完事。

模型和接口地址都是程序里定好的（deepseek-v4-flash / api.deepseek.com），
用户不用选服务商、不用填地址、不用挑模型 —— 只把密钥粘进来。

还没有 Key？页面上的链接点一下就跳到 DeepSeek 开放平台去注册。
"""
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from rpa_page_agent.core.page_agent import config as pa_config
from rpa_page_agent.core.page_agent import llm


class _OpenHub(QObject):
    """「打开 AI 设置」的全局信号：步骤编辑器（在弹窗里）也能把主窗口切到这个标签页。

    为什么要绕一下：节点编辑器是独立的对话框，拿不到主窗口的标签页控件，
    用一个小信号中枢比自己一层层往上找 parent 干净。
    """

    open_requested = pyqtSignal()


_HUB: Optional[_OpenHub] = None


def open_hub() -> _OpenHub:
    """拿（必要时创建）那个信号中枢。"""
    global _HUB
    if _HUB is None:
        _HUB = _OpenHub()
    return _HUB


class _PingThread(QThread):
    """后台发一次最小请求（【试一下】），别把界面冻住。"""

    done = pyqtSignal(bool, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)

    def run(self):
        try:
            ok, message = llm.ping(self.cfg)
        except Exception as e:                 # 兜底：线程里绝不让异常跑出去
            ok, message = False, f"{type(e).__name__}: {e}"
        self.done.emit(ok, message)


class AiSettingsTab(QWidget):
    """AI 设置标签页（改完自动存，不用点保存）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ping: Optional[_PingThread] = None
        self._silent = False                   # 刷界面时别触发自动保存

        root = QVBoxLayout(self)

        title = QLabel("AI 设置")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "智能任务节点用 DeepSeek 干活，你只要填一个 API 密钥就行 —— "
            f"模型（{pa_config.MODEL}）和接口地址程序里都定好了。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #64748b;")
        root.addWidget(tip)

        # ---- 密钥行 ----
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("API 密钥：")
        label.setFixedWidth(72)
        key_layout.addWidget(label)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…（粘进来即可）")
        key_layout.addWidget(self.key_edit, 1)
        self.btn_show = QPushButton("显示")
        self.btn_show.setCheckable(True)
        self.btn_show.setFixedWidth(56)
        self.btn_show.toggled.connect(self._toggle_show)
        key_layout.addWidget(self.btn_show)
        self.btn_ping = QPushButton("试一下")
        self.btn_ping.setToolTip("发一次最小请求，确认这个密钥能用")
        self.btn_ping.clicked.connect(self._on_ping)
        key_layout.addWidget(self.btn_ping)
        root.addWidget(key_row)

        # ---- 去哪拿 Key ----
        where = QLabel(
            f"还没有密钥？<a href=\"{pa_config.PROVIDER_HOME}\">"
            f"去 DeepSeek 开放平台注册 / 拿 Key →</a>")
        where.setOpenExternalLinks(False)
        where.linkActivated.connect(self._open_link)
        root.addWidget(where)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        root.addWidget(self.state_label)

        note = QLabel(
            f"密钥只存在本机：{pa_config.settings_file()}\n"
            "不会进网页、不会写进运行日志（页面里的 AI 要调模型时，"
            "请求由本程序代发）。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #94a3b8;")
        root.addWidget(note)
        root.addStretch(1)

        # ---- 改哪儿存哪儿 ----
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_now)
        self.key_edit.textChanged.connect(self._queue_save)

        self.reload()

    # ------------------------------------------------------------------
    def _open_link(self, url: str):
        QDesktopServices.openUrl(QUrl(str(url)))

    def _toggle_show(self, shown: bool):
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                  else QLineEdit.EchoMode.Password)
        self.btn_show.setText("隐藏" if shown else "显示")

    def reload(self):
        """从设置文件刷一遍界面（切到这个标签页、或别处改过配置时调）。"""
        self._silent = True
        self.key_edit.setText(pa_config.load_key())
        self._silent = False
        self._refresh_state()

    def current_config(self) -> dict:
        """当前配置（形状跟执行器要的那份一致）。"""
        return pa_config.load()

    def _queue_save(self, *_):
        if not self._silent:
            self._save_timer.start()

    def _save_now(self):
        if self._silent:
            return
        pa_config.save_key(self.key_edit.text())
        self._refresh_state()

    # ------------------------------------------------------------------
    def _on_ping(self):
        cfg = pa_config.load()
        if pa_config.problem(cfg):
            QMessageBox.warning(self, "先填密钥", pa_config.problem(cfg))
            return
        self._save_now()
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

    def _refresh_state(self):
        cfg = pa_config.load()          # 以文件为准（密钥是唯一可改的东西）
        problem = pa_config.problem(cfg)
        if problem:
            self.state_label.setText("⚠ " + problem)
            self.state_label.setStyleSheet("color: #b45309;")
        else:
            self.state_label.setText(
                f"✓ 就绪：{pa_config.describe(cfg)}　（所有智能节点共用这个密钥）")
            self.state_label.setStyleSheet("color: #0f766e;")
