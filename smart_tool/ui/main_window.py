# -*- coding: utf-8 -*-
"""主窗口：装配标签页。"""
from PyQt6.QtWidgets import (
    QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)

from smart_tool.ui import picker_session
from smart_tool.ui.web_automation_tab import WebAutomationTab


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
        layout.addWidget(self.tabs)

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
        # 捕获用的那个浏览器是自己端着不放的（见 ui/picker_session），退出时收干净
        try:
            picker_session.shutdown()
        except Exception:
            pass
        event.accept()
