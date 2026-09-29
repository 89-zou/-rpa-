# -*- coding: utf-8 -*-
"""主窗口的【AI 设置】标签页：一个 API 密钥管全程序。

用法就一句话：**把 API 密钥粘进去**。程序会拿它挨家去问「你是哪家的」，
认出来自动填好接口地址，并把那家**真实可用的模型名**列出来让你挑；
认不出来也不要紧，下面那张表里点【用这家】手填即可（各家都是 OpenAI 兼容接口）。

去哪注册拿 Key：下面那张表的【拿去注册】按钮，点一下用系统浏览器打开对应页面。

密钥存在本机数据目录的 `ai_settings.json`（跟 projects/ 放一起）。
"""
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from rpa_page_agent.core.page_agent import config as pa_config
from rpa_page_agent.core.page_agent import llm, providers


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


class _IdentifyThread(QThread):
    """后台识别：粘完 Key 别把界面冻住（最多要试十几家）。"""

    done = pyqtSignal(dict)
    progress = pyqtSignal(str)

    def __init__(self, api_key: str, parent=None):
        super().__init__(parent)
        self.api_key = api_key

    def run(self):
        try:
            result = providers.identify(self.api_key, on_progress=self.progress.emit)
        except Exception as e:                 # 兜底：线程里绝不让异常跑出去
            result = {"ok": False, "provider": None, "models": [], "tried": [],
                      "error": f"{type(e).__name__}: {e}"}
        self.done.emit(result)


class _PingThread(QThread):
    """后台发一次最小请求（【试一下】）。"""

    done = pyqtSignal(bool, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)

    def run(self):
        try:
            ok, message = llm.ping(self.cfg)
        except Exception as e:
            ok, message = False, f"{type(e).__name__}: {e}"
        self.done.emit(ok, message)


