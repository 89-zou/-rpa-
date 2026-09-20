# -*- coding: utf-8 -*-
"""桌面自动化：全屏截图 + 模板匹配定位 + 系统级鼠标键盘。

场景＝桌面（`scene: "desktop"`）时用这一套，Playwright 完全不参与：
定位靠「截一张全屏图，用 OpenCV 找模板」，操作靠 pyautogui 发 OS 级输入。

坐标口径（本机实测一致，别改）：
进程是 DPI 感知的（Qt6 启动时设置），所以
- `ImageGrab.grab()` 截的是**物理像素**（1920×1080）；
- `pyautogui` 收的也是**物理像素**；
- 模板匹配出来的坐标可以 1:1 直接喂给 pyautogui。
（注意：网页那边 `window.devicePixelRatio` 会骗人，这里不会——两边别互相套用。）

没有 XPath，也没有 DOM：所以定位精度天然不如网页，模板要裁得干净
（只框控件本身，别带上大片背景）。
"""
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from smart_tool.core import image_locator
from smart_tool.core.crash_guard import note

# 找图时的默认等待：桌面程序渲染慢，一次没找到就多试几次
DEFAULT_WAIT_S = 10.0
POLL_S = 0.4
# 中文按键别名（写中文也认）
KEY_ALIASES = {
    "回车": "enter", "回车键": "enter", "确认": "enter", "确定": "enter",
    "制表": "tab", "空格": "space", "退格": "backspace", "删除": "delete",
    "取消": "esc", "退出": "esc", "上": "up", "下": "down",
    "左": "left", "右": "right",
}


class DesktopError(Exception):
    """桌面自动化出错（依赖没装、窗口没找到、图上没匹配到……）。"""


@dataclass
class DesktopMatch:
    """屏幕上匹配到的位置（物理像素）。"""
    x: float          # 中心 x
    y: float          # 中心 y
    width: float
    height: float
    confidence: float
    scale: float
    how: str = ""     # 怎么找到的：窗口内匹配 / 红框偏移 / 全屏匹配（写日志用）


#: 整窗模板的默认阈值：窗口是大图，内容一直在变，别拿控件的 0.8 去卡它
WINDOW_THRESHOLD = 0.75
#: 只有窗口匹配得**足够确定**，才允许「窗口内没找到控件 → 按红框偏移点」。
#: 窗口认错的话，偏移是相对错误原点算的，点了就是乱点 —— 宁可不点。
WINDOW_OFFSET_MIN_CONF = 0.85
#: 在窗口矩形外再放宽几个像素（窗口阴影、边框抖动）
WINDOW_MARGIN = 12


def available() -> bool:
    """桌面场景需要的库都装了吗（pyautogui + Pillow）。"""
    try:
        import pyautogui  # noqa: F401
        from PIL import ImageGrab  # noqa: F401
    except Exception:
        return False
    return True


def missing_hint() -> str:
    """缺依赖时给一句能照着做的提示。"""
    return ("桌面场景需要 pyautogui 和 Pillow，请在本项目的 .venv 里装：\n"
            "    .venv\\Scripts\\python -m pip install pyautogui Pillow")


def _gui():
    try:
        import pyautogui
    except Exception as e:
        raise DesktopError(f"没能加载 pyautogui：{e}\n{missing_hint()}") from e
    return pyautogui


