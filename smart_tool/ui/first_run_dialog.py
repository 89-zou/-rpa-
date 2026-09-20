# -*- coding: utf-8 -*-
"""首次运行的设置窗口：选一个存放位置，顺手把示例项目和浏览器内核准备好。

什么时候弹（见 main.prepare_first_run）：
· 程序旁边已经有 `projects/`，或者上次选过位置 → 不弹；
· 缺浏览器内核 → 弹，但只勾「下载内核」那一步（位置已经是已知的）；
· 连位置都还没定（真正第一次用）→ 弹，让用户选。

用户点【取消】：位置还没定时直接退出（没地方存数据没法用）；位置已经有了、
只是没装内核时照常进主界面（大不了先写流程，跑的时候会提示装内核）。

做三件事：
1. 把选定的目录写进配置（`paths.set_data_dir`，当场生效）；
2. 在里面建 `projects/`，把内置示例项目复制进去；
3. 把 Chromium 下载到 `<那个目录>/浏览器/`（已经有了就跳过）。
"""
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from smart_tool import paths
from smart_tool.core import browser_setup, project_store

#: 下载内核时如果卡在官方 CDN，用这个国内镜像（跟安装说明里写的一致）
MIRROR_HOST = "https://cdn.npmmirror.com/binaries/playwright"


