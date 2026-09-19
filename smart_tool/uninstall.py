# -*- coding: utf-8 -*-
"""卸载「小邹RPA」：把装进系统里的东西清干净。

谁会用到这里：
    · 用户在「设置 → 应用 → 小邹RPA → 卸载」点卸载（注册表里的 UninstallString 就是它）
    · 命令行：  <安装目录>\\小邹RPA.exe --uninstall
               <安装目录>\\小邹RPA.exe --uninstall --silent           静默，保留用户数据
               <安装目录>\\小邹RPA.exe --uninstall --silent --purge   静默 + 连用户数据一起删
    · 源码运行：.venv\\Scripts\\python -m smart_tool.uninstall

清什么、不清什么（程序和数据在同一个文件夹里，绿色版）：
    清    快捷方式、注册表卸载项、配置目录（%APPDATA%\\小邹RPA）、**程序那个 exe**
    不清  程序文件夹本身，也不动文件夹里别的东西
    要另外勾 项目数据（那个文件夹里的 projects/）—— 默认不勾，删了账号密码就没了
    要另外勾 浏览器内核（%LOCALAPPDATA%\\ms-playwright）—— 别的程序也可能在用

exe 正在运行，Windows 不让删自己（连加载过的 dll 都锁着）。所以真正的删除交给一个
临时 .bat：它先等几秒（那时本进程已退出），再 del 掉 exe / rd 掉目录，删完把自己也删掉。
"""
import argparse
import codecs
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from smart_tool import paths
from smart_tool.core import browser_setup, shortcut, uninstall_reg


# ============================================================
# 卸载清单：要清哪些东西
# ============================================================
@dataclass
class Item:
    """清单里的一项。"""

    key: str
    label: str
    targets: List[Path] = field(default_factory=list)
    note: str = ""
    default: bool = True
    removable: bool = True          # 允许用户取消勾选
    size: int = 0                   # 体积（字节），0＝不显示
    danger: bool = False            # 删了找不回来的（用户数据、浏览器内核）
    # paths   直接删这些文件/目录
    # program 程序目录（正在运行的 exe 锁着，交给临时脚本删）
    # registry 注册表里的卸载入口
    # note    只提示不删（比如"安装位置没登记，程序文件请手动删"）
    kind: str = "paths"


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _shortcut_files(cfg: dict) -> List[Path]:
    """桌面 / 开始菜单里那个快捷方式。安装时记在配置里，配置丢了就按默认位置找。"""
    found: List[Path] = []
    recorded = [Path(str(p)) for p in (cfg.get("shortcut_paths") or [])]
    for p in recorded:
        if p.is_file():
            found.append(p)
    if not recorded:                    # 没有记录才去问系统（省一次 PowerShell 启动）
        for folder in (shortcut.desktop_dir(), shortcut.start_menu_dir()):
            p = folder / f"{paths.APP_NAME}.lnk"
            if p.is_file():
                found.append(p)
    return found


def _looks_like_install_dir(path: Path) -> bool:
    """这个目录看着像本程序所在的地方吗（有同名 exe 或 _internal 文件夹）。

    为什么要有这道检查：万一配置丢了/读坏了，"程序在哪儿"就没法确定。
    那时候宁可不动程序本体（让用户手动删），也绝不能凭猜去删别人一个文件夹。
    """
    try:
        if not path.is_dir():
            return False
        if (path / Path(sys.executable).name).is_file():
            return True
        return (path / "_internal").is_dir()      # onedir 打包时的目录结构
    except OSError:
        return False


def _program_files(install_dir: Path) -> List[Path]:
    """程序**自己**的那几个文件：exe（单文件版就只有它）+ onedir 版的 _internal。

    注意：程序和数据在同一个文件夹里，所以这里绝对不能把整个文件夹当程序删掉，
    否则用户的项目、账号密码、采集结果会跟着一起没。
    """
    files: List[Path] = []
    try:
        exe = install_dir / Path(sys.executable).name
        if exe.is_file():
            files.append(exe)
        internal = install_dir / "_internal"
        if internal.is_dir():
            files.append(internal)
    except OSError:
        pass
    return files