def _ensure_dpi_aware():
    """尽最大努力让进程按物理像素报坐标（Qt6 一般已设，重复设置无害）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ------------------------------
# 截图
# ------------------------------
def screen_origin() -> Tuple[int, int]:
    """虚拟桌面左上角在 Windows 坐标里的位置（多屏时可能是负数）。

    截图拿到的是「整个虚拟桌面」的图，图内坐标要加上这个原点才是屏幕坐标。
    """
    if sys.platform != "win32":
        return 0, 0
    import ctypes
    u = ctypes.windll.user32
    return (int(u.GetSystemMetrics(76)),      # SM_XVIRTUALSCREEN
            int(u.GetSystemMetrics(77)))      # SM_YVIRTUALSCREEN


def grab_screen():
    """截全屏，返回 PIL Image（物理像素）。

    多屏时截**整个虚拟桌面**（配合 screen_origin 换算坐标），
    这样目标程序在副屏上也能找到。
    """
    note("桌面：截屏")
    _ensure_dpi_aware()
    try:
        from PIL import ImageGrab
    except Exception as e:
        raise DesktopError(f"没能加载 Pillow：{e}\n{missing_hint()}") from e
    try:
        return ImageGrab.grab(all_screens=True)
    except TypeError:               # 老版本 Pillow 没有这个参数
        return ImageGrab.grab()
    except Exception as e:
        raise DesktopError(f"截屏失败：{e}") from e


def save_screen(path: Path):
    """把当前屏幕存成图（给「截屏取模板」用）。"""
    img = grab_screen()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path))
    return img


# ------------------------------
# 找图
# ------------------------------
def locate(template_path: Path, threshold: Optional[float] = None,
           wait_s: float = DEFAULT_WAIT_S,
           log: Callable[[str], None] = print) -> DesktopMatch:
    """在屏幕上找这张模板；找不到就等着重试，超时抛 DesktopError。"""
    path = Path(template_path)
    if not path.exists():
        raise DesktopError(f"模板图不存在：{path}\n"
                           "（在步骤编辑器里点【截屏取模板…】重新截一张）")
    note(f"桌面：找图 {path.name}")
    template = image_locator.imread_unicode(path)
    th, tw = template.shape[:2]
    if th < 4 or tw < 4:
        raise DesktopError(f"模板太小（{tw}x{th}），请重新框选：{path.name}")

    limit = threshold if threshold is not None else image_locator.DEFAULT_THRESHOLD
    deadline = time.monotonic() + max(0.0, wait_s)
    tries = 0
    trace: List[str] = []
    while True:
        tries += 1
        screen = _grab_bgr()
        trace = []
        found = image_locator.best_match(screen, template, threshold=limit,
                                         key=str(path), trace=trace)
        if found is not None:
            ox, oy = screen_origin()        # 图内坐标 → 屏幕坐标
            x, y = ox + found.x, oy + found.y
            log(f"  截图匹配成功：屏幕({x:.0f},{y:.0f}) "
                f"{image_locator.describe(found)}")
            return DesktopMatch(x, y, found.width, found.height,
                                found.confidence, found.scale)
        if time.monotonic() >= deadline:
            raise DesktopError(
                f"屏幕上没找到这张图（{path.name}，试了 {tries} 次，"
                f"阈值 {limit}）。\n"
                "    可能原因：目标窗口不在最前面、图被裁得不干净、"
                "程序还没画出来，或这张模板是别的分辨率下截的。"
                + "".join(f"\n    · {t}" for t in dict.fromkeys(trace[-2:]))
            )
        time.sleep(POLL_S)


def _grab_bgr():
    """截全屏并转成 OpenCV 的 BGR 数组。"""
    import numpy as np
    img = grab_screen().convert("RGB")
    return np.array(img)[:, :, ::-1].copy()


# ------------------------------
# 窗口：先找到窗口，再在窗口里找控件
# ------------------------------
def window_rect_at(x: float, y: float) -> Optional[Tuple[int, int, int, int]]:
    """(x, y) 所在**顶层窗口**的屏幕矩形；问不到就返回 None。

    先用 UI Automation（准、还带窗口标题），失败退回 Win32 的
    WindowFromPoint + GetAncestor(GA_ROOT)。自绘界面、权限不足时可能都拿不到。
    """
    try:
        from smart_tool.core import desktop_uia
        ctrl = desktop_uia.window_at(x, y)
        if ctrl is not None:
            return ctrl.rect
    except Exception:
        pass
    return _win32_window_rect(x, y)


def _win32_window_rect(x: float, y: float) -> Optional[Tuple[int, int, int, int]]:
    """Win32 兜底：点 → 窗口句柄 → 根窗口矩形（含标题栏和边框）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None
        root = u.GetAncestor(hwnd, 2)        # GA_ROOT = 2
        rect = wintypes.RECT()
        if not u.GetWindowRect(root, ctypes.byref(rect)):
            return None
        box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        return box
    except Exception:
        return None


