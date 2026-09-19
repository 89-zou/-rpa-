# -*- coding: utf-8 -*-
"""安装向导：引导选文件夹 → 环境构建 → 建快捷方式 → 启动程序。

用户拿到 exe 以后看到的第一个界面就是它（第一次运行自动弹）：
    1. 【欢迎】说明这个小工具能干什么；
    2. 【选一个文件夹】程序和数据都放这儿（默认＝程序现在待的位置，
       不硬推 C 盘；也可以自己挑，选好之后程序会把自己复制过去）；
    3. 【选项】桌面快捷方式（默认勾上，强烈建议留着）、开始菜单；
    4. 【安装】开始环境构建，实时日志：
          · 写配置、建项目目录
          · 把内置的演示项目复制到你的文件夹（启动后会默认打开它）
          · 下载浏览器内核 Chromium（打包版没有自带，约 150 MB）
          · 把自己复制到目标文件夹、建带 logo 的快捷方式
          · 登记到 Windows 的卸载列表（设置 → 应用 里能看到「小邹RPA」，点卸载走
            `smart_tool/uninstall.py`，会把快捷方式、程序本体、配置一并清掉）
    5. 完成后可以直接启动程序。

程序和数据在同一个文件夹里（绿色版）：换电脑、拷 U 盘，整个文件夹搬走就行。

写不进去系统的情况（没权限、注册表被策略锁住）不报错，直接退成「免安装模式」：
程序留在原地，只把文件夹和环境构建好，不碰注册表。

另外也能当独立入口跑：`小邹RPA.exe --setup`（随时重跑，比如补装浏览器）。
"""
import shutil
import subprocess
import sys
from datetime import datetime
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
from smart_tool.core import browser_setup, shortcut, uninstall_reg