def build_plan() -> List[Item]:
    """看看这台机器上装了哪些东西，列成清单（只看不删）。"""
    cfg = paths.load_config()
    frozen = bool(getattr(sys, "frozen", False))
    items: List[Item] = []
    install_dir: Optional[Path] = None

    # 1) 程序本体（打包安装才有；源码运行不动仓库）
    if frozen:
        recorded = str(cfg.get("install_dir") or "").strip()
        install = Path(recorded) if recorded else None
        if install is not None and _looks_like_install_dir(install):
            install_dir = install
            targets = _program_files(install)
            items.append(Item(
                "program", "程序本体（就那个 exe）", targets,
                note=f"{install}　窗口关掉后由后台小脚本删除"
                     "（正在运行的 exe 删不掉自己）；这个文件夹里的项目数据不在这里删",
                size=sum(uninstall_reg.dir_size(t) for t in targets),
                kind="program"))
        else:
            # 没登记过安装位置（老版本装的，或者配置文件丢过）→ 不猜、不动
            items.append(Item(
                "program", "程序本体", removable=False, kind="note",
                note="没登记安装位置，卸载不会自动删；要清理就手动删掉程序那个 exe："
                     f"{paths.app_dir()}"))

    # 2) 快捷方式
    lnks = _shortcut_files(cfg)
    if lnks:
        items.append(Item("shortcut", "快捷方式（桌面 / 开始菜单）", lnks,
                          note="、".join(str(p) for p in lnks)))

    # 3) 配置目录 + 项目数据
    #    正常安装（程序和数据在同一个文件夹）时，数据只删那个文件夹里的 projects/，
    #    别动文件夹本身——文件夹里还有用户自己放的别的东西。
    data_dir = Path(str(cfg.get("data_dir") or paths.DATA_DIR))
    if _same(data_dir, paths.CONFIG_DIR):
        # 老式安装：数据就放在配置目录里 → 合成一项说清楚
        items.append(Item(
            "config", "配置目录 + 用户数据（项目、账号密码、图片库、采集结果、登录态）",
            [paths.CONFIG_DIR],
            note=f"{paths.CONFIG_DIR}　数据就在这里面，删了找不回来",
            default=False, danger=True))
    else:
        items.append(Item("config", "配置目录（config.json、图标缓存）",
                          [paths.CONFIG_DIR], note=str(paths.CONFIG_DIR)))
        if install_dir is not None and _same(data_dir, install_dir):
            items.append(Item(
                "data", "项目数据（项目、账号密码、图片库、采集结果、登录态）",
                [data_dir / "projects"],
                note=f"{data_dir}\\projects　就在程序那个文件夹里，"
                     "程序本体和它分开删；不删的话下次安装还能接着用",
                default=False, danger=True))
        else:
            items.append(Item(
                "data", "用户数据（项目、账号密码、图片库、采集结果、登录态）",
                [data_dir], note=f"{data_dir}　不删的话下次安装还能接着用",
                default=False, danger=True))

    # 4) 浏览器内核（Chromium）
    if browser_setup.is_installed():
        bdir = browser_setup.browsers_dir()
        items.append(Item(
            "browser", "浏览器内核 Chromium", [bdir],
            note=f"{bdir}　别的程序（比如其它 Playwright 工具）也可能在用，"
                 "删了要重新下",
            default=False, danger=True, size=uninstall_reg.dir_size(bdir)))

    # 5) 注册表卸载入口：必须删，不然系统里会留个点了没反应的条目
    items.append(Item(
        "registry", "注册表卸载入口", note="必须删：留着的话系统里会多一条坏掉的条目",
        removable=False, kind="registry"))
    return items


