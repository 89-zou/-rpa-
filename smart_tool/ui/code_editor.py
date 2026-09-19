# -*- coding: utf-8 -*-
"""写代码用的多行输入框：回车自动补缩进、Tab 打 4 个空格。

普通 QPlainTextEdit 得让用户自己按 Tab 凑缩进——Python 里 Tab 和空格一混用
就报 TabError，JS 里也没人愿意数空格。这里按回车时自动补上一行的缩进，
上一行以 `:` `{` `[` `(` 结尾就再多缩一层。
"""
import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QKeyEvent, QTextCursor
from PyQt6.QtWidgets import QPlainTextEdit

#: 统一用 4 个空格（Python 官方推荐，JS 也通用）
INDENT = "    "


class CodeEditor(QPlainTextEdit):
    """带自动缩进的代码输入框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(mono)
        # 不让 Tab 跑去切换焦点，否则按 Tab 是没反应的
        self.setTabChangesFocus(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        # 回车：补上一行的缩进（+ 一层，如果上一行是冒号/左括号结尾）
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                not (mods & Qt.KeyboardModifier.ShiftModifier):
            cursor = self.textCursor()
            head = cursor.block().text()[:cursor.positionInBlock()]
            indent = re.match(r"[ \t]*", head).group(0).replace("\t", INDENT)
            extra = INDENT if head.rstrip().endswith((":", "{", "[", "(")) else ""
            super().keyPressEvent(event)
            self.textCursor().insertText(indent + extra)
            return
        # Tab：插 4 个空格（选中多行时整体缩进）
        if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ControlModifier):
            cursor = self.textCursor()
            if cursor.hasSelection():
                self._indent_selection(cursor, INDENT)
            else:
                cursor.insertText(INDENT)
            return
        # Shift+Tab：选中多行整体减少一层缩进
        if key == Qt.Key.Key_Backtab:
            self._indent_selection(self.textCursor(), INDENT, remove=True)
            return
        super().keyPressEvent(event)

    def _indent_selection(self, cursor, indent: str, remove: bool = False):
        """选中的每一行行首统一加 / 减一层缩进。"""
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        cursor.setPosition(start)
        first = cursor.blockNumber()
        cursor.setPosition(end)
        last = cursor.blockNumber()
        cursor.beginEditBlock()
        try:
            for row in range(first, last + 1):
                block = self.document().findBlockByNumber(row)
                if not block.isValid():
                    continue
                text = block.text()
                if remove:
                    new = re.sub(r"^[ \t]{1,%d}" % len(indent), "", text, count=1)
                else:
                    new = indent + text
                if new != text:
                    cur = self.textCursor()
                    cur.setPosition(block.position())
                    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                     QTextCursor.MoveMode.KeepAnchor)
                    cur.insertText(new)
        finally:
            cursor.endEditBlock()
