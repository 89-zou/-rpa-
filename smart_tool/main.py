# -*- coding: utf-8 -*-
"""程序入口。"""
import sys
import traceback
from datetime import datetime

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMessageBox

from smart_tool import paths
from smart_tool.ui.main_window import MainWindow


def install_crash_handler():
    """把未捕获的异常写进 crash.log 并弹窗提示。

    没有它的话，Qt 槽函数里抛出的异常会让程序毫无提示地直接退出（闪退），
    既看不到原因，也没留下任何线索。
    """
    log_file = paths.ROOT_DIR / "crash.log"

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}")
        except OSError:
            pass
        try:
            QMessageBox.critical(
                None, "程序内部错误",
                f"出错了，详细信息已写入：\n{log_file}\n\n{text[-1500:]}",
            )
        except Exception:
            pass

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    # Windows 下显式指定中文字体，避免回退到无 CJK 字形的字体
    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    install_crash_handler()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
