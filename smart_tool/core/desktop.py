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
import ctypes
import math
import random
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

#: 鼠标行为（项目级设置，运行开始时由执行器调 configure_mouse 设一次）
#: human＝拟人化移动（分步 + 缓入缓出 + 轻微抖动）；speed＝移过去大概用几秒
_MOUSE = {"human": False, "speed": 0.3}
#: 拟人化移动的速度预设（秒）——界面上是「快 / 中 / 慢」三个选项
MOUSE_SPEED_PRESETS = (("fast", "快（0.15 秒）", 0.15),
                       ("mid", "中（0.3 秒）", 0.3),
                       ("slow", "慢（0.6 秒）", 0.6))
DEFAULT_MOUSE_SPEED = 0.3

#: 拟人化移动的帧间隔（秒）。位置更新的节奏要跟得上系统消化鼠标消息的速度，
#: 这里约 120Hz——每帧只挪一点点，看上去才是「滑过去」而不是「跳过去」。
MOVE_FRAME_S = 0.008
#: 一帧最多跨多少像素。距离远的时候靠它补帧，免得帧数被时间卡死、一步跨一大截。
MOVE_MAX_PX = 22.0
#: 轨迹的弧度和手抖幅度（像素）。弧度＝手腕甩出去的那种感觉；
#: 手抖用低频正弦，不用每帧塞随机数（那看着是「毛刺」，不像手）。
MOVE_BEND_PX = 18.0
MOVE_TREMOR_PX = 1.2
#: 帧数上下限：太少能看出台阶，太多白耗时间
MOVE_MIN_FRAMES = 12
MOVE_MAX_FRAMES = 240
#: 急停区域：光标进到这个角落里就中断（跟 pyautogui.FAILSAFE 一个意思）
FAILSAFE_BOX = 2
#: 按下 / 松开左键要发的系统事件（pyautogui 在 Windows 上发的也是这两个）
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class _POINT(ctypes.Structure):
    """GetCursorPos 用的结构体。"""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _user32():
    if sys.platform != "win32":
        raise DesktopError("桌面自动化目前只支持 Windows。")
    return ctypes.windll.user32


def _cursor_pos() -> Tuple[float, float]:
    """光标现在在哪（物理像素）。"""
    pt = _POINT()
    _user32().GetCursorPos(ctypes.byref(pt))
    return float(pt.x), float(pt.y)


def _set_cursor(x: float, y: float) -> None:
    """把光标直接放到 (x, y)（物理像素），顺手做一次左上角急停检查。

    为什么不直接调 pyautogui.moveTo：它每次调用完会自己 sleep 一个
    `pyautogui.PAUSE`（默认 0.1 秒）。拟人化轨迹要连着发几十帧，于是变成
    「跳一格 → 冻 0.1 秒 → 再跳一格」，这就是一卡一卡的根因
    （实测：15 步的移动，光 PAUSE 就多花 1.5 秒）。这里直接调系统 API
    （pyautogui 内部调的也是它），把节奏完全握在自己手里。

    自己发事件就绕开了 pyautogui 的 FAILSAFE，所以在这里补回来——
    跑的时候把鼠标猛地甩到屏幕左上角＝急停，这个救命功能不能丢。
    """
    try:
        import pyautogui
        failsafe = bool(pyautogui.FAILSAFE)
    except Exception:
        failsafe = False
    if failsafe:
        cx, cy = _cursor_pos()
        if cx <= FAILSAFE_BOX and cy <= FAILSAFE_BOX:
            raise DesktopError("检测到光标被甩到屏幕左上角，已急停。")
    _user32().SetCursorPos(int(round(x)), int(round(y)))


def _mouse_button(down: bool) -> None:
    """按下 / 松开鼠标左键。

    直接发系统事件，不走 pyautogui.mouseDown/mouseUp —— 那两个同样会各 sleep 一个
    PAUSE（默认 0.1 秒），拖动前按下时多停 0.1 秒，轨迹的起手节奏就不对了。
    """
    flag = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
    _user32().mouse_event(flag, 0, 0, 0, 0)


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
                           "（在步骤编辑器里点【定位匹配…】重新框一张）")
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


def grab_rect_png(left: float, top: float, width: float, height: float) -> bytes:
    """按屏幕坐标截一小块，返回 PNG 字节。

    给验证码识别用：只要「验证码那一块」的当前像素，不要整屏 ——
    识别本来就只吃那一小块图，给整屏又慢又容易认到别的地方去。
    """
    import io
    from PIL import ImageGrab
    box = (int(round(left)), int(round(top)),
           int(round(left + width)), int(round(top + height)))
    buf = io.BytesIO()
    ImageGrab.grab(bbox=box).save(buf, "PNG")
    return buf.getvalue()


