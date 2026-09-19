# -*- coding: utf-8 -*-
"""安装向导：像商业软件那样先引导一下（选目录 + 建快捷方式）。

运行方式：
    · 源码运行：  python -m smart_tool.setup_wizard
    · 打包之后：  单独打一个「小邹RPA-安装向导.exe」（同一个入口）

它做三件事：
    1. 让用户指定【用户数据目录】（项目 / 账号密码 / 采集结果 / 登录态都放那儿）；
    2. （打包版）把程序文件复制到用户选的【安装目录】；
    3. 在桌面 / 开始菜单建一个带 logo 图标的快捷方式。

所有选择写进 `%APPDATA%\\小邹RPA\\config.json`，程序启动时读它。
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget, QWizard,
    QWizardPage,
)

from smart_tool import paths
from smart_tool.core import shortcut


def _default_data_dir() -> str:
    """默认用户数据目录：已经配过就沿用，否则 %APPDATA%\\小邹RPA。"""
    cfg = paths.load_config().get("data_dir")
    if cfg:
        return str(cfg)
    if (paths.app_dir() / "projects").is_dir():
        return str(paths.app_dir())          # 绿色版：就放程序旁边
    return str(paths.CONFIG_DIR)


def _default_install_dir() -> str:
    """默认安装目录（打包版才用得到）。"""
    import os

    base = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return str(Path(base) / paths.APP_NAME)


class SetupWizard(QWizard):
    """安装向导（4 步：欢迎 → 目录 → 选项 → 安装完成）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{paths.APP_NAME} 安装向导")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(720, 520)
        self.results: list = []
        self._frozen = bool(getattr(sys, "frozen", False))

        icon = paths.icon_file()
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步 >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< 上一步")
        self.setButtonText(QWizard.WizardButton.FinishButton, "开始安装")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")

        self.addPage(self._page_welcome())
        self.addPage(self._page_dirs())
        self.addPage(self._page_options())
        self.addPage(self._page_finish())

    # ------------------------------
    # 第 1 步：欢迎
    # ------------------------------
    def _page_welcome(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("欢迎使用「小邹RPA」")
        page.setSubTitle("浏览器 / 桌面自动化 + AI 编排，作者：" + paths.AUTHOR)
        lay = QVBoxLayout(page)
        logo = paths.logo_file()
        if logo.is_file():
            pix = QPixmap(str(logo)).scaled(
                160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            pic = QLabel()
            pic.setPixmap(pix)
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(pic)
        text = QLabel(
            "这个向导会帮你做三件事：\n"
            "  1. 指定【用户数据目录】——你的项目、账号密码、采集结果、登录态都放这里；\n"
            "  2. （打包版）把程序文件复制到你选的【安装目录】；\n"
            "  3. 在桌面建一个带 logo 的快捷方式，双击就能启动。\n\n"
            "随时点【取消】都不会改动任何东西。"
        )
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addStretch(1)
        return page

    # ------------------------------
    # 第 2 步：目录
    # ------------------------------
    def _page_dirs(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("选择目录")
        page.setSubTitle("想改的话点【浏览…】；默认值直接用也没问题")
        root = QVBoxLayout(page)

        form = QFormLayout()
        self.data_edit = QLineEdit(_default_data_dir())
        btn_data = QPushButton("浏览…")
        btn_data.clicked.connect(lambda: self._pick(self.data_edit, "选用户数据目录"))
        form.addRow("用户数据目录：", self._with_button(self.data_edit, btn_data))
        root.addLayout(form)
        hint = QLabel(
            "项目（含账号密码）、图片库、采集到的数据、登录态 cookie、崩溃日志都在这个目录里。\n"
            "换电脑或备份，只拷这一个目录就够了。")
        hint.setStyleSheet("color:#666;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        if self._frozen:
            form2 = QFormLayout()
            self.install_edit = QLineEdit(_default_install_dir())
            btn_app = QPushButton("浏览…")
            btn_app.clicked.connect(
                lambda: self._pick(self.install_edit, "选安装目录"))
            form2.addRow("程序安装目录：", self._with_button(self.install_edit, btn_app))
            root.addLayout(form2)
            root.addWidget(QLabel("程序文件会复制到这里（建议用默认的 Program Files）。"))
        else:
            self.install_edit = None
            root.addWidget(QLabel(
                f"程序现在在：{paths.app_dir()}\n"
                "（源码运行时不会复制程序文件；打包成 exe 后这一步才会把程序装到"
                "你选的目录。）"))
        root.addStretch(1)
        return page

    @staticmethod
    def _with_button(edit: QLineEdit, button: QPushButton) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        lay.addWidget(button)
        return box

    def _pick(self, edit: QLineEdit, title: str):
        folder = QFileDialog.getExistingDirectory(self, title, edit.text())
        if folder:
            edit.setText(folder)

    # ------------------------------
    # 第 3 步：选项
    # ------------------------------
    def _page_options(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("选项")
        lay = QVBoxLayout(page)
        cfg = paths.load_config()
        self.chk_desktop = QCheckBox("在桌面创建快捷方式（带 logo 图标）")
        self.chk_desktop.setChecked(bool(cfg.get("desktop_shortcut", True)))
        self.chk_start = QCheckBox("在开始菜单里放一个")
        self.chk_start.setChecked(bool(cfg.get("startmenu_shortcut", True)))
        self.chk_ad = QCheckBox("启动时显示海报页（加载完才能点进入）")
        self.chk_ad.setChecked(bool(cfg.get("show_ad", True)))
        self.chk_start_after = QCheckBox("安装完成后立即启动程序")
        self.chk_start_after.setChecked(True)
        for w in (self.chk_desktop, self.chk_start, self.chk_ad, self.chk_start_after):
            lay.addWidget(w)
        lay.addStretch(1)
        return page

    # ------------------------------
    # 第 4 步：安装
    # ------------------------------
    def _page_finish(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("准备安装")
        page.setSubTitle("点【开始安装】执行；安装完这里会显示结果")
        lay = QVBoxLayout(page)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary)
        lay.addStretch(1)
        return page

    def _summary_text(self) -> str:
        lines = [f"· 用户数据目录：{self.data_edit.text()}"]
        if self._frozen and self.install_edit is not None:
            lines.append(f"· 程序安装目录：{self.install_edit.text()}")
        if self.chk_desktop.isChecked():
            lines.append("· 桌面快捷方式：会创建")
        if self.chk_start.isChecked():
            lines.append("· 开始菜单：会创建")
        lines.append("· 启动海报：" + ("显示" if self.chk_ad.isChecked() else "不显示"))
        return "\n".join(lines)

    def initializePage(self, page_id: int):
        super().initializePage(page_id)
        if page_id == self.pageIds()[-1]:
            self.summary.setText(self._summary_text())

    # ------------------------------
    # 真正干活
    # ------------------------------
    def accept(self):
        """点【开始安装】→ 执行安装步骤 → 报告结果。"""
        self.results = []
        try:
            self._do_install()
        except Exception as e:                      # 兜底：不让向导崩掉
            self.results.append((False, f"安装过程出错：{type(e).__name__}: {e}"))
        ok = all(flag for flag, _ in self.results)
        body = "\n".join(("✓ " if flag else "✗ ") + text
                         for flag, text in self.results)
        tip = ("\n\n现在就可以双击桌面的「%s」启动了。" % paths.APP_NAME) if ok else \
            "\n\n有步骤没成功，按上面的提示处理一下再重来。"
        box = QMessageBox(self)
        box.setWindowTitle("安装完成" if ok else "安装有失败")
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
        box.setText(body + tip)
        box.exec()
        super().accept()
        if ok and self.chk_start_after.isChecked():
            self._launch()

    def _do_install(self):
        cfg_dir = Path(str(self.data_edit.text()).strip())
        try:
            paths.set_data_dir(cfg_dir)
            self.results.append((True, f"用户数据目录已设为：{cfg_dir}"))
        except OSError as e:
            self.results.append((False, f"用户数据目录不可用：{e}"))
        paths.save_config(
            show_ad=bool(self.chk_ad.isChecked()),
            desktop_shortcut=bool(self.chk_desktop.isChecked()),
            startmenu_shortcut=bool(self.chk_start.isChecked()),
        )

        target = shortcut.launch_target()
        exe_dir = paths.app_dir()
        if self._frozen and self.install_edit is not None:
            want = Path(str(self.install_edit.text()).strip())
            if want.resolve() != exe_dir.resolve():
                try:
                    shutil.copytree(exe_dir, want, dirs_exist_ok=True)
                    target = {"target": str(want / Path(sys.executable).name),
                              "args": "", "workdir": str(want)}
                    self.results.append((True, f"程序已复制到：{want}"))
                except OSError as e:
                    self.results.append(
                        (False, f"复制程序到 {want} 失败：{e}（可能需要管理员权限）"))
            else:
                self.results.append((True, f"程序就在安装目录里：{exe_dir}"))
        else:
            self.results.append(
                (True, "源码运行模式：跳过复制程序文件（打包成 exe 后才会复制）"))

        icon = paths.icon_file()
        if self.chk_desktop.isChecked():
            r = shortcut.create_shortcut(
                target["target"], target["args"], icon=str(icon) if icon.is_file() else "",
                workdir=target["workdir"], where="desktop",
                description=f"{paths.APP_NAME} ｜ {paths.AUTHOR}")
            self.results.append((r["ok"], f"桌面快捷方式：{r.get('path') or r['error']}"
                                + ("" if r["ok"] else f"（{r['error']}）")))
        if self.chk_start.isChecked():
            r = shortcut.create_shortcut(
                target["target"], target["args"], icon=str(icon) if icon.is_file() else "",
                workdir=target["workdir"], where="startmenu",
                description=f"{paths.APP_NAME} ｜ {paths.AUTHOR}")
            self.results.append((r["ok"], f"开始菜单：{r.get('path') or r['error']}"
                                + ("" if r["ok"] else f"（{r['error']}）")))

    def _launch(self):
        t = shortcut.launch_target()
        try:
            subprocess.Popen([t["target"]] + ([t["args"]] if t["args"] else []),
                             cwd=t["workdir"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass


def main():
    app = QApplication(sys.argv)
    icon = paths.icon_file()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    wizard = SetupWizard()
    wizard.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