def find_window(window_template: Path, threshold: Optional[float] = None,
                wait_s: float = DEFAULT_WAIT_S,
                log: Callable[[str], None] = print) -> Optional[DesktopMatch]:
    """用「整窗截图」在屏幕上找窗口 → 它现在的位置和大小；没找到返回 None。

    窗口模板的阈值默认比控件低（WINDOW_THRESHOLD）：整窗图里内容一直在变
    （标题、列表、输入框），拿控件的 0.8 去卡它基本卡不住。
    """
    try:
        return locate(window_template, threshold=threshold or WINDOW_THRESHOLD,
                      wait_s=wait_s, log=log)
    except DesktopError as e:
        log("  没找到窗口：" + str(e).splitlines()[0])
        return None


def _window_box_in_image(win: DesktopMatch,
                         shape: Tuple[int, ...]) -> Optional[Tuple[int, int, int, int]]:
    """窗口命中 → 截图图像里的裁剪范围（外扩 WINDOW_MARGIN，并夹在图内）。"""
    ox, oy = screen_origin()
    ih, iw = shape[:2]
    left = int(round(win.x - win.width / 2 - ox)) - WINDOW_MARGIN
    top = int(round(win.y - win.height / 2 - oy)) - WINDOW_MARGIN
    right = int(round(win.x + win.width / 2 - ox)) + WINDOW_MARGIN
    bottom = int(round(win.y + win.height / 2 - oy)) + WINDOW_MARGIN
    left, top = max(0, left), max(0, top)
    right, bottom = min(iw, right), min(ih, bottom)
    if right - left < 4 or bottom - top < 4:
        return None
    return left, top, right, bottom


def locate_in_window(win: DesktopMatch, template_path: Path,
                     threshold: Optional[float] = None, wait_s: float = 6.0,
                     log: Callable[[str], None] = print) -> Optional[DesktopMatch]:
    """只在**窗口矩形内**找控件（不再全屏找）—— 范围小了，假匹配少、也快。"""
    path = Path(template_path)
    if not path.is_file():
        raise DesktopError(f"模板图不存在：{path}")
    template = image_locator.imread_unicode(path)
    limit = threshold if threshold is not None else image_locator.DEFAULT_THRESHOLD
    key = str(path)
    deadline = time.monotonic() + max(0.0, wait_s)
    trace: List[str] = []
    while True:
        screen = _grab_bgr()
        box = _window_box_in_image(win, screen.shape)
        if box is not None:
            l, t, r, b = box
            sub = screen[t:b, l:r]
            trace = []
            hit = image_locator.best_match(sub, template, threshold=limit,
                                           key=key, trace=trace)
            if hit is not None:
                ox, oy = screen_origin()
                x, y = ox + l + hit.x, oy + t + hit.y
                log(f"  窗口内匹配到控件：屏幕({x:.0f},{y:.0f}) "
                    f"{image_locator.describe(hit)}")
                return DesktopMatch(x, y, hit.width, hit.height,
                                    hit.confidence, hit.scale, how="窗口内匹配")
        if time.monotonic() >= deadline:
            log("  窗口内没匹配到控件"
                + (f"：{trace[-1]}" if trace else ""))
            return None
        time.sleep(POLL_S)


