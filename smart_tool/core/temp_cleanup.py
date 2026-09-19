# -*- coding: utf-8 -*-
"""清理单文件 exe 留下的临时残渣（`%TEMP%\\_MEIxxxxxx`）。

为什么会有：打包成单文件后，exe 每次启动都要先把自己解压到
`%TEMP%\\_MEIxxxxxx`（约 375 MB），正常退出时会自己删掉；但被强杀 / 崩溃 /
关机时就留在磁盘上了，越攒越多（实测见过 32 份、4.4 GB）。

这里只做一件事：启动时在后台把**够旧的**残渣删掉。三条安全线：
1. 只认 `%TEMP%` 下以 `_MEI` 开头、且**够旧**（默认 24 小时没动过）的目录；
2. 当前进程自己的解包目录（`sys._MEIPASS`）绝不碰；
3. 删之前先改名试探：改不动说明还有程序在用它（Windows 上目录里有打开的
   文件就改不了名），直接跳过；删的过程中删不掉的文件也一并忽略，不报错。
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

#: PyInstaller 解包目录的前缀
PREFIX = "_MEI"
#: 默认只清理「这么久没动过」的残渣
DEFAULT_MAX_AGE_HOURS = 24


def _own_dir() -> Optional[str]:
    """当前进程自己的解压目录（不能删）。源码运行时没有，返回 None。"""
    mei = str(getattr(sys, "_MEIPASS", "") or "")
    return mei or None


def candidates(max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> List[Path]:
    """列出可以清理的残渣目录（只看，不动手）。"""
    root = Path(tempfile.gettempdir())
    own = _own_dir()
    own_key = os.path.normcase(str(Path(own).resolve())) if own else ""
    deadline = time.time() - max_age_hours * 3600

    try:
        entries = list(root.iterdir())
    except OSError:
        return []

    out: List[Path] = []
    for p in entries:
        name = p.name
        if not name.startswith(PREFIX) or len(name) <= len(PREFIX):
            continue
        try:
            if not p.is_dir():                      # 同名文件不碰
                continue
            if own_key and os.path.normcase(str(p.resolve())) == own_key:
                continue                            # 自己的解包目录
            if p.stat().st_mtime > deadline:
                continue                            # 刚动过：可能还有程序在用
        except OSError:
            continue
        out.append(p)
    return out


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def clean(max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> Tuple[int, int]:
    """删掉够旧的残渣，返回 (删掉几个, 释放多少字节)。删不动的跳过，不抛异常。"""
    removed = freed = 0
    for p in candidates(max_age_hours):
        size = _size(p)
        # 先改名试探：还有程序在用的话会失败（目录里有打开的文件就改不了名）
        probe = p.with_name(p.name + ".cleanup")
        try:
            p.rename(probe)
        except OSError:
            continue
        try:
            shutil.rmtree(probe, ignore_errors=True)
        except Exception:
            continue
        if probe.exists():                          # 没删干净（还有文件被占）
            continue
        removed += 1
        freed += size
    return removed, freed


def clean_in_background(max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                        on_done: Optional[Callable[[int, int], None]] = None):
    """在后台线程里清一次（启动时调用，不耽误开界面）。"""
    import threading

    def job():
        try:
            removed, freed = clean(max_age_hours)
        except Exception:
            return
        if removed and on_done:
            try:
                on_done(removed, freed)
            except Exception:
                pass

    t = threading.Thread(target=job, name="clean-mei-leftovers", daemon=True)
    t.start()
    return t
