# -*- coding: utf-8 -*-
"""启动「用户自己装的浏览器」（Edge / Chrome），并等它的调试端口就绪。

为什么是这个路子
----------------
Page Agent 是跑在网页里的 JS，要有人把它注入进去、还要能收发消息 —— 所以浏览器
必须开一个**调试端口**（CDP）。而新版 Chrome/Edge 出于安全**不允许**用「你日常在
用的那个配置目录」开调试端口（会直接忽略这个参数），所以我们给程序自己一个
**独立配置目录**（数据目录下的「浏览器配置/」）：登录一次之后 cookie 就留在那儿，
下次接着用，也不会打扰你日常的浏览器。

两条使用路径：
1. 独立配置目录（默认）：本模块启动一个带调试端口的窗口 → 由执行器 connect_over_cdp 连上；
2. 端口上已经有实例（比如上次勾了「跑完不关」）：本模块直接返回"已有"，不重复启动、
   结束时也不去关它。

浏览器路径怎么定：设置里指定的 exe → 指定的桌面快捷方式 .lnk（简易解析目标）→
自动探测常见安装位置。
"""
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Dict, Optional, Tuple

from rpa_page_agent import paths

#: 自动探测的常见安装位置（先 Chrome 再 Edge；本机没装的会被跳过）
CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)

#: 配置目录名（放在数据目录的「浏览器/」下面 —— 那个目录本来就不进 git，
#: 而浏览器配置里有 cookie/缓存，绝不能被提交）
PROFILE_DIR_NAME = "用户配置"
#: 等调试端口就绪最多几秒
CDP_WAIT_S = 30.0


class BrowserError(RuntimeError):
    """启动/连接本机浏览器时的错误（没找到浏览器、端口起不来……）。"""


def profile_dir() -> Path:
    """程序专用的浏览器配置目录：数据目录的「浏览器/用户配置/」。

    放这儿而不是数据目录根下：`.gitignore` 已经忽略了「浏览器/」，
    而这里面会有 cookie、缓存（跟 Playwright 的内核目录做邻居，反正都是浏览器的东西）。
    """
    return Path(paths.DATA_DIR) / "浏览器" / PROFILE_DIR_NAME


# ---------------------------------------------------------------- 找浏览器
def parse_lnk(lnk_path) -> str:
    """从桌面快捷方式里粗略抠出目标 exe 路径（.lnk 是二进制，没装 pywin32 就只能这样）。

    做法：把整个文件按 UTF-16LE 解出来，找出所有以 .exe 结尾的片段，取最长的那个
    （.lnk 里目标路径就存在这种片段里）。抠不出来返回空串，由调用方提示改选 exe。
    """
    try:
        raw = Path(lnk_path).read_bytes()
    except Exception:
        return ""
    try:
        text = raw.decode("utf-16-le", "ignore")
    except Exception:
        return ""
    hits = re.findall(r"[A-Za-z]:\\[^\x00<>|?*\"]+?\.exe", text, re.I)
    if not hits:
        return ""
    # 优先带 chrome/edge 字样的，其次取最长的
    hits.sort(key=lambda p: (("chrome" in p.lower() or "edge" in p.lower()), len(p)),
              reverse=True)
    return hits[0]


def find_browser(hint: str = "") -> str:
    """定下来用哪个浏览器 exe：设置里给的（exe 或 .lnk）→ 自动探测。"""
    hint = str(hint or "").strip().strip('"')
    if hint:
        p = Path(os.path.expandvars(hint))
        if p.suffix.lower() == ".lnk":
            target = parse_lnk(p)
            if target and Path(target).is_file():
                return target
            raise BrowserError(
                f"没能从这个快捷方式里读出浏览器路径：{p}\n"
                "    请改选浏览器本体的 exe（一般在 C:\\Program Files\\…\\chrome.exe）。")
        if p.is_file():
            return str(p)
        raise BrowserError(f"设置里的浏览器路径不存在：{p}")
    for cand in CANDIDATES:
        p = Path(os.path.expandvars(cand))
        if p.is_file():
            return str(p)
    raise BrowserError(
        "没找到本机装的 Chrome / Edge。\n"
        "    可以在「打开网页」节点里点【浏览器设置…】手动指定浏览器 exe，\n"
        "    或改回用程序自带的内核（默认）。")


def browser_name(exe: str) -> str:
    """人话名字（写日志用）。"""
    low = str(exe).lower()
    if "edge" in low:
        return "Edge"
    if "chrome" in low:
        return "Chrome"
    return Path(exe).stem


# ---------------------------------------------------------------- 启 / 停
def cdp_version(port: int, timeout_s: float = 2.0) -> Optional[Dict]:
    """调试端口上有实例就返回它的 /json/version（浏览器名、版本），没有返回 None。"""
    try:
        url = f"http://127.0.0.1:{int(port)}/json/version"
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            import json
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def launch(exe: str, port: int, url: str = "", headless: bool = False,
           log=print) -> subprocess.Popen:
    """启动浏览器（带调试端口 + 独立配置目录），返回进程句柄。

    url 为空＝先开一个空白窗（留给「用键盘敲网址」那条路）。
    """
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url or "about:blank")
    log(f"  启动 {browser_name(exe)}（调试端口 {port}，配置目录 {profile}）"
        + ("，无头模式" if headless else ""))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        raise BrowserError(f"启动浏览器失败（{exe}）：{e}") from e
    return proc


def wait_cdp(port: int, timeout_s: float = CDP_WAIT_S, log=print) -> Dict:
    """等调试端口就绪，返回 /json/version 的内容；超时抛 BrowserError。"""
    deadline = time.monotonic() + max(3.0, timeout_s)
    while time.monotonic() < deadline:
        info = cdp_version(port)
        if info:
            log(f"  调试端口已就绪：{info.get('Browser') or '（未知版本）'}")
            return info
        time.sleep(0.4)
    raise BrowserError(
        f"等了 {int(timeout_s)} 秒，调试端口 {port} 还没起来。\n"
        "    常见原因：端口被别的程序占着；或浏览器启动后被安全软件拦了。")


