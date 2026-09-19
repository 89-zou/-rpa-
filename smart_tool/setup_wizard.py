# -*- coding: utf-8 -*-
"""安装向导：引导选目录 → 环境构建 → 建快捷方式 → 启动程序。

用户拿到 exe 以后看到的第一个界面就是它（第一次运行自动弹）：
    1. 【欢迎】说明这个小工具能干什么；
    2. 【目录】默认 C 盘常用安装路径（Program Files），也可以自己挑；
       还要选【用户数据目录】（项目、账号密码、采集结果、登录态都放这儿）；
    3. 【选项】桌面快捷方式（默认勾上，强烈建议留着）、开始菜单；
    4. 【安装】开始环境构建，实时日志：
          · 写配置、建目录
          · 把内置的演示项目复制到你的数据目录（启动后会默认打开它）
          · 下载浏览器内核 Chromium（打包版没有自带，约 150 MB）
          · 建桌面 / 开始菜单快捷方式（带 logo 图标）
    5. 完成后可以直接启动程序。

另外也能当独立入口跑：`python -m smart_tool.setup_wizard`（随时重跑，比如补装浏览器）。
"""
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget, QWizard, QWizardPage,
)

from smart_tool import paths
from smart_tool.core import browser_setup, shortcut


# ============================================================
# 环境构建（在后台线程里跑，界面不卡）
# ============================================================
class BuildWorker(QThread):
    """按计划做环境构建：写配置 → 建目录 → 演示项目 → 浏览器内核 → 快捷方式。"""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    done = pyqtSignal(bool, list)          # (全部成功?, [(成功?, 说明), ...])

    def __init__(self, plan: Dict, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.results: List[Tuple[bool, str]] = []
        self._bar = 0

    def _step(self, percent: int, text: str = ""):
        self._bar = max(self._bar, percent)
        self.progress.emit(self._bar)
        if text:
            self.log.emit(text)

    def run(self):
        try:
            self._build()
        except Exception as e:                     # 兜底：不让向导崩掉
            self.results.append((False, f"环境构建出错：{type(e).__name__}: {e}"))
        ok = bool(self.results) and all(flag for flag, _ in self.results)
        self.progress.emit(100)
        self.done.emit(ok, self.results)

    # ------------------------------
    def _build(self):
        plan = self.plan
        data_dir = Path(str(plan["data_dir"])).expanduser()
        self._step(6, f"用户数据目录：{data_dir}")

        # 1) 写配置：数据目录 / 快捷方式选项 / 默认打开的项目
        try:
            paths.set_data_dir(data_dir)
            paths.save_config(
                installed=True,
                desktop_shortcut=bool(plan.get("desktop")),
                startmenu_shortcut=bool(plan.get("startmenu")),
                default_project=paths.DEMO_PROJECT_NAME,
            )
            self.results.append((True, f"配置已写入：{paths.CONFIG_FILE}"))
        except OSError as e:
            self.results.append((False, f"写配置失败：{e}（可能是目录没权限）"))

        # 2) 建目录
        projects = data_dir / "projects"
        try:
            projects.mkdir(parents=True, exist_ok=True)
            (data_dir / "logs").mkdir(parents=True, exist_ok=True)
            self._step(16, f"项目目录：{projects}")
            self.results.append((True, f"项目目录就绪：{projects}"))
        except OSError as e:
            self.results.append((False, f"建项目目录失败：{e}"))
            return

        # 3) 内置演示项目
        self._step(24, "正在装演示项目…")
        demo = projects / paths.DEMO_PROJECT_NAME
        template = paths.demo_template_dir()
        if demo.is_dir():
            self.results.append((True, f"演示项目已存在，保留你现在的：{demo.name}"))
            self.log.emit(f"演示项目已经在了，跳过（{demo}）")
        elif template.is_dir():
            try:
                shutil.copytree(template, demo)
                fixed = _fix_project_paths(demo)
                self.results.append(
                    (True, f"演示项目已装好：{demo.name}"
                           + (f"（改了 {len(fixed)} 处写死的路径）" if fixed else "")))
                for line in fixed:
                    self.log.emit("  " + line)
            except OSError as e:
                self.results.append((False, f"复制演示项目失败：{e}"))
        else:
            self.results.append((False, "没找到内置演示项目模板（assets/templates）"))

        # 4) 浏览器内核（打包版不自带，要下 ~150MB）
        self._step(32)
        if not plan.get("browser", True):
            self.results.append((True, "按你的选择跳过浏览器内核下载"))
            self.log.emit("跳过浏览器内核下载")
        elif browser_setup.is_installed():
            self.log.emit(f"浏览器内核已经装好了：{browser_setup.browsers_dir()}")
            self.results.append((True, "浏览器内核：已就绪（不用重下）"))
            self._step(88)
        else:
            self.log.emit("正在下载浏览器内核 Chromium（约 150 MB）…")
            ok = browser_setup.install(on_log=self._browser_log)
            self.results.append((ok, "浏览器内核（Chromium）已装好" if ok else
                                 "浏览器内核没装成功（可以重跑安装向导再试）"))
            self._step(88)

        # 5) 快捷方式
        self._step(90, "正在建快捷方式…")
        target = _install_target(plan, self.results)
        icon = paths.icon_file()
        icon_text = str(icon) if icon.is_file() else ""
        for where, want, label in (("desktop", plan.get("desktop"), "桌面快捷方式"),
                                   ("startmenu", plan.get("startmenu"), "开始菜单快捷方式")):
            if not want:
                self.results.append((True, f"{label}：按你的选择跳过"))
                continue
            r = shortcut.create_shortcut(
                target["target"], target["args"], icon=icon_text,
                workdir=target["workdir"], where=where,
                description=f"{paths.APP_NAME} ｜ {paths.AUTHOR}")
            if r["ok"]:
                self.results.append((True, f"{label}：{r['path']}"))
            else:
                self.results.append(
                    (False, f"{label}创建失败：{r['error']}（可以稍后手动建）"))
        self._step(98)

    def _browser_log(self, text: str):
        """下载浏览器的输出：日志照发，进度条也跟着动一动。"""
        if text:
            self.log.emit("  " + text)
        self.progress.emit(min(86, self._bar + 1))
        self._bar = min(86, self._bar + 1)


def _install_target(plan: Dict, results: List[Tuple[bool, str]]) -> Dict[str, str]:
    """算快捷方式该指向谁（打包版可能会先复制程序文件到安装目录）。"""
    target = shortcut.launch_target()
    if not plan.get("frozen"):
        results.append((True, "源码运行模式：跳过复制程序文件（打包成 exe 后才会复制）"))
        return target
    want = str(plan.get("install_dir") or "").strip()
    if not want:
        return target
    want_path = Path(want).expanduser()
    here = paths.app_dir()
    if want_path.resolve() == here.resolve():
        results.append((True, f"程序就在安装目录里：{here}"))
        return target
    try:
        shutil.copytree(here, want_path, dirs_exist_ok=True)
    except OSError as e:
        results.append((False, f"复制程序到 {want_path} 失败：{e}（可能要管理员权限）"))
        return target
    results.append((True, f"程序已复制到：{want_path}"))
    return {"target": str(want_path / Path(sys.executable).name), "args": "",
            "workdir": str(want_path)}


def _fix_project_paths(project_dir: Path) -> List[str]:
    """把演示项目里写死的绝对路径改成新位置。

    演示项目的「关键词表路径」这类变量原来指向开发机的目录，装到别的机器就失效；
    这里按「项目里有没有同名文件」重新指一遍。
    """
    from smart_tool.core.project_store import ProjectStore

    store = ProjectStore(project_dir)
    variables = store.load_variables()
    fixed: List[str] = []
    for name, value in list(variables.items()):
        text = str(value).strip()
        if not text or ":" not in text[:3]:        # 只看绝对路径（C:\ / D:\）
            continue
        matches = [p for p in project_dir.rglob(Path(text).name) if p.is_file()]
        if matches and str(matches[0]) != text:
            variables[name] = str(matches[0])
            fixed.append(f"{name} → {matches[0].relative_to(project_dir)}")
    if fixed:
        store.save_variables(variables)
    return fixed


# ============================================================
# 向导页面
# ============================================================
class _WelcomePage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.setTitle(f"欢迎使用「{paths.APP_NAME}」")
        self.setSubTitle("浏览器 / 桌面自动化 + AI 编排 ｜ 作者：" + paths.AUTHOR)
        lay = QVBoxLayout(self)
        logo = paths.logo_file()
        if logo.is_file():
            pic = QLabel()
            pic.setPixmap(QPixmap(str(logo)).scaled(
                150, 150, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(pic)
        text = QLabel(
            "接下来会带你做这几件事（点【取消】不会改动任何东西）：\n"
            "  1. 选目录：程序装哪儿、你的项目数据存哪儿；\n"
            "  2. 环境构建：自动把内置演示项目装好、把浏览器内核下好；\n"
            "  3. 建一个带 logo 的桌面快捷方式，以后双击就能启动。\n\n"
            "装完就能直接用：打开程序会默认载入那个演示项目（不想要了随时删，\n"
            "删掉以后就是一个空项目，自己新建即可）。"
        )
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addStretch(1)


class _DirsPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("选择目录")
        self.setSubTitle("默认值直接用也行；想改就点【浏览…】")
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.data_edit = QLineEdit(wizard.default_data_dir)
        btn_data = QPushButton("浏览…")
        btn_data.clicked.connect(lambda: wizard.pick_dir(self.data_edit, "选用户数据目录"))
        form.addRow("用户数据目录：", wizard.with_button(self.data_edit, btn_data))
        if wizard.frozen:
            self.install_edit = QLineEdit(wizard.default_install_dir)
            btn_app = QPushButton("浏览…")
            btn_app.clicked.connect(
                lambda: wizard.pick_dir(self.install_edit, "选程序安装目录"))
            form.addRow("程序安装目录：", wizard.with_button(self.install_edit, btn_app))
        else:
            self.install_edit = None
        root.addLayout(form)

        hint = QLabel(
            "· 用户数据目录：你的项目（含账号密码）、图片库、采集结果、登录态 cookie、\n"
            "  崩溃日志都放这里。换电脑或做备份，只拷这一个目录就够了。\n"
            + ("· 程序安装目录：程序文件会复制到这里（建议用默认的 Program Files）。"
               if wizard.frozen else
               f"· 程序现在在：{paths.app_dir()}"
               "（源码运行时不会复制程序文件；打包成 exe 后才会复制）"))
        hint.setStyleSheet("color:#666;")
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch(1)

    def validatePage(self) -> bool:
        text = self.data_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先选一个「用户数据目录」")
            return False
        try:
            Path(text).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "这个目录用不了", f"{text}\n\n{e}")
            return False
        return True


class _OptionsPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.setTitle("选项")
        lay = QVBoxLayout(self)
        cfg = paths.load_config()
        self.chk_desktop = QCheckBox("在桌面创建快捷方式（带 logo 图标）")
        self.chk_desktop.setChecked(True)
        tip = QLabel("    ↑ 建议留着：以后双击桌面图标就能启动")
        tip.setStyleSheet("color:#2e7d32; font-size:11px;")
        self.chk_start = QCheckBox("在开始菜单里也放一个（以后重跑安装向导用得上）")
        self.chk_start.setChecked(bool(cfg.get("startmenu_shortcut", True)))
        self.chk_browser = QCheckBox("下载浏览器内核 Chromium（约 150 MB，跑网页自动化必装）")
        self.chk_browser.setChecked(True)
        self.chk_launch = QCheckBox("安装完成后立即启动程序")
        self.chk_launch.setChecked(True)
        for w in (self.chk_desktop, tip, self.chk_start, self.chk_browser, self.chk_launch):
            lay.addWidget(w)
        lay.addStretch(1)


class _BuildPage(QWizardPage):
    """安装页：进来自动开始环境构建，日志实时刷。"""

    def __init__(self, wizard):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("正在安装 / 环境构建")
        self.setSubTitle("喝口水，装完这里会写清楚每一步的结果")
        lay = QVBoxLayout(self)

        self.status = QLabel("准备中…")
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(220)
        self.log_box.setPlaceholderText("安装过程中的日志会显示在这里…")
        lay.addWidget(self.log_box, 1)

    def initializePage(self, page_id: int):
        super().initializePage(page_id)
        buttons = self.wizard_ref.button
        buttons(QWizard.WizardButton.FinishButton).setEnabled(False)
        buttons(QWizard.WizardButton.BackButton).setEnabled(False)
        buttons(QWizard.WizardButton.CancelButton).setEnabled(False)
        self.status.setText("开始环境构建…")
        self.bar.setValue(2)
        self.log_box.setPlainText("")

        plan = {
            "data_dir": self.wizard_ref.data_dir(),
            "install_dir": self.wizard_ref.install_dir(),
            "desktop": self.wizard_ref.page_options.chk_desktop.isChecked(),
            "startmenu": self.wizard_ref.page_options.chk_start.isChecked(),
            "browser": self.wizard_ref.page_options.chk_browser.isChecked(),
            "frozen": self.wizard_ref.frozen,
        }
        self.worker = BuildWorker(plan, self)
        self.worker.log.connect(self._on_log)
        self.worker.progress.connect(self.bar.setValue)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_log(self, text: str):
        self.log_box.appendPlainText(text)
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum())

    def _on_done(self, ok: bool, results: list):
        self.wizard_ref.results = results
        lines = [("✓ " if flag else "✗ ") + text for flag, text in results]
        self.log_box.appendPlainText("\n" + "\n".join(lines))
        self.status.setText("全部完成 ✓ 点【完成】就行" if ok else
                            "有几步没成功（上面带 ✗ 的那些），点【完成】关闭后可以重跑")
        self.wizard_ref.button(QWizard.WizardButton.FinishButton).setEnabled(True)
        self.wizard_ref.button(QWizard.WizardButton.BackButton).setEnabled(not ok)
        self.wizard_ref.button(QWizard.WizardButton.CancelButton).setEnabled(True)
        if not ok:
            QMessageBox.warning(self, "有步骤没成功",
                                "\n".join(l for l in lines if l.startswith("✗")))


class SetupWizard(QWizard):
    """安装向导。"""

    def __init__(self, first_run: bool = False, parent=None):
        super().__init__(parent)
        self.first_run = first_run
        self.frozen = bool(getattr(sys, "frozen", False))
        self.results: List[Tuple[bool, str]] = []
        self.default_data_dir = self._default_data_dir()
        self.default_install_dir = self._default_install_dir()

        self.setWindowTitle(f"{paths.APP_NAME} 安装向导")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(760, 600)
        icon = paths.icon_file()
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步 >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< 上一步")
        self.setButtonText(QWizard.WizardButton.FinishButton, "完成")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")

        self.page_welcome = _WelcomePage(self)
        self.page_dirs = _DirsPage(self)
        self.page_options = _OptionsPage(self)
        self.page_build = _BuildPage(self)
        for page in (self.page_welcome, self.page_dirs, self.page_options, self.page_build):
            self.addPage(page)

    # ------------------------------
    # 小工具
    # ------------------------------
    @staticmethod
    def _default_data_dir() -> str:
        cfg = paths.load_config().get("data_dir")
        if cfg:
            return str(cfg)
        if (paths.app_dir() / "projects").is_dir():
            return str(paths.app_dir())          # 绿色版：程序旁边
        return str(paths.CONFIG_DIR)

    @staticmethod
    def _default_install_dir() -> str:
        import os

        base = os.environ.get("ProgramFiles") or r"C:\Program Files"
        return str(Path(base) / paths.APP_NAME)

    def data_dir(self) -> str:
        return self.page_dirs.data_edit.text().strip()

    def install_dir(self) -> Optional[str]:
        edit = self.page_dirs.install_edit
        return edit.text().strip() if edit is not None else None

    @staticmethod
    def with_button(edit: QLineEdit, button: QPushButton) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        lay.addWidget(button)
        return box

    def pick_dir(self, edit: QLineEdit, title: str):
        folder = QFileDialog.getExistingDirectory(self, title, edit.text())
        if folder:
            edit.setText(folder)

    # ------------------------------
    def accept(self):
        """点【完成】：可选立即启动，然后关闭向导。"""
        super().accept()
        if self.results and all(flag for flag, _ in self.results) \
                and self.page_options.chk_launch.isChecked():
            self._launch()

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