def locate_by_window(window_template, target_template: Path, offset=(),
                     threshold: Optional[float] = None,
                     wait_s: float = DEFAULT_WAIT_S,
                     log: Callable[[str], None] = print) -> DesktopMatch:
    """桌面定位主流程：先找到窗口 → 在窗口里找控件 → 都没有再退。

    为什么这样找：整屏匹配等于「在两百万个位置里挑一个最像的」，浅色界面很容易挑错；
    先认出窗口，范围就只剩这个窗口（面积常常小一个数量级），再全屏兜底。

    三条路，从上往下：
    1. 窗口内匹配到控件 → 点命中点（最准，抗窗口内布局微调）
    2. 窗口找到了、控件没匹配上 → 按捕获时记下的**红框偏移**点（你说的「点红框中心」）
    3. 窗口都没找到 → 退回全屏匹配（没有窗口模板的老项目走这条，行为不变）
    """
    limit = threshold if threshold is not None else image_locator.DEFAULT_THRESHOLD
    if window_template and Path(window_template).is_file():
        log(f"  先找窗口：{Path(window_template).name}")
        win = find_window(window_template, wait_s=min(wait_s, 8.0), log=log)
        if win is not None:
            log(f"  窗口在屏幕({win.x - win.width / 2:.0f},{win.y - win.height / 2:.0f}) "
                f"大小 {win.width:.0f}×{win.height:.0f}")
            hit = locate_in_window(win, target_template, threshold=limit,
                                   wait_s=min(wait_s, 6.0), log=log)
            if hit is not None:
                return hit
            if offset and len(offset) >= 4:
                if win.confidence < WINDOW_OFFSET_MIN_CONF:
                    log(f"  窗口匹配得不够确定（置信度 {win.confidence:.3f} < "
                        f"{WINDOW_OFFSET_MIN_CONF}），不敢按红框偏移点 → 退回全屏匹配")
                else:
                    dx, dy, dw, dh = (float(v) for v in offset[:4])
                    s = float(win.scale or 1.0)
                    x = win.x - win.width / 2 + (dx + dw / 2) * s
                    y = win.y - win.height / 2 + (dy + dh / 2) * s
                    log(f"  按红框偏移点：屏幕({x:.0f},{y:.0f})（窗口缩放 {s:g}）")
                    return DesktopMatch(x, y, dw * s, dh * s, win.confidence, s,
                                        how="红框偏移")
            log("  窗口里没匹配到控件，也没记红框偏移 → 退回全屏匹配")
    elif window_template:
        log(f"  窗口模板不存在，退回全屏匹配：{window_template}")
    hit = locate(target_template, threshold=limit, wait_s=wait_s, log=log)
    hit.how = "全屏匹配"
    return hit


def wait_gone(template_path: Path, wait_s: float = DEFAULT_WAIT_S,
              threshold: Optional[float] = None,
              log: Callable[[str], None] = print):
    """等这张图从屏幕上消失（转圈、加载提示这类）。

    已经不在屏幕上就直接返回；一直不走就等到超时（超时不算失败，记一条日志）。
    """
    path = Path(template_path)
    if not path.exists():
        raise DesktopError(f"模板图不存在：{path}")
    template = image_locator.imread_unicode(path)
    limit = threshold if threshold is not None else image_locator.DEFAULT_THRESHOLD
    deadline = time.monotonic() + max(0.0, wait_s)
    while image_locator.best_match(_grab_bgr(), template, threshold=limit):
        if time.monotonic() >= deadline:
            log(f"  等图片消失超时（{wait_s:g}s）：{path.name}，继续往下走")
            return
        time.sleep(POLL_S)
    log(f"  {path.name} 已消失")


# ------------------------------
# 鼠标 / 键盘
# ------------------------------
def exists(template_path, threshold: Optional[float] = None) -> bool:
    """屏幕上现在有没有这张图（即时看一眼，不等、不抛异常）。

    给「循环」的条件判断用：监控某个图标 / 提示是不是出现了。
    想让它出现就等一会儿的（比如等按钮变亮）用 `locate()`。
    """
    try:
        path = Path(template_path)
        if not path.is_file():
            return False
        template = image_locator.imread_unicode(path)
        if template is None or min(template.shape[:2]) < 4:
            return False
        limit = (threshold if threshold is not None
                 else image_locator.DEFAULT_THRESHOLD)
        return image_locator.best_match(_grab_bgr(), template,
                                        threshold=limit) is not None
    except Exception:
        return False


def move(x: float, y: float):
    _gui().moveTo(int(round(x)), int(round(y)))


def click(x: float, y: float, times: int = 1):
    """在屏幕坐标点一下（times=2 就是双击）。"""
    note(f"桌面：点击 ({x:.0f},{y:.0f}) x{times}")
    gui = _gui()
    _ensure_dpi_aware()
    gui.moveTo(int(round(x)), int(round(y)))
    if int(times) >= 2:
        gui.doubleClick()
    else:
        gui.click()


def clear_field(log: Callable[[str], None] = print):
    """把当前焦点输入框里的内容全选删掉。

    输入文字前先清空，行为跟网页端的 fill 一致（网页那边是直接覆盖值）。
    """
    gui = _gui()
    gui.hotkey("ctrl", "a")
    time.sleep(0.06)
    gui.press("delete")
    time.sleep(0.06)