def grab_match_png(m: "DesktopMatch") -> bytes:
    """把匹配到的那块区域截下来 → PNG 字节。"""
    return grab_rect_png(m.x - m.width / 2, m.y - m.height / 2,
                         m.width, m.height)


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


def window_info_at(x: float, y: float) -> Optional[Tuple[str, Tuple[int, int, int, int]]]:
    """(x, y) 所在顶层窗口的 (标题, 矩形)；问不到返回 None。

    捕获时用它：知道是哪个窗口，才能「只截这个窗口」并把窗口名记下来。
    """
    try:
        from smart_tool.core import desktop_uia
        ctrl = desktop_uia.window_at(x, y)
        if ctrl is not None and ctrl.window_title:
            return ctrl.window_title, ctrl.rect
    except Exception:
        pass
    rect = _win32_window_rect(x, y)
    if rect is None:
        return None
    return _win32_window_title(x, y) or "", rect


def _win32_window_title(x: float, y: float) -> str:
    """Win32 兜底：拿这个位置所在根窗口的标题。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        u = ctypes.windll.user32
        from ctypes import wintypes
        hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return ""
        root = u.GetAncestor(hwnd, 2)
        length = u.GetWindowTextLengthW(root)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        u.GetWindowTextW(root, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


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


def _rect_from_match(win: DesktopMatch) -> Tuple[float, float, float, float]:
    """窗口命中（中心 + 宽高）→ 矩形 (左, 上, 右, 下)。"""
    return (win.x - win.width / 2, win.y - win.height / 2,
            win.x + win.width / 2, win.y + win.height / 2)


def _match_once_in_rect(rect, template_path: Path, threshold: float,
                        key: str, trace: List[str]) -> Optional[DesktopMatch]:
    """截一张全屏图，在 rect 那块里匹配模板 → 屏幕坐标（一次尝试）。"""
    path = Path(template_path)
    template = image_locator.imread_unicode(path)
    screen = _grab_bgr()
    ox, oy = screen_origin()
    left = int(round(rect[0] - ox)) - WINDOW_MARGIN
    top = int(round(rect[1] - oy)) - WINDOW_MARGIN
    right = int(round(rect[2] - ox)) + WINDOW_MARGIN
    bottom = int(round(rect[3] - oy)) + WINDOW_MARGIN
    ih, iw = screen.shape[:2]
    left, top = max(0, left), max(0, top)
    right, bottom = min(iw, right), min(ih, bottom)
    if right - left < 4 or bottom - top < 4:
        return None
    trace.clear()
    hit = image_locator.best_match(screen[top:bottom, left:right], template,
                                   threshold=threshold, key=key, trace=trace)
    if hit is None:
        return None
    return DesktopMatch(ox + left + hit.x, oy + top + hit.y,
                        hit.width, hit.height, hit.confidence, hit.scale)


def locate_by_window(window_template, target_template: Path, offset=(),
                     threshold: Optional[float] = None,
                     wait_s: float = DEFAULT_WAIT_S,
                     log: Callable[[str], None] = print,
                     title: str = "", window_size=(),
                     feature: str = "", feature_offset=()) -> DesktopMatch:
    """桌面定位主流程：**先认窗口，再在窗口里找控件**。

    认窗口的顺序（从上往下）：
    1. **窗口名**（捕获时记下的标题关键字）→ 直接问系统要窗口矩形，最稳最快，
       窗口挪了位置、改了大小都不怕；
    2. 没窗口名 / 名字对不上 → 拿「整窗截图」当模板在屏幕上找；
    3. 都不行 → 退回全屏匹配控件（老项目走这条，行为不变）。

    认到窗口之后（三条路都试，从准到稳）：
    - **深度定位**（勾了才有）：先在窗口里对一下**特征图**（捕获时框的那块“不会变的地方”）。
      对不上说明窗口内容跟捕获时差太多 → 不点，报错；
    - 在窗口里匹配**控件本身** → 点命中点（抗窗口内布局微调）；
    - 都没有 → 按**红框中心**点（窗口原点 + 记录的红框位置 × 窗口缩放）。

    窗口缩放怎么算：`当前窗口宽 ÷ 捕获时窗口宽`，红框坐标按这个比例换算，
    所以窗口被放大 / 换分辨率之后照样对得上。
    """
    limit = threshold if threshold is not None else image_locator.DEFAULT_THRESHOLD
    rect: Optional[Tuple[float, float, float, float]] = None
    scale = 1.0

    if title:
        r = window_rect_by_title(title)
        if r:
            rect = tuple(float(v) for v in r)
            log(f"  按窗口名找到「{title}」：屏幕({rect[0]:.0f},{rect[1]:.0f}) "
                f"大小 {rect[2] - rect[0]:.0f}×{rect[3] - rect[1]:.0f}")
        else:
            log(f"  没找到标题里含「{title}」的窗口")

    if rect is None and window_template and Path(window_template).is_file():
        log(f"  改用整窗截图找窗口：{Path(window_template).name}")
        win = find_window(window_template, wait_s=min(wait_s, 8.0), log=log)
        if win is not None:
            rect = _rect_from_match(win)

    if rect is not None:
        if window_size and len(window_size) >= 2 and window_size[0]:
            scale = (rect[2] - rect[0]) / float(window_size[0])
            if abs(scale - 1.0) > 0.02:
                log(f"  窗口大小跟捕获时不一样了，按比例 {scale:.3f} 换算红框坐标")
        deadline = time.monotonic() + max(0.0, min(wait_s, 6.0))
        while True:
            # 深度定位：先用特征图确认「这个窗口还是那个窗口、内容没跑偏」
            if feature and Path(feature).is_file():
                trace: List[str] = []
                ok = _match_once_in_rect(rect, Path(feature), limit,
                                         str(feature), trace)
                if ok is None:
                    log("  深度定位没对上（特征图没匹配到）："
                        + (trace[-1] if trace else "窗口内容跟捕获时差太多"))
                    log("  → 这次不点。确认目标窗口是最前面、内容没变，"
                        "或重新做一次「定位匹配」")
                    raise DesktopError(
                        f"深度定位失败：在窗口「{title or Path(window_template).name}」里"
                        f"没找到特征图 {Path(feature).name}。\n"
                        "    这一步不会乱点。请确认目标窗口已打开、内容跟捕获时一致，"
                        "或者关掉深度定位 / 重新捕获。")
                log(f"  深度定位通过（特征图匹配 {ok.confidence:.3f}）")
            hit = _match_once_in_rect(rect, target_template, limit,
                                      str(target_template), [])
            if hit is not None:
                hit.how = "窗口内匹配"
                log(f"  窗口内匹配到控件：屏幕({hit.x:.0f},{hit.y:.0f}) "
                    f"置信度={hit.confidence:.3f} 缩放={hit.scale:g}")
                return hit
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
        if offset and len(offset) >= 4:
            dx, dy, dw, dh = (float(v) for v in offset[:4])
            x = rect[0] + (dx + dw / 2) * scale
            y = rect[1] + (dy + dh / 2) * scale
            log(f"  窗口内没匹配到控件，按红框中心点：屏幕({x:.0f},{y:.0f})"
                f"（窗口缩放 {scale:.3f}）")
            return DesktopMatch(x, y, dw * scale, dh * scale, 1.0, scale,
                                how="红框中心")
        log("  窗口里没匹配到控件，也没记红框坐标 → 退回全屏匹配")
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
# 窗口：只截这个窗口（不截全屏）+ 按窗口名定位
# ------------------------------
def guess_window_keyword(title: str) -> str:
    """从窗口标题里猜一个「不会老是变」的关键字（运行时靠它找窗口）。

    「未命名 - 记事本」→ 记事本；「百度一下 - Google Chrome」→ Google Chrome；
    「文档1 - Word」→ Word。中间那截（文件名、网页标题）最容易变，取最后一段最稳。
    """
    text = (title or "").strip()
    for sep in (" - ", " — ", " – ", " － "):
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if parts:
                return parts[-1]
    return text


def _find_window(keyword: str):
    """按标题子串找窗口对象（多个命中时取面积最大的那个）。

    面积最大的通常是主窗口，避免匹配到“另存为”这类小对话框。
    """
    try:
        import pygetwindow as gw
    except Exception as e:
        raise DesktopError(f"没能加载 pygetwindow：{e}\n{missing_hint()}") from e
    kw = (keyword or "").strip().lower()
    if not kw:
        raise DesktopError("还没填窗口标题关键字")
    hits = [w for w in gw.getWindowsWithTitle("")
            if kw in (w.title or "").lower()]
    if not hits:
        titles = [t for t, _ in list_windows()][:10]
        raise DesktopError(
            f"没找到标题里含「{keyword}」的窗口。\n"
            "    当前可见窗口：" + ("、".join(titles) if titles else "（一个都没有）"))
    return max(hits, key=lambda w: int(getattr(w, "width", 0) or 0)
               * int(getattr(w, "height", 0) or 0))


def window_rect_by_title(keyword: str) -> Optional[Tuple[int, int, int, int]]:
    """按窗口名找窗口 → 它现在的屏幕矩形（物理像素，含标题栏）。

    这是运行时定位的首选：**窗口名 + 系统给的矩形**，不用靠图像去猜窗口在哪。
    找不到返回 None（调用方会退回图像匹配或全屏匹配）。
    """
    try:
        w = _find_window(keyword)
    except DesktopError:
        return None
    try:
        left, top = int(w.left), int(w.top)
        width, height = int(w.width), int(w.height)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def grab_window_win32(hwnd: int, rect: Tuple[int, int, int, int]):
    """用 PrintWindow 把一个窗口画进内存位图（只这个窗口，被遮挡也能截）。

    返回 PIL 图；拿不到（有些程序不支持、或者出来全黑）返回 None。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image
    except Exception:
        return None
    left, top, right, bottom = rect
    w, h = int(right - left), int(bottom - top)
    if w <= 0 or h <= 0:
        return None

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    if not mem or not bmp:
        user32.ReleaseDC(hwnd, hdc)
        return None
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):    # 2 = PW_RENDERFULLCONTENT
            return None
        gdi32.SelectObject(mem, old)                # GetDIBits 要求位图不在 DC 里
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h                  # 负＝自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0              # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")
        import numpy as np
        g = img.convert("L")
        if float(np.std(np.asarray(g))) < 3.0:      # 全黑/全白＝没截到
            return None
        return img
    except Exception:
        return None
    finally:
        try:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)
        except Exception:
            pass