# ============================================================
# 真正动手删
# ============================================================
def _on_rm_error(func, path, _exc):
    """只读文件删不掉：去掉只读属性再试一次。"""
    try:
        Path(path).chmod(stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove_paths(targets: List[Path]) -> Tuple[bool, str]:
    """删一组文件 / 目录，返回 (删干净了?, 说明)。"""
    removed = 0
    failed: List[str] = []
    for p in targets:
        try:
            if p.is_dir():
                shutil.rmtree(p, onerror=_on_rm_error)
                removed += 1
            elif p.exists():
                p.chmod(stat.S_IWRITE)
                p.unlink()
                removed += 1
        except OSError as e:
            reason = getattr(e, "strerror", None) or str(e)
            failed.append(f"{p}（{reason}）")
    if failed:
        return False, "有东西没删掉：" + "；".join(failed[:3]) + \
            "（程序还在运行的话，关掉再重试一次）"
    if not removed:
        return True, "本来就没有，跳过"
    return True, f"已删除（{removed} 项）"


def _write_delete_bat(targets: List[Path], delay_s: int) -> Path:
    """写一个「延迟删东西」的小脚本，返回脚本路径。

    目录用 rd，文件（比如正在运行的 exe）用 del。
    """
    def commands() -> List[str]:
        lines = []
        for t in targets:
            if t.is_dir():
                lines.append(f'rd /s /q "{t}" 2>nul')
            else:
                lines.append(f'del /f /q "{t}" 2>nul')
        return lines

    body = "\r\n".join(
        ["@echo off",
         f"ping -n {delay_s} 127.0.0.1 > nul"]        # 等本进程退出（那时 exe/dll 才解锁）
        + commands()
        + ["ping -n 2 127.0.0.1 > nul"]               # 没删干净就再补一刀
        + commands()
        # 脚本把自己也删掉。两个讲究（都是实机踩出来的）：
        #   · 每条删除命令后面必须 2>nul：第二次删会因为东西已经没了而报错，
        #     报错输出没被吞掉的话，它后面的行（包括这行自删）根本不执行；
        #   · 前面的 (goto) 2>nul 让 cmd 跳到文件末尾、松开文件句柄，del 才删得掉。
        + ['(goto) 2>nul & del /f /q "%~f0"']) + "\r\n"
    bat = Path(tempfile.gettempdir()) / f"{paths.APP_NAME}-卸载.bat"
    try:
        # cmd 默认按系统 ANSI 读批处理（中文系统＝GBK），这样中文路径才不会乱码。
        # 必须写字节：用文本模式写会把 \n 再转一次，每行末尾多出个 \r，
        # `del "%~f0"` 就会带上这个字符导致删不掉。
        bat.write_bytes(body.encode("mbcs"))
    except (LookupError, UnicodeEncodeError):
        bat.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
    return bat


def _schedule_delete(targets: List[Path], delay_s: int = 4) -> Optional[Path]:
    """把删除交给临时脚本去办（本进程退出后才动手，那时 exe/dll 才解锁）。"""
    paths_to_go = [Path(t) for t in targets]
    if sys.platform != "win32":                     # 非 Windows 没这麻烦，直接删
        _remove_paths(paths_to_go)
        return None
    bat = _write_delete_bat(paths_to_go, delay_s)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", str(bat)], creationflags=flags,
                     close_fds=True)
    return bat


def execute(items: List[Item], log: Callable[[str], None] = print) -> List[Tuple[bool, str]]:
    """按清单逐项清理，返回 [(成功?, 说明), ...]（界面和静默模式共用）。"""
    results: List[Tuple[bool, str]] = []
    for item in items:
        try:
            if item.kind == "registry":
                r = uninstall_reg.unregister()
                results.append((bool(r["ok"]),
                                "注册表卸载入口：" + ("已删除" if r["ok"] else str(r["error"]))))
            elif item.kind == "note":
                # 只是告诉用户"这一项没自动删"，什么都不动
                results.append((True, f"{item.label}：{item.note}"))
            elif item.kind == "program":
                log("程序本体交给后台小脚本删除：" +
                    "、".join(str(t) for t in item.targets))
                _schedule_delete(item.targets)
                results.append((True, "程序本体："
                                      + "、".join(str(t) for t in item.targets)
                                      + "（窗口关掉后自动删除，大约几秒）"))
            else:
                ok, msg = _remove_paths(item.targets)
                results.append((ok, f"{item.label}：{msg}"))
        except Exception as e:                       # 兜底：一项出错不影响其它项
            results.append((False, f"{item.label}：删除出错 {type(e).__name__}: {e}"))
        log(("✓ " if results[-1][0] else "✗ ") + results[-1][1])
    return results


# ============================================================
# 卸载界面
# ============================================================
class UninstallWorker(QThread):
    """后台删，界面不卡。"""

    log = pyqtSignal(str)
    done = pyqtSignal(bool, list)

    def __init__(self, items: List[Item], parent=None):
        super().__init__(parent)
        self.items = items

    def run(self):
        try:
            results = execute(self.items, self.log.emit)
        except Exception as e:
            results = [(False, f"卸载出错：{type(e).__name__}: {e}")]
        self.done.emit(all(flag for flag, _ in results), results)


