# -*- coding: utf-8 -*-
"""创建 Windows 快捷方式（桌面 / 开始菜单）。

走系统的 PowerShell + WScript.Shell：不用额外装 pywin32，也不碰注册表。
图标用 `paths.icon_file()`（从 assets/logo.png 生成的 .ico）。
"""
import subprocess
import sys
from pathlib import Path
from typing import Optional

from smart_tool import paths


def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串：里面的单引号要写两遍。"""
    return "'" + str(text or "").replace("'", "''") + "'"


def _run_ps(script: str) -> tuple:
    """跑一段 PowerShell，返回 (成功?, 输出)。

    两个讲究：
    · 先让 PowerShell 用 UTF-8 输出（中文系统默认按 GBK 输出，Python 按 UTF-8 解
      会在读取线程里直接抛 UnicodeDecodeError，调用方那边就卡住了）；
    · 再加 errors="replace" 兜底：万一还是有不认识的字节，替换掉就行，别让整个
      建快捷方式的流程崩掉。
    """
    if sys.platform != "win32":
        return False, "只有 Windows 支持创建快捷方式"
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
              "$OutputEncoding = [Text.Encoding]::UTF8\n") + script
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用 PowerShell 失败：{e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    # PowerShell 里普通报错不会让返回码变成非 0（比如 $s.Save() 抛 COMException 时
    # 返回码照样是 0），所以 stderr 有东西就当失败把原文带回去，别报"没报错"
    err = (proc.stderr or "").strip()
    if err:
        return False, err[:300]
    return True, (proc.stdout or "").strip()


def desktop_dir() -> Path:
    """桌面目录（兼容 OneDrive 把桌面挪走的情况）。"""
    ok, out = _run_ps('[Environment]::GetFolderPath("Desktop")')
    if ok and out:
        return Path(out)
    return Path.home() / "Desktop"


def start_menu_dir() -> Path:
    """开始菜单里的程序目录。"""
    ok, out = _run_ps(
        '[Environment]::GetFolderPath("Programs")')
    if ok and out:
        return Path(out)
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" \
        / "Start Menu" / "Programs"


def create_shortcut(target: str, args: str = "", name: str = paths.APP_NAME,
                    icon: str = "", workdir: str = "",
                    where: str = "desktop",
                    folder_path: Optional[str] = None,
                    description: str = "") -> dict:
    """建一个快捷方式，返回 {"ok":bool, "path":..., "error":...}。

    target      要启动的东西（打包后＝exe；源码运行＝pythonw.exe）
    args        启动参数（源码运行时是 "-m smart_tool.main"）
    where       "desktop" 桌面 / "startmenu" 开始菜单 / "custom"（配 folder_path）
    folder_path 指定放到哪个目录（测试和自定义用；where="custom" 时必填）
    """
    if folder_path:
        folder = Path(folder_path)
    elif where == "startmenu":
        folder = start_menu_dir()
    else:
        folder = desktop_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"创建目录失败：{e}", "path": ""}

    lnk = folder / f"{name}.lnk"
    icon_path = icon or (str(paths.icon_file())
                         if paths.icon_file().is_file() else "")
    script = (
        "$W = New-Object -ComObject WScript.Shell\n"
        f"$s = $W.CreateShortcut({_ps_quote(str(lnk))})\n"
        f"$s.TargetPath = {_ps_quote(target)}\n"
        f"$s.Arguments = {_ps_quote(args)}\n"
        f"$s.WorkingDirectory = {_ps_quote(workdir or str(folder))}\n"
        f"$s.Description = {_ps_quote(description or f'{paths.APP_NAME}')}\n"
        + (f"$s.IconLocation = {_ps_quote(icon_path)}\n" if icon_path else "")
        + "$s.Save()\n"
        "Write-Output 'OK'\n"
    )
    ok, out = _run_ps(script)
    if not ok:
        return {"ok": False, "error": out, "path": str(lnk)}
    if not lnk.exists():
        return {"ok": False, "error": "快捷方式没写出来（PowerShell 没报错）",
                "path": str(lnk)}
    return {"ok": True, "error": "", "path": str(lnk)}


def launch_target() -> dict:
    """当前程序该怎么被启动（快捷方式的目标与参数）。

    · 打包后：目标＝exe，参数空；
    · 源码运行：目标＝同目录的 pythonw.exe（不弹黑窗），参数＝-m smart_tool.main。
    """
    if getattr(sys, "frozen", False):
        return {"target": str(Path(sys.executable).resolve()), "args": "",
                "workdir": str(Path(sys.executable).resolve().parent)}
    # 源码运行：优先用 venv 里的 pythonw.exe（不弹黑窗）
    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    target = str(pythonw if pythonw.is_file() else sys.executable)
    # 工作目录必须是「仓库根」：pythonw -m smart_tool.main 靠 sys.path[0]（＝当前目录）
    # 找到 smart_tool 包，目录不对会直接 ImportError
    return {"target": target, "args": "-m smart_tool.main",
            "workdir": str(paths.BUNDLE_DIR)}