def grab_window(title_keyword: str, log: Callable[[str], None] = print):
    """只截这个窗口（绝不截整屏）：返回 (PIL 图, 窗口矩形)。

    先试 PrintWindow（窗口被挡住也能截）；拿不到就把窗口切到最前、
    按窗口矩形那一块截。矩形与截图用的是同一套坐标，后面算偏移才准。
    """
    win = _find_window(title_keyword)
    try:
        title = (win.title or "").strip()
    except Exception:
        title = title_keyword
    rect = (int(win.left), int(win.top),
            int(win.left) + int(win.width), int(win.top) + int(win.height))
    hwnd = int(getattr(win, "_hWnd", 0) or 0) or int(getattr(win, "hWnd", 0) or 0)
    if hwnd:
        img = grab_window_win32(hwnd, rect)
        if img is not None:
            log(f"  已截取窗口「{title}」{img.width}×{img.height}（PrintWindow）")
            return img, rect
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
        time.sleep(0.35)
    except Exception:
        pass
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=rect)
    log(f"  已截取窗口「{title}」{img.width}×{img.height}（按窗口区域截）")
    return img, rect


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
    """把光标移到 (x, y)。开了拟人化就分步挪过去，否则一步到位。"""
    human, speed = _MOUSE["human"], _MOUSE["speed"]
    if human and speed > 0:
        _human_move(x, y, speed)
    else:
        _set_cursor(x, y)