class UninstallDialog(QDialog):
    """勾一勾 → 开始卸载。"""

    def __init__(self, purge: bool = False, parent=None):
        super().__init__(parent)
        self.items = build_plan()
        if purge:
            for it in self.items:
                it.default = True
        self.checks: Dict[str, QCheckBox] = {}
        self.worker: Optional[UninstallWorker] = None
        self.finished_ok = False

        self.setWindowTitle(f"卸载 {paths.APP_NAME}")
        self.setMinimumSize(660, 560)
        icon = paths.icon_file()
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))

        root = QVBoxLayout(self)
        title = QLabel(f"卸载「{paths.APP_NAME}」")
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        root.addWidget(title)
        tip = QLabel("勾上要清掉的东西，点【开始卸载】。"
                     "红色那几项删了就找不回来，默认没勾，自己确认过再勾。")
        tip.setStyleSheet("color:#666;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        for item in self.items:
            root.addWidget(self._row(item))
        root.addStretch(1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)                      # 不确定进度，转圈就行
        self.bar.setVisible(False)
        root.addWidget(self.bar)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(150)
        self.log_box.setPlaceholderText("清理过程中的日志会显示在这里…")
        root.addWidget(self.log_box, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_go = QPushButton("开始卸载")
        self.btn_go.setStyleSheet(
            "QPushButton{background:#c62828;color:#fff;padding:6px 18px;"
            "border-radius:4px;} QPushButton:disabled{background:#9e9e9e;}")
        self.btn_go.clicked.connect(self._start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._close_clicked)
        row.addWidget(self.btn_go)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

        # 程序还在跑的话，文件是锁着的，先说清楚
        if getattr(sys, "frozen", False):
            self.status.setText("提示：卸载前请先关掉正在运行的小邹RPA 主窗口。")

    def _row(self, item: Item) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(1)
        chk = QCheckBox(item.label)
        chk.setChecked(item.default)
        if not item.removable:
            chk.setEnabled(False)
        if item.danger:
            chk.setStyleSheet("color:#c62828;")
        lay.addWidget(chk)
        info = item.note
        if item.size:
            info = f"{uninstall_reg.human_size(item.size)}　{info}"
        lab = QLabel("　　" + info)
        lab.setStyleSheet("color:#666; font-size:11px;")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.checks[item.key] = chk
        return box

    def _chosen(self) -> List[Item]:
        return [it for it in self.items if self.checks[it.key].isChecked()]

    def _start(self):
        chosen = self._chosen()
        if not chosen:
            QMessageBox.information(self, "提示", "一项都没勾，那就没什么可清的了。")
            return
        danger = [it.label for it in chosen if it.danger]
        if danger:
            answer = QMessageBox.warning(
                self, "再确认一次",
                "你勾了这几项，删了找不回来：\n\n  · " + "\n  · ".join(danger) +
                "\n\n确定要一起删掉吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        for chk in self.checks.values():
            chk.setEnabled(False)
        self.btn_go.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.bar.setVisible(True)
        self.status.setText("正在清理…")
        self.worker = UninstallWorker(chosen, self)
        self.worker.log.connect(self._on_log)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_log(self, text: str):
        self.log_box.appendPlainText(text)
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum())

    def _on_done(self, ok: bool, results: list):
        self.bar.setVisible(False)
        self.finished_ok = ok
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("关闭")
        self.status.setText("清理完成 ✓ 点【关闭】就行" if ok else
                            "有几项没清干净（上面带 ✗ 的），关上窗口再跑一次试试")
        if not ok:
            QMessageBox.warning(self, "有东西没删掉",
                                "\n".join(t for f, t in results if not f))

    def _close_clicked(self):
        """清理还没开始时是【取消】，清完变成【关闭】。"""
        self.accept() if self.finished_ok else self.reject()


# ============================================================
# 入口
# ============================================================
def _silent_run(purge: bool) -> int:
    """静默卸载：不弹界面，只往标准输出打日志（给命令行 / 自动化用）。

    默认**保留**用户数据和浏览器内核，只有加 --purge 才一起删。
    """
    plan = build_plan()
    selected = [it for it in plan if purge or it.key not in ("data", "browser")]
    keys = {it.key for it in selected}
    for item in plan:
        if item.key not in keys:
            print(f"— 保留：{item.label}（想一起删就加 --purge）")
    results = execute(selected)
    ok = all(flag for flag, _ in results)
    print("卸载完成 ✓" if ok else "卸载没完全成功，看上面的 ✗")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    """卸载入口：`<程序> --uninstall [--silent] [--purge]`。"""
    parser = argparse.ArgumentParser(
        prog=f"{paths.APP_NAME} 卸载程序", add_help=True,
        description="清掉安装时写进系统的东西（快捷方式、程序文件、配置、卸载列表条目）。")
    parser.add_argument("--uninstall", action="store_true", help="卸载（就是本程序）")
    parser.add_argument("--silent", action="store_true",
                        help="不弹界面，直接清理（保留用户数据）")
    parser.add_argument("--purge", action="store_true",
                        help="连用户数据、浏览器内核一起删（慎用）")
    args, _unknown = parser.parse_known_args(
        sys.argv[1:] if argv is None else list(argv))

    if args.silent:
        return _silent_run(args.purge)

    app = QApplication.instance() or QApplication(sys.argv)
    icon = paths.icon_file()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    UninstallDialog(purge=args.purge).exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
