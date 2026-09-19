# -*- coding: utf-8 -*-
"""程序入口。

启动顺序（用户拿到 exe 后看到的顺序）：
    第一次运行 → 先弹【安装向导】（选目录、构建环境、建快捷方式）
    → 显示启动海报（加载完才让点，或者 8 秒后自动进）
    → 主窗口（默认载入演示项目，删了就是空项目）。
"""
import sys
import traceback
from datetime import datetime

from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

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


def run_first_time_setup() -> bool:
    """第一次运行先弹安装向导（选目录、构建环境、建捷径）。

    用户取消也不拦着——直接标成「已处理过」，用默认目录继续，免得每次启动都弹。
    """
    if paths.load_config().get("installed"):
        return False
    from smart_tool.setup_wizard import SetupWizard

    wizard = SetupWizard(first_run=True)
    finished = wizard.exec() == QDialog.DialogCode.Accepted
    if not finished:
        paths.save_config(installed=True)
    return True


def main():
    # 卸载入口：系统「设置 → 应用 → 小邹RPA → 卸载」执行的就是这条
    # （注册表里的 UninstallString 写着 "<本程序>" --uninstall）
    if "--uninstall" in sys.argv[1:]:
        from smart_tool.uninstall import main as uninstall_main
        sys.exit(uninstall_main())

    app = QApplication(sys.argv)
    # Windows 下显式指定中文字体，避免回退到无 CJK 字形的字体
    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    # 窗口/任务栏图标（从 assets/logo.png 生成 .ico，缓存在配置目录）
    icon = paths.icon_file()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    paths.ensure_dirs()
    install_crash_handler()

    # 第一次运行：先把环境装好（数据目录、演示项目、浏览器内核、快捷方式）
    run_first_time_setup()

    # 启动海报：先画出来，再去建主窗口（建窗口最慢，海报上会写进度）
    splash = AdSplash.try_create()
    if splash is not None:
        splash.show()
        splash.set_status("正在加载界面…", 20)
        app.processEvents()

    window = MainWindow()

    if splash is not None:
        splash.set_status("加载完成", 100)
        splash.set_ready()
        app.processEvents()
        splash.exec()               # 等用户点【进入程序】，或者 8 秒后自动进

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