def human_trace(sx: float, sy: float, x: float, y: float,
                duration: float) -> List[Tuple[float, float]]:
    """生成一条像人的移动轨迹（缓入缓出 + 轻微弧度 + 低频手抖）。

    抽成独立函数是为了让「按住拖动」也能用同一套轨迹 —— 滑块验证码比点击更怕
    被看出是机器：站点会检查按住期间的采样点，直线匀速、或者一步跳到位都很容易
    被判掉。

    返回中间帧的坐标（**不含**收尾那个精确的终点点，调用方自己补）。
    """
    sx, sy, x, y = float(sx), float(sy), float(x), float(y)
    dist = math.hypot(x - sx, y - sy)
    if dist < 1:
        return [(x, y)]
    frames = max(MOVE_MIN_FRAMES,
                 int(duration / MOVE_FRAME_S),
                 int(dist / MOVE_MAX_PX))
    frames = min(frames, MOVE_MAX_FRAMES)
    # 垂直方向上的单位向量：弧度和手抖都加在这个方向上（不影响前进的进度）
    ux, uy = -(y - sy) / dist, (x - sx) / dist
    bend = random.uniform(-1.0, 1.0) * min(MOVE_BEND_PX, dist * 0.06)
    amp = MOVE_TREMOR_PX if dist > 40 else 0.0
    phase = random.uniform(0.0, math.tau)
    pts: List[Tuple[float, float]] = []
    for i in range(1, frames + 1):
        t = i / frames
        e = t * t * (3 - 2 * t)             # 缓入缓出：起步慢、中间快、收尾慢
        # sin(πt) 让弧度两头归零、中间最大，收尾自然收敛到目标点
        off = (math.sin(math.pi * t) * bend
               + math.sin(phase + math.tau * 1.5 * t) * amp * math.sin(math.pi * t))
        pts.append((sx + (x - sx) * e + ux * off,
                    sy + (y - sy) * e + uy * off))
    return pts


