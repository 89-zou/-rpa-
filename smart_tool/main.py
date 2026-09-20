# -*- coding: utf-8 -*-
"""程序入口（GUI）。

启动顺序：

    1. 后台清掉单文件版被强杀留下的解包残渣（不耽误开界面）；
    2. 建 QApplication、装崩溃日志钩子；
    3. **首次运行的准备**：程序旁边就有 `projects/`（绿色版），或者以前选过位置
       → 直接进主界面；否则弹一个窗口让用户选「数据放哪儿」，顺手在里面建好
       示例项目、把浏览器内核下载到那个目录下的 `浏览器/`；
    4. 生产版（打包出来的 exe）显示启动海报，然后进主窗口。

用户点了【取消】：位置还没定时直接退出（没地方存数据没法用）；
位置已经定过、只是缺浏览器内核时照常进主界面（以后重开程序再装一次）。

开源版没有「启动广告页」（作者发行版专用），导入是可选的：
文件不在就照常进主界面，不影响写流程、跑流程。
"""
import sys
import traceback
from datetime import datetime

from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from smart_tool import paths
from smart_tool.core import browser_setup, crash_guard, temp_cleanup
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

    日志写在**用户数据目录**（不是程序目录）：程序目录不可写时，
    写不进去就等于崩溃线索全丢了。
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


def prepare_first_run() -> bool:
    """首次运行：确认「数据位置」和「浏览器内核」都齐了。返回 False＝退出程序。

    判断依据（都不用问用户）：
    · 程序旁边就有 `projects/`（绿色版），或者以前选过位置 → 位置算定好了；
    · `<数据目录>/浏览器/` 里有 chromium → 内核算装好了。

    两样都齐就什么都不弹，直接进主界面；缺哪样弹窗口补哪样。
    """
    need_path = not paths.data_dir_ready()
    need_kernel = not browser_setup.is_installed()
    if not need_path and not need_kernel:
        return True

    from smart_tool.ui.first_run_dialog import FirstRunDialog

    dlg = FirstRunDialog(need_path=need_path, need_kernel=need_kernel)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return True
    # 取消：位置都没定就没法用（没地方放项目），只能退出；
    # 只是缺内核的话，让他先进去写流程，跑之前再把内核装上。
    return not need_path


def clean_temp_leftovers():
    """后台清掉单文件版被强杀留下的解包残渣（`%TEMP%\\_MEIxxxxxx`）。

    只清「超过 24 小时没动过」的，正在跑的那份动不了会被跳过（见 core/temp_cleanup.py）。
    在后台线程里做，不耽误开界面；清了东西才写一行 cleanup.log（在用户数据目录里）。
    """
    def report(removed: int, freed: int):
        try:
            log = paths.DATA_DIR / "cleanup.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  "
                        f"清掉 {removed} 个 _MEI 残渣，释放 {freed / 1024 / 1024:.0f} MB\n")
        except OSError:
            pass

    temp_cleanup.clean_in_background(on_done=report)


def main():
    # 顺手清掉上次被强杀留下的解包残渣（后台做，不影响启动）
    clean_temp_leftovers()

    app = QApplication(sys.argv)
    # Windows 下显式指定中文字体，避免回退到无 CJK 字形的字体
    font = QFont("Microsoft YaHei", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    # 窗口/任务栏图标（从 assets/logo.png 生成 .ico，缓存在配置目录）
    icon = paths.icon_file()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    install_crash_handler()

    # 第一次使用：先让用户选好数据位置、把示例项目和浏览器内核准备出来。
    # 用户放弃且位置还没定 → 什么都不启动。
    if not prepare_first_run():
        return
    paths.ensure_dirs()

    # 启动海报：先画出来，再去建主窗口（建窗口最慢，海报上会写进度）。
    # 海报是给最终用户看的，写代码时（源码运行）每次挡一下太烦，所以只在 exe 里显示。
    production = paths.is_production()
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