def type_text(text: str, log: Callable[[str], None] = print):
    """输入文字（中文也行）。

    走 Windows 的 SendInput + KEYEVENTF_UNICODE，**一个字符一个字符直接敲进去**：
    这样不占剪贴板、不受输入法/键盘布局影响，也没有「粘贴时剪贴板被别的程序换掉」
    的竞态（实测用「复制到剪贴板 + Ctrl+V」会粘进过期的内容）。
    """
    if not text:
        return
    note(f"桌面：输入文字（{len(text)} 字）")
    if sys.platform != "win32":
        _gui().write(text, interval=0.02)
        return
    _ensure_dpi_aware()
    _send_unicode(text)


def _send_unicode(text: str):
    """用 SendInput 逐字发 Unicode 按键（中文等非 ASCII 也能打）。

    注意 INPUT 结构体在 64 位下必须是 40 字节（type+padding 8 + 联合体 32），
    不然 SendInput 会以「cbSize 不对」为由直接返回 0、什么都不发。
    """
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    # SendInput 按 UTF-16 码元发：先把文本转成 UTF-16LE，再按 2 字节切
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little")
             for i in range(0, len(raw), 2)]

    user32 = ctypes.windll.user32
    size = ctypes.sizeof(INPUT)

    def send(flags: int, scan: int):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags,
                              time=0, dwExtraInfo=None)
        if user32.SendInput(1, ctypes.byref(inp), size) != 1:
            raise DesktopError(
                "发按键失败（SendInput 没接受）。可能是权限或输入被拦截。"
            )

    for unit in units:
        send(KEYEVENTF_UNICODE, unit)                       # 按下
        send(KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, unit)     # 抬起
        time.sleep(0.01)


def hotkey(keys: str):
    """按一个键或一组快捷键：enter / ctrl+s / alt+f4（也认「回车」这类中文）。"""
    raw = (keys or "").replace("，", ",").replace("＋", "+").replace(" ", "")
    parts = [p for p in re.split(r"[+,]", raw) if p]
    if not parts:
        raise DesktopError("「按键」没填要按什么（例：enter、ctrl+s、alt+f4）")
    note(f"桌面：按键 {keys}")
    mapped = [KEY_ALIASES.get(p, p.lower()) for p in parts]
    _gui().hotkey(*mapped)


# ------------------------------
# 窗口
# ------------------------------
def list_windows() -> List[Tuple[str, bool]]:
    """当前所有可见窗口：[(标题, 是否在前台)]。"""
    try:
        import pygetwindow as gw
    except Exception as e:
        raise DesktopError(f"没能加载 pygetwindow（pyautogui 自带的）：{e}\n"
                           f"{missing_hint()}") from e
    out: List[Tuple[str, bool]] = []
    for w in gw.getWindowsWithTitle(""):
        title = (w.title or "").strip()
        if not title:
            continue
        try:
            active = bool(w.isActive)
        except Exception:
            active = False
        out.append((title, active))
    return out


def activate_window(keyword: str,
                    log: Callable[[str], None] = print) -> str:
    """把标题里含 keyword 的窗口切到前台，返回它真实的标题。

    桌面自动化几乎都要先做这一步：点之前得让目标窗口在最前面。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        raise DesktopError("「激活窗口」没填窗口标题关键字（填标题里的一小段即可）")
    note(f"桌面：激活窗口（{keyword}）")
    try:
        import pygetwindow as gw
    except Exception as e:
        raise DesktopError(f"没能加载 pygetwindow：{e}\n{missing_hint()}") from e

    hits = [w for w in gw.getWindowsWithTitle("")
            if kw in (w.title or "").lower()]
    if not hits:
        titles = [t for t, _ in list_windows()][:10]
        raise DesktopError(
            f"没找到标题里含「{keyword}」的窗口。\n"
            "    当前可见窗口：" + ("、".join(titles) if titles else "（一个都没有）")
        )
    win = hits[0]
    title = (win.title or "").strip()
    try:
        if win.isMinimized:
            win.restore()
        win.activate()
    except Exception as e:
        # pygetwindow 的 activate 偶尔会抛，但窗口常常已经切过来了，看结果说话
        time.sleep(0.2)
        if not getattr(win, "isActive", False):
            raise DesktopError(
                f"把窗口「{title}」切到前台失败：{e}\n"
                "    可以先手动点一下那个窗口，再运行流程。"
            ) from e
    time.sleep(0.2)
    log(f"  已切到窗口：{title}")
    return title
