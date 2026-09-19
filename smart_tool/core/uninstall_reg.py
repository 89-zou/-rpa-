# -*- coding: utf-8 -*-
"""把「小邹RPA」登记进 Windows 的卸载列表（设置 → 应用 / 控制面板 → 程序和功能）。

为什么需要：光复制文件 + 建快捷方式，用户在系统里根本找不到卸载入口，
删文件只能自己摸索着找目录。登记一下，系统里就会出现【小邹RPA】这一条，
点【卸载】执行的就是我们自己的卸载程序（smart_tool/uninstall.py）。

写的是 **当前用户** 的卸载键，不需要管理员权限，装到 Program Files 也能登记：
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\小邹RPA
（用标准库 winreg，不额外装 pywin32）

顺便放了 dir_size()：登记时算体积、卸载界面显示体积都要用。
"""
import os
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

from smart_tool import __version__, paths

#: 系统卸载列表所在的注册表位置（当前用户级）
UNINSTALL_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def supported() -> bool:
    """只有 Windows 才有这套卸载列表。"""
    return sys.platform == "win32"


def key_name() -> str:
    return paths.APP_NAME


def entry_path() -> str:
    """完整注册表路径（就在 HKCU 下面）。"""
    return f"{UNINSTALL_ROOT}\\{key_name()}"


def _winreg():
    import winreg
    return winreg


# ------------------------------
# 体积统计（登记 EstimatedSize 用）
# ------------------------------
def dir_size(path, budget: int = 0) -> int:
    """统计目录/文件的体积（字节）。budget>0 时够数就提前收工，省得在几万文件上磨。"""
    p = Path(path)
    try:
        if p.is_file():
            return p.stat().st_size
    except OSError:
        return 0
    if not p.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:                       # 删着删着就没了，忽略
                continue
        if budget and total >= budget:
            return total
    return total


def human_size(size: int) -> str:
    """给人看的体积：1.2 GB / 156 MB / 12 KB。"""
    num = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


# ------------------------------
# 卸载命令：用户在系统里点【卸载】时执行什么
# ------------------------------
def uninstall_commands(launch: dict) -> Tuple[str, str]:
    """算出 (带界面的卸载命令, 静默卸载命令)。

    launch 就是 shortcut.launch_target() 的返回值：
    · 打包版  {"target": "...\\小邹RPA.exe", "args": ""}
              → `"...\\小邹RPA.exe" --uninstall`
    · 源码版  {"target": "...\\pythonw.exe",  "args": "-m smart_tool.main"}
              → `"...\\pythonw.exe" -m smart_tool.uninstall`
    """
    target = str(launch.get("target") or "")
    args = str(launch.get("args") or "").strip()
    base = f'"{target}" --uninstall' if not args else f'"{target}" -m smart_tool.uninstall'
    return base, base + " --silent"


def register(launch: dict, size_bytes: int = 0) -> dict:
    """登记/更新卸载信息。返回 {"ok":bool, "error":..., "key":...}。

    launch    启动目标（shortcut.launch_target() 的结果）
    size_bytes 安装体积（用来显示「大小」这一列；0＝不显示）
    """
    if not supported():
        return {"ok": False, "error": "只有 Windows 才有这套卸载列表", "key": ""}
    target = str(launch.get("target") or "")
    if not target:
        return {"ok": False, "error": "没有可执行的启动目标，没法登记卸载命令", "key": ""}

    winreg = _winreg()
    un_str, quiet_str = uninstall_commands(launch)
    exe_mode = not str(launch.get("args") or "").strip()    # 打包版：exe 自己就是程序
    install_dir = str(Path(target).resolve().parent) if exe_mode else ""

    values: List[Tuple[str, object]] = [
        ("DisplayName", f"{paths.APP_NAME} ｜ {paths.AUTHOR}"),
        ("DisplayVersion", __version__),
        ("Publisher", paths.AUTHOR),
        ("InstallLocation", install_dir),
        ("InstallDate", f"{date.today():%Y%m%d}"),
        ("UninstallString", un_str),
        ("QuietUninstallString", quiet_str),
        # 系统里不显示【修改】【修复】按钮（我们没做这两个功能）
        ("NoModify", 1),
        ("NoRepair", 1),
    ]
    if size_bytes > 0:
        values.append(("EstimatedSize", max(1, int(size_bytes // 1024))))   # 单位是 KB
    if exe_mode:
        values.append(("DisplayIcon", f"{target},0"))     # exe 自带图标
    else:
        ico = paths.icon_file()
        if ico.is_file():
            values.append(("DisplayIcon", f"{ico},0"))

    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, entry_path(),
                                 0, winreg.KEY_WRITE)
    except OSError as e:
        return {"ok": False, "error": f"写注册表失败：{e}", "key": ""}
    try:
        for name, value in values:
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)
    except OSError as e:
        return {"ok": False, "error": f"写注册表失败：{e}", "key": ""}
    finally:
        winreg.CloseKey(key)
    return {"ok": True, "error": "", "key": entry_path()}


def read_entry() -> Dict[str, object]:
    """读回登记的内容（没登记过就是空字典；测试和卸载程序都要用）。"""
    if not supported():
        return {}
    winreg = _winreg()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry_path(), 0,
                             winreg.KEY_READ)
    except OSError:
        return {}
    data: Dict[str, object] = {}
    try:
        count = winreg.QueryInfoKey(key)[1]
        for i in range(count):
            name, value, _kind = winreg.EnumValue(key, i)
            data[name] = value
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)
    return data


def is_registered() -> bool:
    """系统卸载列表里有没有这一条。"""
    return bool(read_entry())


def can_register() -> bool:
    """试写一次再删掉：这台机器（当前用户）到底能不能写卸载列表。

    受限账户、注册表被组策略锁住的机器写不了。写不了就别硬来——安装向导会
    退成免安装模式（程序留在原地，卸载时删文件夹），而不是给用户报一堆错。
    """
    if not supported():
        return False
    winreg = _winreg()
    test_path = f"{UNINSTALL_ROOT}\\{key_name()}-写入测试"
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, test_path, 0,
                                 winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, "Test", 0, winreg.REG_SZ, "1")
        finally:
            winreg.CloseKey(key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_path)
        return True
    except OSError:
        return False


def unregister() -> dict:
    """把卸载列表里的这一条删掉（卸载程序最后一步）。"""
    if not supported():
        return {"ok": False, "error": "只有 Windows 才有这套卸载列表"}
    winreg = _winreg()
    try:
        # 用 DeleteKey（不要用 DeleteKeyEx：那家伙的第 3 个参数只认 WOW64 标志，
        # 传 KEY_WRITE 会直接报 [WinError 87] 参数错误）
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, entry_path())
    except FileNotFoundError:
        return {"ok": True, "error": ""}          # 本来就没有，也算删干净了
    except OSError as e:
        return {"ok": False, "error": f"删注册表失败：{e}"}
    return {"ok": True, "error": ""}
