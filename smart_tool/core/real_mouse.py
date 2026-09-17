# -*- coding: utf-8 -*-
"""真实鼠标：用 pyautogui 发 OS 级鼠标输入（默认关闭，个别站点需要时才开）。

为什么要它：Playwright 的 `page.mouse` 走 CDP 合成事件；有些站点（canvas 画板、
拖拽类控件、盯自动化特征的站）只认操作系统的真输入。

坐标为什么不能靠公式算（实测数据，本机 125% 缩放的 1080p 屏）：
    页面报的 viewport 坐标 = 0.80 × 屏幕坐标 - 偏移
也就是「屏幕坐标 → viewport 坐标」是**缩放 + 平移**的仿射关系，不只是平移。
而 `window.devicePixelRatio` 在这条路上会报 1.0（浏览器被强制成 1 倍缩放），
所以「窗口位置 + 边框高度」这种算法实测能偏 150 像素。
解决办法是**两点标定**：把光标移到两个已知的屏幕位置，问页面「你收到的鼠标在
哪」，两个点就能把比例和偏移一起解出来，之后每次点击前再自校验一次（窗口被
挪动、缩放变了都能自动跟上）。

其余代价（界面上也写了）：
- 浏览器窗口必须在最前面：被别的窗口盖住时点击会落到那个窗口上。页面收不到
  鼠标移动即判定「不在最前面」，本次运行退回普通点击。
- 会霸占你真实的鼠标：跑的时候别动电脑。紧急情况把鼠标猛地甩到屏幕左上角，
  pyautogui 的 FAILSAFE 会立刻中断。

只接管鼠标点击；键盘仍走 Playwright（`fill` 直接写值更可靠，也不影响这些站点）。
"""
import sys
import time
from typing import Callable, Optional, Tuple

from playwright.sync_api import Page, Error as PlaywrightError

# 记录「页面收到的鼠标位置」的监听器（只装一次）
LISTEN_JS = """() => {
  if (window.__traeMouse) { return true; }
  window.__traeMouse = {x: null, y: null};
  addEventListener('mousemove', (e) => {
    window.__traeMouse.x = e.clientX;
    window.__traeMouse.y = e.clientY;
  }, true);
  return true;
}"""

READ_JS = "() => window.__traeMouse ? [window.__traeMouse.x, window.__traeMouse.y] : null"
RESET_JS = ("() => { if (window.__traeMouse) {"
            " window.__traeMouse.x = null; window.__traeMouse.y = null; } }")

# 粗算用的窗口信息（**故意不用 devicePixelRatio**：它会被浏览器报成 1）
METRICS_JS = """() => ({
  sx: window.screenX, sy: window.screenY,
  ox: window.outerWidth, oy: window.outerHeight,
  ix: window.innerWidth, iy: window.innerHeight
})"""

# 标定时第二个探测点相对第一个的偏移（屏幕像素）
PROBE_STEPS = ((200, 200), (-200, 200), (200, -200), (-200, -200))
# 认为「这次点对了」的容差（CSS 像素）
TOLERANCE = 1.5
# 等页面回报鼠标位置的最长时间（秒）
READ_TIMEOUT = 0.5
# 合理的缩放比范围（超出这个范围说明标定算错或窗口怪）
SCALE_RANGE = (0.4, 3.0)


class RealMouseUnavailable(Exception):
    """真实鼠标用不了（pyautogui 没装 / 窗口不在最前面 / 页面没响应）。"""


def available() -> bool:
    """pyautogui 装了没（没装就走普通点击，不影响其他功能）。"""
    try:
        import pyautogui  # noqa: F401
    except Exception:
        return False
    return True


def _gui():
    """取 pyautogui 模块；没装就给一句能照着做的中文提示。"""
    try:
        import pyautogui
    except Exception as e:
        raise RealMouseUnavailable(
            "「真实鼠标」需要 pyautogui，但没能加载：{}\n"
            "    在项目的 .venv 里执行：.venv\\Scripts\\python -m pip install pyautogui"
            .format(e)
        ) from e
    return pyautogui