def _step_through(points: List[Tuple[float, float]], duration: float):
    """按 points 一点点挪光标，整体耗时约 duration 秒。

    时间对齐到「开始时刻 + 这一帧应到的时间」：sleep 的误差不会被累加，
    不然帧多了会明显拖长（或越走越快）。
    """
    frames = len(points)
    t0 = time.perf_counter()
    for i, (px, py) in enumerate(points, 1):
        _set_cursor(px, py)
        gap = t0 + duration * (i / frames) - time.perf_counter()
        if gap > 0.0005:
            time.sleep(gap)


def drag(x0: float, y0: float, x1: float, y1: float,
         duration: float = 0.6, log: Callable[[str], None] = print):
    """按住左键，从 (x0,y0) 拖到 (x1,y1) 再松开（滑块验证码用）。

    按下之前先把光标**精确移到起点**：mouseDown 是按在「光标当前所在的地方」，
    光标不在起点的话，等于从别处开始拖 —— 拖动距离就全错了。
    """
    _ensure_dpi_aware()
    _set_cursor(x0, y0)
    time.sleep(0.05)
    log(f"  按住 ({x0:.0f},{y0:.0f}) 拖到 ({x1:.0f},{y1:.0f})，约 {duration:g} 秒")
    _mouse_button(True)
    try:
        _step_through(human_trace(x0, y0, x1, y1, duration), duration)
        _set_cursor(x1, y1)
        time.sleep(0.08)                  # 落点停一下再松手，真人就是这样
    finally:
        _mouse_button(False)


def click(x: float, y: float, times: int = 1):
    """在屏幕坐标点一下（times=2 就是双击）。

    开了「拟人化鼠标」时：光标分步移动过去（带一点抖动）、稍等一下再点，
    更像人手；不勾就是原来的「瞬移到位 + 立刻点」（最快）。
    """
    note(f"桌面：点击 ({x:.0f},{y:.0f}) x{times}")
    gui = _gui()
    _ensure_dpi_aware()
    move(x, y)
    human, _speed = _MOUSE["human"], _MOUSE["speed"]
    # 落点后稍微停一下再按下（真实操作里手也会停一下）
    time.sleep(random.uniform(0.05, 0.12) if human else 0.0)
    if int(times) >= 2:
        gui.doubleClick()
    else:
        gui.click()


def configure_mouse(human: bool = False, speed: float = 0.3) -> None:
    """设置桌面点击的鼠标行为（每次运行开始时由执行器调一次）。

    :param human: 拟人化移动（分步 + 缓入缓出 + 轻微抖动）
    :param speed: 移过去大概用多少秒（0＝不拟人，直接到位）
    """
    _MOUSE["human"] = bool(human)
    _MOUSE["speed"] = max(0.0, float(speed or 0))


def mouse_setting() -> Tuple[bool, float]:
    """当前的鼠标设置 (拟人化, 秒)。"""
    return bool(_MOUSE["human"]), float(_MOUSE["speed"])


def _human_move(x: float, y: float, duration: float):
    """分步把光标挪过去（缓入缓出 + 轻微弧线 + 低频手抖），最后精确落到目标点。

    轨迹本身在 `human_trace` 里（拖动也用它）；这里只负责按节奏把它放出来。
    两个要点，都是「一卡一卡」的病根：
    · 中间帧不走 pyautogui.moveTo——它每调一次会自己 sleep 0.1 秒，
      几十帧下来就变成一格一格跳（细节见 `_set_cursor`）；
    · 帧数取「按时间」和「按距离」里更密的那个，长距离不会被时间卡成
      一步跨上百像素的台阶。
    """
    if duration <= 0:
        _set_cursor(x, y)
        return
    sx, sy = _cursor_pos()
    _step_through(human_trace(sx, sy, x, y, duration), duration)
    _set_cursor(x, y)


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
