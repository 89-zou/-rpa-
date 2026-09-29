# -*- coding: utf-8 -*-
"""「用哪个浏览器」的设置窗口（项目级，从「打开网页」节点上点进来）。

两条路：
· 程序自带内核（默认）：Playwright 下载的 Chromium，内核在数据目录的「浏览器/」里；
· 用我自己装的 Edge / Chrome：启动一个**独立配置目录**的窗口 + 调试端口，
  由程序连上去（登录一次就长期记住，也不打扰你日常那个浏览器）。

为什么用真浏览器要独立配置目录：新版 Chrome/Edge 出于安全**不允许**拿你日常在用的
配置目录开调试端口（会直接忽略这个参数）；独立目录是唯一稳定的做法。
"""
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QWidget,
)

from rpa_page_agent.core import browser_launcher as bl
from rpa_page_agent.core.project_store import ProjectStore


class BrowserSettingsDialog(QDialog):
    """改完点【保存】写回项目的 steps.json（不影响别的设置）。"""

    def __init__(self, project_dir, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir) if project_dir else None
        self.setWindowTitle("浏览器设置")
        self.setMinimumWidth(620)
        cfg = (ProjectStore(self.project_dir).load_browser()
               if self.project_dir else
               {"use_real_browser": False, "browser_path": "", "debug_port": 9222,
                "type_url": False, "keep_open": False})

        root = QFormLayout(self)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("程序自带的内核（默认，稳）", False)
        self.mode_combo.addItem("我自己装的 Edge / Chrome", True)
        self.mode_combo.setCurrentIndex(1 if cfg["use_real_browser"] else 0)
        self.mode_combo.currentIndexChanged.connect(self._refresh_enabled)
        root.addRow("用哪个浏览器：", self.mode_combo)

        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("留空＝自动探测（先找 Chrome 再找 Edge）")
        self.path_edit.textChanged.connect(self._refresh_state)
        self.btn_auto = QPushButton("自动探测")
        self.btn_auto.setToolTip("在常见安装位置找 Chrome / Edge，并把版本读出来")
        self.btn_auto.clicked.connect(self._auto_detect)
        self.btn_pick = QPushButton("选择浏览器…")
        self.btn_pick.setToolTip("选浏览器的 exe；桌面快捷方式（.lnk）也能选，会自动解析")
        self.btn_pick.clicked.connect(self._pick)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.btn_auto)
        path_layout.addWidget(self.btn_pick)
        root.addRow("浏览器路径：", path_row)

        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(cfg["debug_port"]))
        self.port_spin.setToolTip("程序靠它跟浏览器说话（CDP）；被别的程序占着就换一个")
        port_layout.addWidget(self.port_spin)
        port_layout.addStretch()
        root.addRow("调试端口：", port_row)

        self.type_url_box = QCheckBox("网址用键盘敲进地址栏（像真人；窗口得在前台）")
        self.type_url_box.setChecked(bool(cfg["type_url"]))
        self.type_url_box.setToolTip("不勾＝程序直接让浏览器打开网址（快、稳）；\n"
                                     "勾上＝Ctrl+L 聚焦地址栏后逐字敲进去再回车（拟人，但窗口被挡住就会敲错地方）")
        root.addRow("", self.type_url_box)

        self.keep_open_box = QCheckBox("跑完不关浏览器窗口（留着你自己看）")
        self.keep_open_box.setChecked(bool(cfg["keep_open"]))
        self.keep_open_box.setToolTip("不勾＝流程跑完就关掉程序启动的那个窗口（下次还用它，cookie 都在）")
        root.addRow("", self.keep_open_box)

        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.state.setStyleSheet("color: #64748b;")
        root.addRow("", self.state)

        note = QLabel("说明：用真浏览器时，登录状态由它**自己的配置目录**保存"
                      "（在数据目录的「浏览器/用户配置/」里），登录一次就长期有效；\n"
                      "也不需要再下载 Playwright 的 700MB 内核。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #94a3b8;")
        root.addRow("", note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addRow("", buttons)

        self._refresh_enabled()
        self._refresh_state()

    # ------------------------------------------------------------------
    def _refresh_enabled(self):
        real = bool(self.mode_combo.currentData())
        for w in (self.path_edit, self.btn_auto, self.btn_pick, self.port_spin,
                  self.type_url_box, self.keep_open_box):
            w.setEnabled(real)
        self._refresh_state()

    def _refresh_state(self, *_):
        if not bool(self.mode_combo.currentData()):
            self.state.setText("现在用程序自带的内核（内核缺失时会提示你下载）。")
            self.state.setStyleSheet("color: #64748b;")
            return
        hint = self.path_edit.text().strip()
        try:
            exe = bl.find_browser(hint)
        except Exception as e:
            self.state.setText("⚠ " + str(e).splitlines()[0])
            self.state.setStyleSheet("color: #b45309;")
            return
        extra = ""
        info = bl.cdp_version(int(self.port_spin.value()), timeout_s=0.5)
        if info:
            extra = f"　（端口 {self.port_spin.value()} 上已有实例：{info.get('Browser') or '未知'}）"
        self.state.setText(f"✓ 用 {bl.browser_name(exe)}：{exe}{extra}")
        self.state.setStyleSheet("color: #0f766e;")

    def _auto_detect(self):
        try:
            exe = bl.find_browser("")
        except Exception as e:
            QMessageBox.warning(self, "没找到", str(e))
            return
        self.path_edit.setText(exe)
        self._refresh_state()

    def _pick(self):
        picked, _ = QFileDialog.getOpenFileName(
            self, "选浏览器（exe，或桌面上的快捷方式）", "",
            "浏览器或快捷方式 (*.exe *.lnk);;所有文件 (*.*)")
        if picked:
            self.path_edit.setText(picked)
            self._refresh_state()

    def _on_save(self):
        real = bool(self.mode_combo.currentData())
        if real:
            try:
                bl.find_browser(self.path_edit.text().strip())
            except Exception as e:
                QMessageBox.warning(self, "先把这个改好", str(e))
                return
        store = ProjectStore(self.project_dir)
        store.save_browser(
            use_real_browser=real,
            browser_path=self.path_edit.text().strip(),
            debug_port=int(self.port_spin.value()),
            type_url=bool(self.type_url_box.isChecked()),
            keep_open=bool(self.keep_open_box.isChecked()),
        )
        self.accept()