def _ensure_dpi_aware():
    """尽最大努力让进程按物理像素报坐标。

    Qt6 启动时一般已经设过（重复设置会失败，无副作用）；这里给纯命令行的
    run_cli.py 兜底。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_V2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class RealMouse:
    """把页面的 viewport 坐标换成屏幕坐标，并发真实鼠标点击。"""

    def __init__(self, page: Page, log: Callable[[str], None] = print):
        self._page = page
        self._log = log
        self._calibrated = False
        self._kx = 1.0                 # 屏幕像素 / viewport 像素
        self._ky = 1.0
        self._bx = 0.0                 # 屏幕坐标 = k × viewport 坐标 + b
        self._by = 0.0
        self._listener = False
        self._disabled = False         # 用不了 → 本次运行不再试

    # ------------------------------
    # 对外
    # ------------------------------
    def click(self, x: float, y: float) -> None:
        """在 viewport 坐标 (x, y) 处发一次真实点击。

        用不了（pyautogui 没装、窗口不在最前面、页面太小……）就抛
        RealMouseUnavailable，调用方退回普通点击。
        """
        if self._disabled:
            raise RealMouseUnavailable("真实鼠标本次运行已停用（原因见日志）")
        gui = _gui()
        _ensure_dpi_aware()
        self._ensure_listener()
        if not self._calibrated:
            self._calibrate(gui)

        sx, sy = self._to_screen(x, y)
        reported = self._move_and_read(gui, sx, sy)
        if reported is None:
            self._fail("页面没收到鼠标移动")
        if (abs(x - reported[0]) > TOLERANCE
                or abs(y - reported[1]) > TOLERANCE):
            # 窗口被挪动 / 缩放变了 → 重新标定一次再点
            self._log("  真实鼠标：落点对不上，重新标定一次")
            self._calibrated = False
            self._calibrate(gui)
            sx, sy = self._to_screen(x, y)
            reported = self._move_and_read(gui, sx, sy)
        extra = ""
        if reported is not None:
            dx, dy = reported[0] - x, reported[1] - y
            extra = f"（实测落点 {reported[0]:.0f},{reported[1]:.0f}）"
            if abs(dx) > TOLERANCE or abs(dy) > TOLERANCE:
                extra += f" 注意仍偏 {dx:+.0f},{dy:+.0f} 像素"
        self._log(f"  真实鼠标：viewport({x:.0f},{y:.0f}) → "
                  f"屏幕({sx:.0f},{sy:.0f}){extra}")
        gui.click()

    # ------------------------------
    # 标定
    # ------------------------------
    def _calibrate(self, gui) -> None:
        """两点标定：解出 viewport → 屏幕 的比例与偏移。

        探测点取 viewport 中心（不在元素上，避免顺手触发页面交互），
        第二个点沿对角线挪 200 屏幕像素，两点都落在页面里才成立。
        """
        vp = self._page.viewport_size or {"width": 800, "height": 600}
        cx = float(vp.get("width") or 800) / 2
        cy = float(vp.get("height") or 600) / 2
        s1 = self._raw_guess(cx, cy)
        r1 = self._move_and_read(gui, *s1)
        if r1 is None:
            self._fail("页面没收到鼠标移动")

        for ox, oy in PROBE_STEPS:
            s2 = (s1[0] + ox, s1[1] + oy)
            r2 = self._move_and_read(gui, *s2)
            if r2 is None:
                continue
            drx, dry = r2[0] - r1[0], r2[1] - r1[1]
            if abs(drx) < 20 or abs(dry) < 20:
                continue            # 探测点跑到页面外了（页面太小），换个方向
            kx = (s2[0] - s1[0]) / drx
            ky = (s2[1] - s1[1]) / dry
            if not (SCALE_RANGE[0] <= kx <= SCALE_RANGE[1]
                    and SCALE_RANGE[0] <= ky <= SCALE_RANGE[1]):
                continue
            self._kx, self._ky = kx, ky
            self._bx = s1[0] - kx * r1[0]
            self._by = s1[1] - ky * r1[1]
            self._calibrated = True
            self._log(f"  真实鼠标：已标定（屏幕/页面 缩放 {kx:.2f}×{ky:.2f}，"
                      f"偏移 {self._bx:.0f},{self._by:.0f}）")
            return
        self._fail("标定失败（浏览器窗口不在最前面，或窗口太小）")

    def _fail(self, why: str):
        """用不了：记一次日志并停用，调用方会退回普通点击。"""
        self._disabled = True
        raise RealMouseUnavailable(f"{why}。浏览器窗口要可见、在最前面，"
                                   "且全程别动鼠标键盘。本次运行改用普通点击。")

    # ------------------------------
    # 坐标
    # ------------------------------
    def _to_screen(self, x: float, y: float) -> Tuple[float, float]:
        return (self._kx * x + self._bx, self._ky * y + self._by)

    def _raw_guess(self, x: float, y: float) -> Tuple[float, float]:
        """粗算（只用来决定「先把光标移进页面」的落点，精度靠标定补）。"""
        try:
            m = self._page.evaluate(METRICS_JS)
        except PlaywrightError:
            m = None
        if not m:
            return x, y
        chrome_h = max(0.0, float(m["oy"]) - float(m["iy"]))
        return (float(m["sx"]) + x, float(m["sy"]) + chrome_h + y)

    def _move_and_read(self, gui, sx: float, sy: float):
        """把光标移过去（绝对定位，不带缓动），返回页面回报的 viewport 坐标。"""
        try:
            self._page.evaluate(RESET_JS)
        except PlaywrightError:
            return None
        gui.moveTo(int(round(sx)), int(round(sy)))      # duration=0：绝对定位，最准
        pos = self._read(READ_TIMEOUT)
        if pos is not None:
            return pos
        # 光标本来就在落点上时系统不发 mousemove：轻轻抖一下再看
        gui.moveTo(int(round(sx)) + 4, int(round(sy)) + 4)
        gui.moveTo(int(round(sx)), int(round(sy)))
        return self._read(READ_TIMEOUT)

    def _read(self, timeout: float):
        """轮询页面回报的鼠标位置（顺便泵事件，让页面执行监听器）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pos = self._page.evaluate(READ_JS)
            except PlaywrightError:
                return None
            if pos and pos[0] is not None and pos[1] is not None:
                return float(pos[0]), float(pos[1])
            time.sleep(0.03)
        return None

    def _ensure_listener(self):
        if self._listener:
            return
        try:
            self._page.evaluate(LISTEN_JS)
            self._listener = True
        except PlaywrightError as e:
            self._fail(f"页面还没准备好（{e}）")