# ============================================================
# 环境构建（在后台线程里跑，界面不卡）
# ============================================================
class BuildWorker(QThread):
    """按计划做环境构建：写配置 → 建目录 → 演示项目 → 浏览器内核 → 快捷方式。

    写不进去系统的机器（没管理员权限、注册表被策略锁了之类）不报错，直接退成
    「免安装模式」：程序留在原地，只把用户数据目录和环境构建好，不碰注册表。
    """

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    #: (还能继续用?, [(成功?, 说明), ...])
    done = pyqtSignal(bool, list)

    def __init__(self, plan: Dict, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.results: List[Tuple[bool, str]] = []
        #: 关键步骤（写配置、建数据目录）失败 → 程序没法正常用
        self.fatal = False
        #: 是不是免安装模式（没往系统里装）
        self.portable = False
        #: 程序被复制到别的位置时，那边的启动目标（用来重新启动）
        self.new_target: Optional[Dict[str, str]] = None
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
            self.fatal = True
        self.progress.emit(100)
        self.done.emit(not self.fatal, self.results)

    # ------------------------------
    def _build(self):
        plan = self.plan
        # 程序和数据就放同一个文件夹（用户只指定这一个目录）
        target_dir = Path(str(plan["target_dir"])).expanduser()
        self._step(6, f"安装位置：{target_dir}")

        # 1) 写配置：数据目录 = 这个文件夹（绿色版），以及快捷方式选项、默认项目
        try:
            paths.set_data_dir(target_dir)
            paths.save_config(
                installed=True,
                desktop_shortcut=bool(plan.get("desktop")),
                startmenu_shortcut=bool(plan.get("startmenu")),
                default_project=paths.DEMO_PROJECT_NAME,
            )
            self.results.append((True, f"配置已写入：{paths.CONFIG_FILE}"))
        except OSError as e:
            self.fatal = True
            self.results.append((False, f"写配置失败：{e}（可能是目录没权限）"))

        # 2) 建目录
        projects = target_dir / "projects"
        try:
            projects.mkdir(parents=True, exist_ok=True)
            (target_dir / "logs").mkdir(parents=True, exist_ok=True)
            self._step(16, f"项目目录：{projects}")
            self.results.append((True, f"项目目录就绪：{projects}"))
        except OSError as e:
            self.fatal = True
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

        # 5) 把程序放到你选的文件夹里 + 建快捷方式 + 登记卸载入口。
        #    先探一下能不能放：放不进去（没权限之类）就退成免安装模式——
        #    程序留在原地用，只把项目目录和环境构建好，一个字节都不写系统。
        self._step(90, "正在准备安装位置…")
        can_copy, can_reg, reason = self._probe()
        self.portable = not (can_copy and can_reg)
        if self.portable:
            self.log.emit(f"免安装模式：{reason}")
            self.log.emit("程序就留在原地用；以后卸载时直接把它删掉就行。")
            self.results.append((True, f"免安装模式（不写入系统）：{reason}"))
            target = shortcut.launch_target()
        else:
            target = self._copy_program() or shortcut.launch_target()

        self._step(92, "正在建快捷方式…")
        icon = paths.icon_file()
        icon_text = str(icon) if icon.is_file() else ""
        lnk_paths: List[str] = []
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
                lnk_paths.append(r["path"])
                self.results.append((True, f"{label}：{r['path']}"))
            else:
                # 快捷方式建不上不算事：桌面/开始菜单权限被锁的机器照样能用
                self.results.append(
                    (False, f"{label}创建失败：{r['error']}（不影响使用，可手动建）"))

        # 6) 记下程序在哪儿 + 登记到 Windows 卸载列表
        self._step(96, "正在登记卸载信息…")
        install_dir = ""
        if not self.portable and not str(target.get("args") or "").strip():
            install_dir = str(Path(target["target"]).resolve().parent)
        paths.save_config(install_dir=install_dir,
                          shortcut_paths=lnk_paths,
                          portable=self.portable,
                          installed_at=f"{datetime.now():%Y-%m-%d %H:%M}")
        if self.portable or not can_reg:
            if paths.is_production():
                msg = ("没有登记系统卸载入口：以后不想要了，删掉程序本体和文件夹即可"
                       f"（程序在 {Path(target['target']).parent}）")
            else:
                msg = "开发版（源码运行）：不写系统卸载入口，只把项目目录和环境构建好"
            self.results.append((True, msg))
        else:
            info = uninstall_reg.register(
                target, size_bytes=uninstall_reg.dir_size(Path(target["target"])))
            if info["ok"]:
                self.results.append(
                    (True, "已登记到系统卸载列表（设置 → 应用 → 小邹RPA 里可以卸载）"))
            else:
                # 登记失败也能用：告诉用户手动删就是卸载
                self.results.append(
                    (False, f"登记卸载入口失败：{info['error']}"
                            "（不影响使用，卸载时删掉程序文件夹即可）"))
        self._step(98)

        # 程序被复制到了别处：记下来，构建完直接启动那边的，别在这边"假装装好了"
        if not self.portable and \
                str(target["target"]) != str(shortcut.launch_target()["target"]):
            self.new_target = target

    # ------------------------------
    def _probe(self) -> Tuple[bool, bool, str]:
        """探一探这台机器能不能把程序放进你选的目录、能不能写卸载列表。

        返回 (能放进去?, 能写注册表?, 说明)。放不进去不算失败——降级免安装即可。
        """
        target_dir = Path(str(self.plan.get("target_dir") or "")).expanduser()
        frozen = bool(self.plan.get("frozen"))
        can_reg = uninstall_reg.can_register()
        no_reg = "" if can_reg else "这台机器写不了注册表（受限账户或组策略限制）"
        if not frozen:
            # 开发版（源码运行）：只把项目目录和环境构建好。往系统里装东西
            # （复制程序、登记卸载入口）是交付给用户时才做的事。
            return True, False, "源码运行（开发版）：不复制程序文件、不写系统卸载入口"
        if not _dir_writable(target_dir):
            return False, can_reg, f"{target_dir} 里写不进东西（换个文件夹，或用管理员身份运行）"
        return True, can_reg, no_reg

    def _copy_program(self) -> Optional[Dict[str, str]]:
        """把程序（单文件 exe）复制到你选的目录，返回那边的启动目标。"""
        if not self.plan.get("frozen"):
            return None
        target_dir = Path(str(self.plan.get("target_dir"))).expanduser()
        src = Path(sys.executable).resolve()
        dst = target_dir / src.name
        try:
            if dst.exists() and dst.resolve() == src.resolve():
                self.results.append((True, f"程序已经在这个文件夹里：{src}"))
                return None
            shutil.copy2(src, dst)
        except OSError as e:
            # 复制不了也不报错：退回免安装模式
            self.portable = True
            self.results.append((False, f"复制程序到 {target_dir} 失败：{e}"))
            self.log.emit("复制不过去，改用免安装模式：程序留在原地用。")
            return None
        self.results.append((True, f"程序已放到：{dst}"))
        return {"target": str(dst), "args": "", "workdir": str(target_dir)}

    def _browser_log(self, text: str):
        """下载浏览器的输出：日志照发，进度条也跟着动一动。"""
        if text:
            self.log.emit("  " + text)
        self.progress.emit(min(86, self._bar + 1))
        self._bar = min(86, self._bar + 1)


def _dir_writable(path: Path) -> bool:
    """这个目录能不能写（不存在就顺手建一下）。装到 Program Files 时用得上：
    没有管理员权限的机器写不进去，那就别硬装，直接走免安装模式。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".小邹RPA-写入测试.tmp"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


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
            "  1. 选一个文件夹：程序和你所有的项目数据都放在这里面（就一个目录）；\n"
            "  2. 环境构建：自动把内置演示项目装好、把浏览器内核下好；\n"
            "  3. 建一个带 logo 的桌面快捷方式，以后双击就能启动。\n\n"
            "装完就能直接用：打开程序会默认载入那个演示项目（不想要了随时删，\n"
            "删掉以后就是一个空项目，自己新建即可）。\n"
            "整个文件夹随时可以拷到 U 盘或另一台电脑，项目数据跟着一起走。"
        )
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addStretch(1)


class _DirsPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("选一个文件夹")
        self.setSubTitle("程序和你的项目数据都放这个文件夹里；默认值是程序现在所在的位置")
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.dir_edit = QLineEdit(wizard.default_dir)
        btn = QPushButton("浏览…")
        btn.clicked.connect(lambda: wizard.pick_dir(self.dir_edit, "选一个文件夹"))
        form.addRow("安装位置：", wizard.with_button(self.dir_edit, btn))
        root.addLayout(form)

        hint = QLabel(
            "· 就这一个目录，程序文件、你的项目（含账号密码）、图片库、采集结果、\n"
            "  登录态 cookie 全在里面；卸载 = 把这个文件夹删掉。\n"
            "· 换个地方也行（比如 D 盘、U 盘）：点【浏览…】选好，程序会把自己复制过去。\n"
            "· 写不进去的目录（比如没权限的系统目录）会自动改用免安装模式：程序留在\n"
            "  原地、不写注册表，只把项目目录和环境构建好。\n"
            + ("" if wizard.frozen else
               f"· 源码运行模式：不会复制程序文件，程序现在在 {paths.app_dir()}"))
        hint.setStyleSheet("color:#666;")
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch(1)

    def validatePage(self) -> bool:
        text = self.dir_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先选一个文件夹")
            return False
        try:
            Path(text).expanduser().mkdir(parents=True, exist_ok=True)
            probe = Path(text).expanduser() / ".小邹RPA-写入测试.tmp"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            QMessageBox.warning(self, "这个目录用不了",
                                f"{text}\n\n{e}\n\n换一个能写的目录吧。")
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
        # 第一次运行时，向导关掉就直接进主界面（由 main.py 接管），不需要再启动一次，
        # 否则会同时开两个程序
        self.chk_launch = QCheckBox(
            "构建完成后直接进入程序" if wizard.first_run else "安装完成后立即启动程序")
        self.chk_launch.setChecked(True)
        if wizard.first_run:
            self.chk_launch.setEnabled(False)
        for w in (self.chk_desktop, tip, self.chk_start, self.chk_browser, self.chk_launch):
            lay.addWidget(w)
        lay.addStretch(1)


class _BuildPage(QWizardPage):
    """安装页：进来自动开始环境构建，日志实时刷。"""

    def __init__(self, wizard):
        super().__init__()
        self.wizard_ref = wizard
        #: 关键步骤都成了没（写配置、建数据目录）——决定构建完能不能直接用
        self.can_continue = False
        #: 程序被复制到别的位置时的启动目标
        self.new_target: Optional[Dict[str, str]] = None
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

    def initializePage(self):
        # 注意：PyQt6 这个钩子不带参数（Qt 原版就是 initializePage()），
        # 写成 initializePage(self, page_id) 会在进入本页时直接抛 TypeError
        super().initializePage()
        buttons = self.wizard_ref.button
        buttons(QWizard.WizardButton.FinishButton).setEnabled(False)
        buttons(QWizard.WizardButton.BackButton).setEnabled(False)
        buttons(QWizard.WizardButton.CancelButton).setEnabled(False)
        self.status.setText("开始环境构建…")
        self.bar.setValue(2)
        self.log_box.setPlainText("")

        plan = {
            "target_dir": self.wizard_ref.target_dir(),
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
        """ok = 关键步骤都成了（非关键的那几步失败也能接着用）。"""
        self.wizard_ref.results = results
        self.can_continue = ok
        self.new_target = getattr(self.worker, "new_target", None)
        lines = [("✓ " if flag else "✗ ") + text for flag, text in results]
        warn = [l for l in lines if l.startswith("✗")]
        self.log_box.appendPlainText("\n" + "\n".join(lines))
        if ok and not warn:
            self.status.setText("全部完成 ✓ 点【完成】就行")
        elif ok:
            self.status.setText("可以用了 ✓ 上面带 ✗ 的那几步没成功，不影响使用")
        else:
            self.status.setText("关键步骤没成功（上面带 ✗ 的），先别急着用，点【完成】后可以重跑")
        self.wizard_ref.button(QWizard.WizardButton.FinishButton).setEnabled(True)
        self.wizard_ref.button(QWizard.WizardButton.BackButton).setEnabled(not ok)
        self.wizard_ref.button(QWizard.WizardButton.CancelButton).setEnabled(True)
        if not ok:
            QMessageBox.warning(self, "有步骤没成功",
                                "\n".join(warn) +
                                "\n\n（点【完成】关掉向导后可以重跑一次）")
        elif warn and self.wizard_ref.first_run:
            QMessageBox.information(
                self, "装好了，有几处没成功",
                "\n".join(warn) + "\n\n这些不影响使用，程序照常打开。")


class SetupWizard(QWizard):
    """安装向导。"""

    def __init__(self, first_run: bool = False, parent=None):
        super().__init__(parent)
        self.first_run = first_run
        self.frozen = bool(getattr(sys, "frozen", False))
        self.results: List[Tuple[bool, str]] = []
        #: 程序被复制到别的安装目录时，那边的启动目标（main.py 用它来重新启动）
        self.relaunch_target: Optional[Dict[str, str]] = None
        self.default_dir = self._default_dir()

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
    def _default_dir() -> str:
        """默认安装位置 = 程序现在待的地方（用户自己放在哪就是哪，不硬推 C 盘）。"""
        config_dir = str(paths.load_config().get("data_dir") or "").strip()
        if config_dir:
            return config_dir
        return str(paths.app_dir())

    def target_dir(self) -> str:
        """用户选的那个文件夹（程序和数据都在里面）。"""
        return self.page_dirs.dir_edit.text().strip()

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
        """点【完成】：关掉向导，必要时启动（或换个位置启动）程序。

        · 第一次运行：不在这里启动——main.py 会接着往下走（或者按 relaunch_target
          去启动新位置的程序），免得同时开出两个程序；
        · 手动重跑向导：按【选项】里的勾选决定要不要启动。
        """
        page = self.page_build
        can_continue = page.can_continue
        if can_continue and page.new_target:
            # 程序被复制到了安装目录：去那边启动
            self.relaunch_target = page.new_target
        super().accept()
        if not can_continue:
            return
        if self.first_run:
            return                       # 交给 main.py 处理
        if self.page_options.chk_launch.isChecked():
            self._launch(self.relaunch_target or shortcut.launch_target())

    def _launch(self, target: Optional[Dict[str, str]] = None):
        t = target or shortcut.launch_target()
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
