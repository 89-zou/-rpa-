# -*- coding: utf-8 -*-
"""程序入口（GUI）。

源码运行（开发版）：
    直接进主界面 —— 不弹安装向导、不显示启动海报、不往系统里写任何东西。

打包后的 exe（生产版）：
    第一次运行 → 先弹【安装向导】（选一个文件夹、构建环境、建快捷方式、登记卸载入口）
    → 每次启动显示启动海报（加载完才让点，或者 8 秒后自动进）
    → 主窗口（默认载入演示项目，删了就是空项目）。

两种情况不启动主界面：
    · 用户在向导里点了【取消】；
    · 程序被复制到了安装目录 —— 改为启动那边的程序（当前进程退出）。

命令行（两个版本都支持）：
    --setup      重跑安装向导（补装浏览器内核、改安装位置、重建快捷方式）
    --uninstall  卸载（系统「设置 → 应用」里点卸载执行的就是这条）

开源版没有「启动广告页」和「安装向导 / 卸载」这几个部件（作者发行版专用），
所以它们的导入都是可选的：文件不在就照常进主界面，不影响写流程、跑流程。
"""
import sys
import traceback
from datetime import datetime

from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from smart_tool import paths
from smart_tool.core import crash_guard
from smart_tool.ui.main_window import MainWindow

try:                     # 开源版不带启动广告页
    from smart_tool.ui.splash import AdSplash
except ImportError:      # pragma: no cover
    AdSplash = None


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


def run_first_time_setup() -> str:
    """第一次运行先弹安装向导（选目录、构建环境、建捷径、登记卸载入口）。

    返回接下来该干什么：
        "continue"  正常继续（装过了 / 装好了，就地运行）
        "relaunch"  程序已经复制到安装目录 → 去启动那边的，当前进程退出
        "quit"      用户取消了安装向导 → 什么都不做，直接退出

    用户取消就不再往下走（不会偷偷启动程序，也不会把取消记成"装过了"）——
    下次运行还会弹向导。
    """
    if paths.load_config().get("installed"):
        return "continue"
    try:
        from smart_tool.setup_wizard import SetupWizard
    except ImportError:      # 开源版没有安装向导：直接进主界面
        return "continue"

    wizard = SetupWizard(first_run=True)
    if wizard.exec() != QDialog.DialogCode.Accepted:
        return "quit"
    if wizard.relaunch_target:
        wizard._launch(wizard.relaunch_target)
        return "relaunch"
    return "continue"


def main():
    # 卸载入口：系统「设置 → 应用 → 小邹RPA → 卸载」执行的就是这条
    # （注册表里的 UninstallString 写着 "<本程序>" --uninstall）
    if "--uninstall" in sys.argv[1:]:
        try:
            from smart_tool.uninstall import main as uninstall_main
        except ImportError:
            print("这个版本不带卸载程序（那是作者发行版里的部件）。")
            return
        sys.exit(uninstall_main())

    # 重跑安装向导（补装浏览器内核、改安装位置、重建快捷方式都用它）。
    # 开发时也留着这条：想看那套流程就 `python -m smart_tool.main --setup`。
    if "--setup" in sys.argv[1:]:
        try:
            from smart_tool.setup_wizard import main as setup_main
        except ImportError:
            print("这个版本不带安装向导：直接 `python -m smart_tool.main` 就能跑。")
            return
        sys.exit(setup_main())

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

    # 开发版（源码运行）与生产版（打包 exe）从这里分开：
    #   生产版：第一次运行弹安装向导 → 启动海报 → 主界面；
    #   开发版：跳过这两样，直接开主界面（写代码时不用每次点一遍向导）。
    production = paths.is_production()

    if production:
        # 第一次运行：先把环境装好（安装位置、演示项目、浏览器内核、快捷方式）
        # 用户点了取消 → 直接退出，什么都不启动
        action = run_first_time_setup()
        if action in ("quit", "relaunch"):
            return

    # 启动海报：先画出来，再去建主窗口（建窗口最慢，海报上会写进度）
    splash = AdSplash.try_create() if (production and AdSplash is not None) else None
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
