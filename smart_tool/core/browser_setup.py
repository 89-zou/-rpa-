# -*- coding: utf-8 -*-
"""Playwright 浏览器内核的检测与安装（打包后第一次运行要用）。

打包出来的程序里只有 playwright 的驱动（node.exe + cli.js），**没有**浏览器本体
（Chromium 约 150 MB）。内核放哪儿按这个顺序找：

1. 环境变量 `PLAYWRIGHT_BROWSERS_PATH`（设了就听它的）；
2. **程序目录旁的「浏览器」文件夹** —— 建了它就用它（放 D 盘/U 盘/项目里都行，
   绿色版、不想往 C 盘塞东西、或者怕被清理软件删掉，就建这个目录）；
3. 默认 `%LOCALAPPDATA%\\ms-playwright`。

`ensure_env()` 会把最终选中的目录写进 `PLAYWRIGHT_BROWSERS_PATH`，这样
playwright 自己去启动浏览器、以及 `install()` 下载，用的都是同一个地方。

· 安装向导 / `install()` 用它下载 Chromium（带日志）；
· 运行时用 `is_installed()` 先看一眼，没装就给出中文提示，别让用户看到
  Playwright 那句英文报错。
"""
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from smart_tool import paths

#: Playwright 自己的环境变量（设了就优先用它）
ENV_KEY = "PLAYWRIGHT_BROWSERS_PATH"
#: 程序目录旁边这个文件夹名（建了就用它装内核，不往 C 盘塞）
PORTABLE_DIR_NAME = "浏览器"


def portable_dir() -> Path:
    """程序目录旁的浏览器文件夹（绿色位置）。"""
    return paths.app_dir() / PORTABLE_DIR_NAME


def browsers_dir() -> Path:
    """浏览器装在哪（跟 Playwright 的规则保持一致，外加"程序旁的浏览器文件夹"）。"""
    custom = os.environ.get(ENV_KEY)
    if custom and custom != "0":
        return Path(custom).expanduser()
    portable = portable_dir()
    # 有内核 → 当然用它；只有空文件夹也算数（说明用户想让内核装在这儿）
    if portable.is_dir():
        return portable
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def ensure_env() -> Path:
    """把选中的内核目录告诉 playwright（启动浏览器和下载都走同一个地方）。

    必须在 `sync_playwright()` 启动浏览器之前调用，否则 playwright 会去默认位置找。
    """
    target = browsers_dir()
    os.environ[ENV_KEY] = str(target)
    return target


def installed_kinds() -> List[str]:
    """已经下载好的浏览器目录名（chromium-1243 这种）。"""
    d = browsers_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def is_installed() -> bool:
    """Chromium 装好了没（有 chromium / chromium_headless_shell 都算）。"""
    return any(name.startswith("chromium") for name in installed_kinds())


def driver_command() -> tuple:
    """拿到包内的 node.exe 和 cli.js。

    为什么不直接 `python -m playwright install`：打包后没有 Python 解释器，
    只有 playwright 包里的驱动，所以直接调它自带的 node + cli.js。
    """
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    return str(node), str(cli)


def install(on_log: Optional[Callable[[str], None]] = None,
            timeout_s: int = 3600) -> bool:
    """下载安装 Chromium（会实时把输出行回调给 on_log）。成功返回 True。"""
    node, cli = driver_command()
    if not Path(node).exists() or not Path(cli).exists():
        if on_log:
            on_log(f"驱动文件不在：{node}")
        return False
    if on_log:
        on_log(f"浏览器目录：{browsers_dir()}")
        on_log("开始下载 Chromium（约 150 MB，第一次会慢一点）…")
    ensure_env()                      # 让下载也落到上面那个目录
    try:
        proc = subprocess.Popen(
            [node, cli, "install", "chromium"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32" else 0,
        )
    except OSError as e:
        if on_log:
            on_log(f"启动下载失败：{e}")
        return False
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            text = line.strip()
            if text and on_log:
                on_log(text)
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        if on_log:
            on_log(f"下载超时（超过 {timeout_s // 60} 分钟），可以重试")
        return False
    ok = proc.returncode == 0 and is_installed()
    if on_log:
        on_log("Chromium 装好了 ✓" if ok else "没装成功，可以稍后在向导里重试")
    return ok


def hint() -> str:
    """没装浏览器时给用户看的提示（中文，能照着做）。"""
    from smart_tool import paths

    if paths.is_production():
        how = ("   最简单的办法：跑一次安装向导，它会在「环境构建」这一步自动下载：\n"
               "       小邹RPA.exe --setup\n")
    else:
        how = ("   源码运行时装一次就行（约 150 MB）：\n"
               "       .venv\\Scripts\\python -m playwright install chromium\n")
    return ("还没装浏览器内核（Chromium），跑流程时启动不了浏览器。\n" + how +
            f"   下载后会装在：{browsers_dir()}")
