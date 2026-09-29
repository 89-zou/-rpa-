# -*- coding: utf-8 -*-
"""主窗口：装配标签页。"""
from PyQt6.QtWidgets import (
    QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)

from rpa_page_agent.ui.ai_settings_tab import AiSettingsTab, open_hub
from rpa_page_agent.ui.web_automation_tab import WebAutomationTab


class MainWindow(QMainWindow):
    """小邹RPA 主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("小邹RPA ｜ 作者：@小邹")
        self.setGeometry(300, 200, 900, 650)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.tabs = QTabWidget()
        self.web_tab = WebAutomationTab()
        self.tabs.addTab(self.web_tab, "主页")
        self.ai_tab = AiSettingsTab()
        self.tabs.addTab(self.ai_tab, "AI 设置")
        layout.addWidget(self.tabs)

        # 步骤编辑器里点【打开 AI 设置】→ 切到这个标签页（编辑器拿不到主窗口的控件）
        open_hub().open_requested.connect(self.show_ai_tab)
        # 每次切过来都重读一遍设置文件（可能刚在别处改过）
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def show_ai_tab(self):
        """切到【AI 设置】（切过去时 currentChanged 会顺带刷新）。"""
        self.tabs.setCurrentWidget(self.ai_tab)

    def _on_tab_changed(self, index: int):
        if self.tabs.widget(index) is self.ai_tab:
            self.ai_tab.reload()

    def closeEvent(self, event):
        """关闭窗口时确保 worker 安全退出。"""
        worker = self.web_tab._worker
        if worker and worker.isRunning():
            reply = QMessageBox.question(
                self, "确认退出",
                "执行正在进行中，确定退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            worker.stop()
            worker.wait(5000)
        event.accept()
