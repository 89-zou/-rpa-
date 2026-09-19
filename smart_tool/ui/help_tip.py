# -*- coding: utf-8 -*-
"""「?」说明按钮：把长段解释从界面正文里请出来，点一下弹窗看。

界面里塞一大段灰字说明，既挤占空间、又容易被当成背景忽略。统一收成一个小按钮，
需要的时候点开：文字可选中复制，内容多也能滚动看。

用法：
    root.addWidget(help_row("一句摘要（可省略）", "标题", 详细的说明文字))
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
    QWidget,
)


class HelpDialog(QDialog):
    """说明弹窗：只读、可滚动、文字能选中复制。"""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"说明 · {title}")
        self.setMinimumSize(560, 400)
        self.resize(660, 500)
        root = QVBoxLayout(self)
        view = QTextBrowser()
        view.setPlainText(text)
        root.addWidget(view, 1)
        btns = QHBoxLayout()
        btns.addStretch()
        self.btn_ok = QPushButton("知道了")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        btns.addWidget(self.btn_ok)
        root.addLayout(btns)


def show_help(parent, title: str, text: str):
    """弹一个说明窗（模态，关掉就回到原界面）。"""
    HelpDialog(title, text, parent).exec()


class HelpButton(QPushButton):
    """一个小「?」按钮：点开就是这段说明。"""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__("?", parent)
        self.setFixedSize(22, 22)
        self.setToolTip("点开看说明")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title = title
        self._text = text
        self.clicked.connect(self._open)

    def _open(self):
        # 用所在窗口当父级：嵌在【项目管理】页签里时，弹窗也挂在那个对话框上
        show_help(self.window(), self._title, self._text)

    def set_content(self, title: str, text: str):
        """换一段说明（同一个按钮要跟着选项变的场景，比如脚本语言）。"""
        self._title, self._text = title, text


def help_row(lead: str, title: str, text: str) -> QWidget:
    """「一句摘要 + 右上角 ?」的一行；摘要留空就只剩一个 ?。"""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    if lead:
        label = QLabel(lead)
        label.setWordWrap(True)
        label.setStyleSheet("color:#777777;")
        lay.addWidget(label, 1)
    lay.addStretch()
    lay.addWidget(HelpButton(title, text, row))
    return row
