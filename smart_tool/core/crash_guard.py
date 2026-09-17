# -*- coding: utf-8 -*-
"""原生崩溃兜底：把「进程级崩溃」也记下来，附带出事前最后在做的事。

Python 的 `sys.excepthook` 只能抓 Python 异常；像访问冲突（0xc0000005）、
用户回调里逃出异常（0xc000041d）这类**原生崩溃**，进程是直接被系统干掉的，
`crash.log` 里一个字都不会有（事件日志里只能看到 qwindows.dll 这种没用的线索）。

这里用 `SetUnhandledExceptionFilter` 挂一个最后的机会：崩溃时把
异常代码、地址，以及 `note()` 记下的「最后一步在干什么」写进 crash.log。

注意：这个回调是在**已经崩了**的线程里跑的，所以：
- 只做最简单的事（拼一小段字节、WriteFile、CloseHandle），绝不做窗口/COM 操作；
- 面包屑在 note() 里就渲染成字节存好，回调里不再做字符串编码。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

# 异常代码 → 人话
CODE_CN = {
    0xC0000005: "访问冲突（读了不该读的内存）",
    0xC000041D: "用户回调里逃出了异常（窗口过程 / 钩子回调里抛异常）",
    0xC0000409: "栈缓冲越界 / 快速失败（fail-fast）",
    0xC00000FD: "栈溢出",
    0xC0000374: "堆损坏",
    0x80000003: "断言失败",
}

_filter = None            # 保住回调对象，别被回收
_log_path: Optional[Path] = None
_breadcrumb = bytearray()  # 预先渲染好的 UTF-8 字节，回调里直接用


def note(text: str):
    """记一句「现在在干什么」。崩了以后这行会出现在 crash.log 里。"""
    stamp = time.strftime("%H:%M:%S")
    _breadcrumb[:] = f"{stamp}  {text}".encode("utf-8", "replace")


def _write_bytes(data: bytes):
    """用 Win32 直接写文件（不依赖 Python 的文件对象，崩溃现场更稳）。"""
    k32 = ctypes.windll.kernel32
    GENERIC_WRITE, CREATE_ALWAYS, FILE_SHARE_READ = 0x40000000, 2, 1
    handle = k32.CreateFileW(str(_log_path), GENERIC_WRITE, FILE_SHARE_READ,
                             None, CREATE_ALWAYS, 0x80, None)
    if handle in (0, -1):
        return
    written = wintypes.DWORD(0)
    buf = ctypes.create_string_buffer(data, len(data))
    k32.WriteFile(handle, buf, len(data), ctypes.byref(written), None)
    k32.CloseHandle(handle)


class _EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [("ExceptionCode", wintypes.DWORD),
                ("ExceptionFlags", wintypes.DWORD),
                ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p),
                ("NumberParameters", wintypes.DWORD),
                ("ExceptionInformation", ctypes.c_void_p * 15)]


class _EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [("ExceptionRecord", ctypes.POINTER(_EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.c_void_p)]


def _handler(info_ptr):
    """最后的救命回调：异常代码 + 出事前最后一步，落盘。然后照常让进程退出。

    注意这里是「已经崩了」的现场：字符串拼装、内存分配都可能失败，
    所以任何一步失败都要退化成「至少把面包屑写下来」。
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            crumb = bytes(_breadcrumb).decode("utf-8", "replace")
        except Exception:
            crumb = "(读不到)"
        text = f"\n===== 原生崩溃 {stamp} =====\n最后在做: {crumb}\n"
        try:
            ptr = ctypes.cast(info_ptr, ctypes.POINTER(_EXCEPTION_POINTERS))
            rec = ptr.contents.ExceptionRecord
            if rec:
                code = rec.contents.ExceptionCode
                addr = rec.contents.ExceptionAddress or 0
                text += (f"异常代码: 0x{code:08X}  {CODE_CN.get(code, '')}\n"
                         f"异常地址: 0x{int(addr):X}\n")
        except Exception:
            text += "（读异常信息失败，只知道崩了）\n"
        _write_bytes(text.encode("utf-8", "replace"))
    except Exception:
        pass
    return 0            # EXCEPTION_CONTINUE_SEARCH：交给系统按原样处理


def install(log_path: Path) -> bool:
    """挂上兜底。装了返回 True（非 Windows 或装不上返回 False）。"""
    global _filter, _log_path
    if sys.platform != "win32":
        return False
    _log_path = Path(log_path)
    try:
        proto = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.POINTER(_EXCEPTION_POINTERS))
        _filter = proto(_handler)
        ctypes.windll.kernel32.SetUnhandledExceptionFilter(_filter)
        return True
    except Exception:
        _filter = None
        return False