class FirstRunDialog(QDialog):
    """「第一次使用，先准备一下」窗口。"""

    def __init__(self, parent=None, need_path: bool = True,
                 need_kernel: bool = True):
        super().__init__(parent)
        self.setWindowTitle("第一次使用 ｜ 小邹RPA")
        self.setMinimumWidth(560)
        self._busy = False
        self._need_path = need_path
        self._need_kernel = need_kernel

        root = QVBoxLayout(self)
        tip = QLabel(
            "先选一个存放数据的位置：<b>项目、账号密码、采集结果、浏览器内核</b>"
            "都会放在这个文件夹里。<br>"
            "想做成绿色版（整个文件夹拷走就能换电脑用），就选程序所在目录。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit(str(paths.suggested_data_dir()))
        self.path_edit.textChanged.connect(self._refresh_state)
        row_layout.addWidget(self.path_edit, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self._browse)
        row_layout.addWidget(self.btn_browse)
        root.addWidget(row)

        self.demo_box = QCheckBox("创建示例项目（采集示例-登录与采集）")
        self.demo_box.setChecked(True)
        root.addWidget(self.demo_box)

        self.kernel_box = QCheckBox("下载浏览器内核 Chromium（约 150 MB，跑网页流程要用）")
        self.kernel_box.setChecked(need_kernel)
        root.addWidget(self.kernel_box)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: #888;")
        root.addWidget(self.state_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setVisible(False)
        root.addWidget(self.log)

        buttons = QWidget()
        btn_layout = QHBoxLayout(buttons)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch(1)
        self.btn_start = QPushButton("开始")
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self._start)
        btn_layout.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        root.addWidget(buttons)

        if not need_path:
            # 位置已经定过了（只是缺内核）：不让改，免得项目目录跟着换
            self.path_edit.setEnabled(False)
            self.btn_browse.setEnabled(False)
            self.demo_box.setChecked(False)
            self.demo_box.setEnabled(False)
            self.setWindowTitle("准备运行环境 ｜ 小邹RPA")
        self._refresh_state()

    # ------------------------------
    # 界面
    # ------------------------------
    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "选择存放数据的文件夹", self.path_edit.text())
        if chosen:
            self.path_edit.setText(chosen)

    def _kernel_dir(self) -> Path:
        return Path(self.path_edit.text().strip()) / browser_setup.PORTABLE_DIR_NAME

    def _refresh_state(self):
        """刷新那一行说明：这个位置里有没有现成的内核。"""
        if self._busy:
            return
        text = self.path_edit.text().strip()
        if not text:
            self.state_label.setText("请先选一个文件夹。")
            return
        folder = Path(text)
        if folder.exists() and not folder.is_dir():
            self.state_label.setText("这个路径是一个文件，请选文件夹。")
            return
        if browser_setup.is_installed(folder / browser_setup.PORTABLE_DIR_NAME):
            self.kernel_box.setChecked(False)
            self.kernel_box.setEnabled(False)
            self.state_label.setText("这个位置里已经有浏览器内核了，不会重复下载。")
        else:
            self.kernel_box.setEnabled(True)
            self.kernel_box.setChecked(self._need_kernel)
            self.state_label.setText(
                f"浏览器内核会装到：{self._kernel_dir()}")
        if not folder.exists():
            self.state_label.setText(
                self.state_label.text() + "（这个文件夹还不存在，会新建）")

    def _say(self, line: str):
        self.log.appendPlainText(line)
        QApplication.processEvents()        # 边做边显示，别让窗口假死

    # ------------------------------
    # 开始
    # ------------------------------
    def _start(self):
        folder = Path(self.path_edit.text().strip()).expanduser()
        if not str(folder):
            QMessageBox.warning(self, "还差一步", "请先选一个存放数据的文件夹。")
            return
        if folder.exists() and not folder.is_dir():
            QMessageBox.warning(self, "路径不对", "这个路径是一个文件，请选文件夹。")
            return
        if not paths.is_writable(folder):
            QMessageBox.warning(
                self, "这个文件夹写不进去",
                f"没权限往这里写文件：\n{folder}\n\n换一个位置试试"
                "（比如「文档」下面，或者程序所在目录）。")
            return

        self._busy = True
        self.btn_start.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.path_edit.setEnabled(False)
        self.demo_box.setEnabled(False)
        self.kernel_box.setEnabled(False)
        self.log.setVisible(True)
        try:
            self._run(folder)
        except OSError as exc:
            self._busy = False
            # 没成：把界面放开，让用户改一改重试（比如换个文件夹、不装内核先进去）
            self.btn_start.setEnabled(True)
            self.btn_browse.setEnabled(self._need_path)
            self.path_edit.setEnabled(self._need_path)
            self.demo_box.setEnabled(self._need_path)
            self._refresh_state()
            QMessageBox.warning(self, "准备环境失败", f"{exc}\n\n换个位置再试试。")
            return
        self._busy = False
        self.accept()

    def _run(self, folder: Path) -> None:
        """真正开始准备（出错就抛 OSError，交给 _start 兜住）。"""
        self._say(f"数据目录：{folder}")
        paths.set_data_dir(folder)              # 写配置 + 当场生效
        self._say(f"项目目录：{paths.PROJECTS_DIR}")

        if self.demo_box.isChecked():
            made = project_store.install_demo_project(paths.PROJECTS_DIR)
            self._say(f"已创建示例项目：{made.name}" if made
                      else "示例项目已经在了，跳过。")

        if not self.kernel_box.isChecked():
            self._say("跳过浏览器内核（以后跑网页流程前再装也行）。")
            return
        self._download_kernel()

    def _download_kernel(self) -> None:
        """下载 Chromium；失败时给出可操作的选择（再试一次 / 先不管它）。"""
        browser_setup.ensure_env()              # 下载落到数据目录下的「浏览器」
        self._say(f"浏览器内核将装到：{browser_setup.browsers_dir()}")
        if browser_setup.is_installed():
            self._say("已经有内核了，不用下载。")
            return True

        # 官方 CDN 在国内经常卡住，默认走镜像（用户要是自己设过就不动它）
        os.environ.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", MIRROR_HOST)
        self._say(f"下载源：{os.environ['PLAYWRIGHT_DOWNLOAD_HOST']}")
        ok = browser_setup.install(on_log=self._say)
        if ok:
            return

        reply = QMessageBox.question(
            self, "内核没装成功",
            "浏览器内核没下载成功（多半是网络问题）。\n\n"
            "要再试一次吗？\n"
            "选【No】= 先不管它、直接打开程序：可以照常写流程，"
            "以后跑网页流程前再重开程序装一次。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._download_kernel()


def main() -> int:
    """命令行单独打开这个窗口（调试用）：python -m smart_tool.ui.first_run_dialog"""
    import sys

    app = QApplication(sys.argv)
    dlg = FirstRunDialog()
    dlg.exec()
    print(f"数据目录：{paths.DATA_DIR}")
    print(f"项目目录：{paths.PROJECTS_DIR}")
    print(f"内核目录：{browser_setup.browsers_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