class AiSettingsTab(QWidget):
    """AI 设置标签页（改哪儿存哪儿，不用点保存）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._identify: Optional[_IdentifyThread] = None
        self._ping: Optional[_PingThread] = None
        self._provider: Dict = {}
        self._silent = False                   # 刷界面时别触发自动保存

        root = QVBoxLayout(self)

        title = QLabel("AI 设置")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "把 API 密钥粘到下框就行 —— 程序会自己认出是哪一家、自动填好接口地址，"
            "并把你能用的模型列出来。\n"
            "密钥只存在本机（" + str(pa_config.settings_file()) + "），"
            "不会进网页、不会写进运行日志。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #64748b;")
        root.addWidget(tip)

        # ---- 密钥行 ----
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(QLabel("API 密钥："))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘进来即可，例如 sk-xxxxxxxx")
        key_layout.addWidget(self.key_edit, 1)
        self.btn_show = QPushButton("显示")
        self.btn_show.setCheckable(True)
        self.btn_show.setFixedWidth(56)
        self.btn_show.toggled.connect(self._toggle_show)
        key_layout.addWidget(self.btn_show)
        self.btn_identify = QPushButton("自动识别")
        self.btn_identify.setToolTip("拿这个 Key 挨家问一遍（并发试探，十几秒）")
        self.btn_identify.clicked.connect(lambda: self._start_identify(force=True))
        key_layout.addWidget(self.btn_identify)
        root.addWidget(key_row)

        self.key_edit.textChanged.connect(self._on_key_changed)
        # 粘完停手 0.8 秒就自动识别一次，不用非得点按钮
        self._key_timer = QTimer(self)
        self._key_timer.setSingleShot(True)
        self._key_timer.setInterval(800)
        self._key_timer.timeout.connect(lambda: self._start_identify(force=False))

        # ---- 服务商 / 接口地址 / 模型 ----
        self.who_label = QLabel("还没配")
        self.who_label.setOpenExternalLinks(False)
        self.who_label.linkActivated.connect(self._open_link)
        self.who_label.setWordWrap(True)
        self._add_row(root, "服务商：", self.who_label)

        url_row = QWidget()
        url_layout = QHBoxLayout(url_row)
        url_layout.setContentsMargins(0, 0, 0, 0)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https:// 开头的 OpenAI 兼容接口地址")
        url_layout.addWidget(self.base_url_edit, 1)
        self.btn_models = QPushButton("刷新模型")
        self.btn_models.setToolTip("按这个接口地址再拉一次模型清单")
        self.btn_models.clicked.connect(self._reload_models)
        url_layout.addWidget(self.btn_models)
        self._add_row(root, "接口地址：", url_row)

        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)     # 列表里没有的模型名也能手打
        self.model_combo.setMinimumWidth(320)
        self.model_combo.setToolTip("智能任务实际用的模型；可以下拉选，也可以自己敲")
        model_layout.addWidget(self.model_combo, 1)
        self.btn_ping = QPushButton("试一下")
        self.btn_ping.setToolTip("发一次最小请求，确认地址 / 模型 / 密钥能通")
        self.btn_ping.clicked.connect(self._on_ping)
        model_layout.addWidget(self.btn_ping)
        self._add_row(root, "模型：", model_row)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        root.addWidget(self.state_label)

        # ---- 服务商清单 ----
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #e2e8f0;")
        root.addWidget(line)
        table_tip = QLabel("没有 Key？点【拿去注册】去对应平台注册；"
                           "自动识别没成功时，点【用这家】把接口地址填上，再手填模型名。")
        table_tip.setWordWrap(True)
        table_tip.setStyleSheet("color: #64748b;")
        root.addWidget(table_tip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        head = ("服务商", "接口地址", "", "")
        for col, text in enumerate(head):
            label = QLabel(text)
            label.setStyleSheet("color: #94a3b8;")
            if col == 0:
                label.setMinimumWidth(150)
            grid.addWidget(label, 0, col)
        for row, prov in enumerate(providers.all_providers(), start=1):
            name = QLabel(prov["name"])
            if prov.get("note"):
                name.setToolTip(prov["note"])
            name.setMinimumWidth(150)
            grid.addWidget(name, row, 0)
            url = QLabel(prov["base_url"])
            url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            url.setStyleSheet("color: #475569;")
            grid.addWidget(url, row, 1)
            btn_signup = QPushButton("拿去注册")
            btn_signup.setFixedWidth(88)
            btn_signup.clicked.connect(
                lambda _=False, p=prov: self._open_link(p["signup"]))
            grid.addWidget(btn_signup, row, 2)
            btn_use = QPushButton("用这家")
            btn_use.setFixedWidth(72)
            btn_use.clicked.connect(lambda _=False, p=prov: self._use_provider(p))
            grid.addWidget(btn_use, row, 3)
        grid.setColumnStretch(1, 1)
        scroll.setWidget(box)
        root.addWidget(scroll, 1)

        # ---- 自动保存 ----
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_now)
        self.key_edit.textChanged.connect(self._queue_save)
        self.base_url_edit.textChanged.connect(self._queue_save)
        self.model_combo.currentTextChanged.connect(self._queue_save)

        self.reload()

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _add_row(self, root, label: str, widget: QWidget):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        text = QLabel(label)
        text.setFixedWidth(72)
        layout.addWidget(text)
        layout.addWidget(widget, 1)
        root.addWidget(row)

    def _open_link(self, url: str):
        QDesktopServices.openUrl(QUrl(str(url)))

    def _toggle_show(self, shown: bool):
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                  else QLineEdit.EchoMode.Password)
        self.btn_show.setText("隐藏" if shown else "显示")

    # ------------------------------------------------------------------
    # 读 / 写
    # ------------------------------------------------------------------
    def reload(self):
        """从设置文件刷一遍界面（切到这个标签页、或别的窗口改过配置时调）。"""
        cfg = pa_config.load()
        self._silent = True
        self.key_edit.setText(cfg["api_key"])
        self.base_url_edit.setText(cfg["base_url"])
        self._provider = providers.provider_by_base(cfg["base_url"]) or {}
        items = [m for m in self._current_items()]
        if cfg["model"] and cfg["model"] not in items:
            items.insert(0, cfg["model"])
        self._fill_models(items, cfg["model"])
        self._silent = False
        self._refresh_state()

    def _current_items(self) -> List[str]:
        return [self.model_combo.itemText(i) for i in range(self.model_combo.count())]

    def _fill_models(self, models: List[str], pick: str = ""):
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if pick:
            self.model_combo.setCurrentText(pick)
        elif models:
            self.model_combo.setCurrentIndex(0)
        self.model_combo.blockSignals(False)

    def _queue_save(self, *_):
        """界面改了就排一次延时保存（刷界面时别排，免得盖掉刚显示的识别结果）。"""
        if not self._silent:
            self._save_timer.start()

    def _save_now(self):
        if self._silent:
            return
        base = self.base_url_edit.text().strip()
        old = pa_config.load_global()
        # 认出来的名字优先；手填地址（不在清单里）时，只要地址没变就保留原来的名字
        name = self._provider.get("name") or (
            str(old.get("provider") or "")
            if str(old.get("base_url") or "") == base else "")
        pa_config.save_global(self.base_url_edit.text(), self.key_edit.text(),
                              self.model_combo.currentText(), name)
        self._refresh_state()

    def current_config(self) -> dict:
        """当前界面上的配置（执行器/编辑器要用到的那份形状）。"""
        return {"base_url": self.base_url_edit.text().strip(),
                "api_key": self.key_edit.text().strip(),
                "model": self.model_combo.currentText().strip(),
                "provider": self._provider.get("name") or "",
                "language": pa_config.DEFAULT_LANGUAGE,
                "max_steps": pa_config.DEFAULT_MAX_STEPS,
                "timeout_s": pa_config.DEFAULT_TIMEOUT_S}

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------
    def _on_key_changed(self, *_):
        if self._silent:
            return
        if self.key_edit.text().strip():
            self.who_label.setText("…写完就自动识别")
        self._key_timer.start()

    def _start_identify(self, force: bool = True):
        key = self.key_edit.text().strip()
        if not key:
            if force:
                QMessageBox.information(self, "先填密钥",
                                        "把 API 密钥粘进来；\n"
                                        "如果你用的是本机模型（Ollama / LM Studio），"
                                        "密钥留空、点下面表里的【用这家】即可。")
            return
        if self._identify and self._identify.isRunning():
            return
        self.btn_identify.setEnabled(False)
        self.btn_identify.setText("识别中…")
        self.state_label.setText("正在识别：拿这个 Key 挨家试（并发，十几秒）…")
        self.state_label.setStyleSheet("color: #475569;")
        self._identify = _IdentifyThread(key, self)
        self._identify.progress.connect(self.state_label.setText)
        self._identify.done.connect(self._on_identified)
        self._identify.start()

    def _on_identified(self, result: dict):
        self.btn_identify.setEnabled(True)
        self.btn_identify.setText("自动识别")
        if result.get("ok"):
            prov = result["provider"]
            models = list(result.get("models") or [])
            self._provider = prov
            self._silent = True
            self.base_url_edit.setText(prov["base_url"])
            keep = self.model_combo.currentText().strip()
            pick = keep if keep in models else (models[0] if models else "")
            self._fill_models(models, pick)
            self._silent = False
            self._save_now()
            self.state_label.setText(
                f"✓ 认出是「{prov['name']}」，可用模型 {len(models)} 个，"
                f"已选 {pick or '（一个都没列出来，自己敲一个）'}。"
                f"模型下拉里挑一个就行。")
            self.state_label.setStyleSheet("color: #0f766e;")
        else:
            self.state_label.setText("✗ " + str(result.get("error") or "识别失败"))
            self.state_label.setStyleSheet("color: #b45309;")

    def _use_provider(self, prov: dict):
        """手选一家：把接口地址填上（模型名按需自己敲或点【刷新模型】）。"""
        self._provider = prov
        self._silent = True
        self.base_url_edit.setText(prov["base_url"])
        self._silent = False
        self._save_now()
        if prov.get("local"):
            self._reload_models()
        else:
            self.state_label.setText(
                f"已把接口地址填成「{prov['name']}」。点【刷新模型】拉模型清单；"
                f"拉不到就照控制台上的模型名手敲一个。"
                + (f"（{prov['note']}）" if prov.get("note") else ""))
            self.state_label.setStyleSheet("color: #475569;")

    def _reload_models(self):
        url = self.base_url_edit.text().strip()
        if not url:
            QMessageBox.information(self, "先填接口地址", "接口地址是空的。")
            return
        key = self.key_edit.text().strip()
        models, why = providers.fetch_models(url, key)
        if not models:
            self.state_label.setText(f"✗ 没拉到模型清单：{why}")
            self.state_label.setStyleSheet("color: #b45309;")
            return
        keep = self.model_combo.currentText().strip()
        self._silent = True
        self._fill_models(models, keep if keep in models else models[0])
        self._silent = False
        self._save_now()
        self.state_label.setText(f"✓ 拉到 {len(models)} 个模型，下拉里挑一个。")
        self.state_label.setStyleSheet("color: #0f766e;")

    # ------------------------------------------------------------------
    # 试一下
    # ------------------------------------------------------------------
    def _on_ping(self):
        cfg = self.current_config()
        problem = pa_config.problem(cfg)
        if problem:
            QMessageBox.warning(self, "先补全设置", problem)
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

    # ------------------------------------------------------------------
    def _refresh_state(self):
        cfg = self.current_config()      # 以界面为准，别让「还没落盘的输入」显得没生效
        base = self.base_url_edit.text().strip()
        prov = self._provider or providers.provider_by_base(base) or {}
        if prov:
            text = f"{prov['name']}（<a href=\"{prov['signup']}\">去注册 / 拿 Key</a>）"
            if prov.get("note"):
                text += f"　{prov['note']}"
            self.who_label.setText(text)
        else:
            self.who_label.setText(base or "还没配")
        problem = pa_config.problem(cfg)
        if problem:
            self.state_label.setText("⚠ " + problem)
            self.state_label.setStyleSheet("color: #b45309;")
        else:
            self.state_label.setText(
                f"✓ 就绪：{pa_config.describe(cfg)}　（所有智能节点共用这份设置）")
            self.state_label.setStyleSheet("color: #0f766e;")
