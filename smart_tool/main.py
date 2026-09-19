# -*- coding: utf-8 -*-
"""程序入口。

启动顺序：装异常钩子 → 显示启动海报（如果配了）→ 建主窗口（最慢的一步）
→ 海报上的【进入程序】亮起来 → 用户确认后才显示主窗口。
"""
import sys
import traceback
from datetime import datetime

from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from smart_tool import paths
from smart_tool.core import crash_guard
from smart_tool.ui.main_window import MainWindow
from smart_tool.ui.splash import AdSplash


def install_crash_handler():
    """把未捕获的异常写进 crash.log 并弹窗提示。

    没有它的话，Qt 槽函数里抛出的异常会让程序毫无提示地直接退出（闪退），
    既看不到原因，也没留下任何线索。

    另外再挂一层「原生崩溃兜底」：像访问冲突、回调里逃出异常这类崩溃，
    Python 层根本来不及反应，由 crash_guard 记下异常代码和最后一步在干什么。

    日志写在**用户数据目录**（不是程序目录）：装到 Program Files 时程序目录
    不可写，写不进去就等于崩溃线索全丢了。
    """
    log_file = paths.crash_log()
    crash_guard.install(log_file)

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
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
    # 窗口/任务栏图标（从 assets/logo.png 生成 .ico，缓存在数据目录）
    icon = paths.icon_file()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    paths.ensure_dirs()
    install_crash_handler()

    # 启动海报：先画出来，再去建主窗口（建窗口最慢，海报上会写进度）
    splash = AdSplash.try_create()
    if splash is not None:
        splash.show()
        splash.set_status("正在加载界面…", 20)
        app.processEvents()

    window = MainWindow()

    if splash is not None:
        splash.set_status("加载完成，点【进入程序】开始使用", 100)
        splash.set_ready()
        app.processEvents()
        splash.exec()               # 等用户点【进入程序】（回车也行）

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