def stop(proc: Optional[subprocess.Popen], log=print):
    """收掉**我们自己启动**的浏览器进程（连接断开不会杀它，得自己来）。"""
    if proc is None or proc.poll() is not None:
        return
    log("  关闭浏览器窗口（这是本程序启动的那个）")
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def start_or_attach(exe_hint: str, port: int, url: str = "", headless: bool = False,
                    log=print) -> Tuple[Optional[subprocess.Popen], Dict, bool]:
    """启动浏览器（或复用端口上已有的实例）。

    返回 (进程句柄或 None, /json/version, 是不是我们刚启的)。
    端口上已经有实例时进程句柄是 None —— 那种情况**不要去关它**。
    """
    info = cdp_version(port)
    if info:
        log(f"  端口 {port} 上已经有浏览器（{info.get('Browser') or '未知'}），直接连它")
        return None, info, False
    exe = find_browser(exe_hint)
    proc = launch(exe, port, url=url, headless=headless, log=log)
    try:
        info = wait_cdp(port, log=log)
    except BrowserError:
        stop(proc, log=log)
        raise
    return proc, info, True


# ---------------------------------------------------------------- 用键盘敲网址
#: 敲完回车等多久算「这次没生效」（没生效就补一次回车）
ENTER_WAIT_S = 3.0


def _href(page) -> str:
    try:
        return str(page.evaluate("() => location.href") or "")
    except Exception:
        return ""


def _is_real_page(href: str) -> bool:
    """这个地址算不算「打开了一个真页面」（排除空白页和浏览器错误页）。"""
    low = (href or "").strip().lower()
    if not low or low in ("about:blank", "about:newtab"):
        return False
    return not (low.startswith("chrome-error")
                or low.startswith("edge-error")
                or low.startswith("about:"))


def _arrived(page, url: str, before: str) -> str:
    """页面跳到目标地址了吗？到了返回当前地址，没到返回空串。

    两种算「到了」：地址变了（站点自己 301 / 补尾斜杠也算），或者本来就在目标站点上。
    —— 光判「不是空白页」不行：浏览器配置目录里可能自己带着一个「新标签页」，
    那样第一轮就会误报「已经打开」，其实网址还没敲进去。
    """
    href = _href(page)
    if not _is_real_page(href):
        return ""
    if href != before:
        return href
    host = urlsplit(url).netloc.lower()
    return href if host and host in href.lower() else ""


def _page_focused(page) -> bool:
    """页面（＝那个浏览器窗口）现在是不是真的在前台？

    `document.hasFocus()` 是网页能问到的「我是不是当前活动窗口」——用它来判断
    「键盘敲下去会敲进浏览器」还是「会敲到别的程序里去」。
    """
    try:
        return bool(page.evaluate("() => document.hasFocus()"))
    except Exception:
        return False


def type_url(page, url: str, timeout_s: float = 20.0, log=print):
    """像真人一样：Ctrl+L 聚焦地址栏 → 逐字敲网址 → 回车，并确认页面真的跳过去了。

    两个坑都在这儿堵住：

    · **窗口不在前台**：键盘敲下去会敲进别的程序（用户正在用的编辑器、聊天框……），
      浏览器这边当然「没反应」。所以敲之前先确认 `document.hasFocus()`，
      不在前台就别敲了，直接导航 —— 免得把网址喷到别人窗口里。
    · **中文输入法吃掉第一个回车**：它只是把候选「上屏」，不算提交地址。
      所以敲完等 3 秒没跳就再敲一次，最多三次；再不行就直接导航兜底。
    """
    try:
        import pyautogui
    except Exception as e:
        raise BrowserError(f"「用键盘敲网址」需要 pyautogui：{e}") from e

    deadline = time.monotonic() + max(5.0, timeout_s)

    def direct(reason: str) -> str:
        log(f"  {reason}，改用直接导航（更稳）")
        left = max(3.0, deadline - time.monotonic())
        page.goto(str(url), wait_until="domcontentloaded", timeout=left * 1000)
        got = _href(page)
        if got:
            log(f"  页面已经打开：{got[:80]}")
        return got

    try:
        page.bring_to_front()             # 标签页切到前台（窗口的激活还得看系统）
    except Exception:
        pass
    time.sleep(0.6)                       # 等窗口真的到前台
    for _ in range(5):                    # 系统切窗口偶尔慢一拍，再等等看
        if _page_focused(page):
            break
        time.sleep(0.3)
    if not _page_focused(page):
        return direct("浏览器窗口没在前台（键盘会敲到别的程序里）")

    before = _href(page)
    log(f"  用键盘输入网址：{url}")

    pyautogui.hotkey("ctrl", "l")         # 聚焦地址栏并全选
    time.sleep(0.25)
    pyautogui.typewrite(str(url), interval=0.03)
    time.sleep(0.15)
    pyautogui.press("enter")

    for attempt in range(3):
        wait = min(ENTER_WAIT_S, max(0.0, deadline - time.monotonic()))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            got = _arrived(page, url, before)
            if got:
                log(f"  页面已经打开：{got[:80]}")
                return got
            time.sleep(0.3)
        if attempt < 2 and time.monotonic() < deadline:
            log("  回车没生效，再敲一次（中文输入法有时会吃掉第一次回车）")
            pyautogui.press("enter")

    # 键盘这条路一直不通 → 别再较劲（结果是用户要的，只是少了「敲」的过程）
    return direct("键盘敲的网址一直没生效")