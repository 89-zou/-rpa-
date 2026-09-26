# -*- coding: utf-8 -*-
"""【试拖一下】：在编辑器里当场真拖一次，检查方向和距离配得对不对。

· 网页场景：另开一个浏览器窗口，打开项目里第一个「打开网页」的地址 →
  等起点元素出现 → 按住拖过去。浏览器**留给你自己看**（关掉那个窗口就结束）。
· 桌面场景：先把编辑器收起来，倒数 3 秒，再从模板图（没配图＝当前鼠标位置）
  真拖一次 —— 拖的就是你屏幕上正开着的那个程序。

拖动本身要花几百毫秒到几秒，还可能要在屏幕上找图（最长几秒），所以整套都放在
后台线程里跑，结果用信号回到主线程弹窗 —— 不然界面会卡死。
"""
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from smart_tool.core import desktop

#: 试拖时在屏幕上找图最多等几秒（正式运行是 desktop.DEFAULT_WAIT_S＝10 秒）
TRY_WAIT_S = 6.0
#: 网页起点元素最多等几秒（页面刚打开，慢站点留点余量）
TRY_ELEMENT_TIMEOUT_MS = 20000


def _clamp(x: float, y: float, w: float, h: float) -> Tuple[float, float]:
    """终点钳在视口里（留 4 像素边）：拖出边界后鼠标事件就跑到别的窗口去了。"""
    return min(max(x, 4.0), w - 4.0), min(max(y, 4.0), h - 4.0)


class DesktopDragTry(QThread):
    """桌面场景的试拖：3 秒倒数 → 找起点 → 真拖一次。

    image 留空＝从当前鼠标位置起拖。定位参数（窗口名 / 窗口图 / 红框位置 /
    特征图）跟正式运行时同一套，所以「试拖能成、运行也能成」。
    """

    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, project_dir: Path, image: str, win_title: str = "",
                 window: str = "", offset: Sequence[float] = (),
                 window_size: Sequence[float] = (), feature: str = "",
                 threshold: float = 0.0, angle: float = 0.0,
                 percent: int = 50, duration: float = 0.5, parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.image = (image or "").strip()
        self.win_title = (win_title or "").strip()
        self.window = (window or "").strip()
        self.offset = list(offset or [])
        self.window_size = list(window_size or [])
        self.feature = (feature or "").strip()
        self.threshold = float(threshold or 0)
        self.angle = float(angle or 0)
        self.percent = int(percent or 50)
        self.duration = float(duration or 0.5)
        self._stop = False

    def stop(self):
        """编辑器已经关了就不要再拖了（只在倒数那 3 秒里拦得住）。"""
        self._stop = True

    def _path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.project_dir / p

    def _locate(self):
        """跟运行时同一个打法：有窗口名 / 窗口图就先认窗口，再在窗口里找控件。"""
        path = self._path(self.image)
        threshold = self.threshold or None
        if self.window or self.win_title:
            wpath = self._path(self.window) if self.window else None
            fpath = self._path(self.feature) if self.feature else ""
            return desktop.locate_by_window(
                wpath, path, offset=self.offset, threshold=threshold,
                wait_s=TRY_WAIT_S, log=self.log.emit, title=self.win_title,
                window_size=self.window_size, feature=fpath)
        return desktop.locate(path, threshold=threshold, wait_s=TRY_WAIT_S,
                              log=self.log.emit)

    def run(self):
        try:
            if not desktop.available():
                self.done.emit(False, desktop.missing_hint())
                return
            for i in (3, 2, 1):
                self.log.emit(f"{i} 秒后开始拖（把手从鼠标上拿开）")
                time.sleep(1.0)
            if self._stop:
                return              # 编辑器被关掉了，别拖了
            if self.image:
                m = self._locate()
                x0, y0 = m.x, m.y
                where = (f"从图上 ({x0:.0f},{y0:.0f}) 开始"
                         f"（置信度 {m.confidence:.2f}"
                         + (f"，{m.how}" if m.how else "") + "）")
            else:
                x0, y0 = desktop.cursor_pos()
                where = f"从当前鼠标位置 ({x0:.0f},{y0:.0f}) 开始"
            sw, sh = desktop.screen_size()
            dx, dy = desktop.drag_delta(self.angle, self.percent, min(sw, sh))
            desktop.drag(x0, y0, x0 + dx, y0 + dy, duration=self.duration,
                         log=self.log.emit)
            self.done.emit(True, (
                f"{where}，朝「{desktop.angle_text(self.angle)}」拖了 "
                f"{self.percent}%（屏幕上约 {abs(dx) + abs(dy):.0f} 像素的位移，"
                f"用了 {self.duration:g} 秒）。\n\n"
                "看到鼠标动了吗？方向、距离对不对？\n"
                "不对就调上面的圆盘和百分比，再试一次。"
            ))
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


class WebDragTry(QThread):
    """网页场景的试拖：开浏览器 → 打开网址 → 等起点元素 → 真拖一次。

    跑完**不关浏览器**（就停在这一步的页面上），让你自己看拖出来的结果、
    手动再拖几下对比 —— 你把那个浏览器窗口关掉，这个线程就结束。
    """

    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, url: str, xpath: str, project_dir: Path,
                 loc_type: str = "xpath", angle: float = 0.0,
                 percent: int = 50, duration: float = 0.5, parent=None):
        super().__init__(parent)
        self.url = (url or "").strip()
        self.xpath = (xpath or "").strip()
        self.project_dir = Path(project_dir)
        self.loc_type = loc_type or "xpath"
        self.angle = float(angle or 0)
        self.percent = int(percent or 50)
        self.duration = float(duration or 0.5)
        self._stop = False

    def stop(self):
        """请线程收工（关掉浏览器）—— 只置标志，Playwright 只能在它自己的线程里调。"""
        self._stop = True

    # ---- 起点 ----
    def _start_point(self, page) -> Tuple[float, float, str]:
        if self.loc_type == "image":
            from smart_tool.core import image_locator
            path = Path(self.xpath)
            if not path.is_absolute():
                path = self.project_dir / path
            m = image_locator.locate_on_page(page, path, log=self.log.emit)
            return m.x, m.y, f"截图找到起点 ({m.x:.0f},{m.y:.0f})"
        el = page.locator(f"xpath={self.xpath}")
        try:
            el.wait_for(state="visible", timeout=TRY_ELEMENT_TIMEOUT_MS)
        except Exception as e:
            raise TimeoutError(
                f"等不到起点元素（{TRY_ELEMENT_TIMEOUT_MS // 1000} 秒）：{self.xpath}\n"
                f"   现在这个页面：{page.url}\n"
                f"   是不是这个 XPath 属于登录之后的页面？"
                f"（试拖没法替你登录，先手动在那个浏览器里登录再试）") from e
        try:
            el.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass                    # 滚不动就算了，box 拿得到就能拖
        box = el.bounding_box()
        if not box:
            raise ValueError("起点元素没有可见位置，算不出从哪里开始拖")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        return x, y, f"元素中心 ({x:.0f},{y:.0f})"

    def _drag(self, page, x0: float, y0: float, x1: float, y1: float):
        """按住拖过去：轨迹跟正式运行、以及滑块验证码共用同一套（像人的手）。"""
        mouse = page.mouse
        mouse.move(x0, y0)
        time.sleep(0.05)
        mouse.down()
        try:
            pts = desktop.human_trace(x0, y0, x1, y1, self.duration)
            frames = len(pts)
            t0 = time.perf_counter()
            for i, (px, py) in enumerate(pts, 1):
                mouse.move(px, py)
                gap = t0 + self.duration * (i / frames) - time.perf_counter()
                if gap > 0.0005:
                    time.sleep(gap)
            mouse.move(x1, y1)
            time.sleep(0.1)          # 落点停一下再松手
        finally:
            mouse.up()

    def run(self):
        from playwright.sync_api import sync_playwright
        browser = None
        try:
            with sync_playwright() as pw:
                self.log.emit("正在开浏览器…")
                browser = pw.chromium.launch(headless=False)
                page = browser.new_context().new_page()
                self.log.emit(f"打开 {self.url} …")
                page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                x0, y0, where = self._start_point(page)
                vw, vh = page.evaluate("() => [window.innerWidth, window.innerHeight]")
                dx, dy = desktop.drag_delta(self.angle, self.percent,
                                            min(float(vw), float(vh)))
                x1, y1 = _clamp(x0 + dx, y0 + dy, float(vw), float(vh))
                self.log.emit(f"{where} → 拖到 ({x1:.0f},{y1:.0f})")
                self._drag(page, x0, y0, x1, y1)
                dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                self.done.emit(True, (
                    f"拖完了：{where}，朝「{desktop.angle_text(self.angle)}」"
                    f"拖了 {self.percent}%（页面上约 {dist:.0f} 像素，"
                    f"用了 {self.duration:g} 秒）。\n\n"
                    "那个浏览器窗口我留着没关：你可以自己看看拖出来的结果、\n"
                    "再手动拖几下对比。（把它关掉就结束）"
                ))
                # 浏览器留给你自己看：等窗口被关掉，或者编辑器要收工
                while not self._stop:
                    try:
                        if page.is_closed():
                            break
                    except Exception:
                        break       # 浏览器没了（被任务管理器杀掉 / 崩了）
                    time.sleep(0.3)
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


def try_drag_desktop(parent, project_dir: Path, image: str, win_title: str = "",
                     window: str = "", offset: Sequence[float] = (),
                     window_size: Sequence[float] = (), feature: str = "",
                     threshold: float = 0.0, angle: float = 0.0,
                     percent: int = 50, duration: float = 0.5
                     ) -> DesktopDragTry:
    """藏起编辑器 → 真拖一次 → 回来报结果（弹窗）。返回线程，关窗时要 stop 它。"""
    from PyQt6.QtWidgets import QMessageBox
    QMessageBox.information(parent, "试拖一下", (
        "点【OK】后会把编辑器收起来，倒数 3 秒再开始真拖一次。\n\n"
        "先把手从鼠标上拿开，并让要拖的那个窗口显示在最前面\n"
        "（这一步拖的是你屏幕上的真实程序）。"
    ))
    thread = DesktopDragTry(project_dir, image, win_title=win_title,
                            window=window, offset=offset,
                            window_size=window_size, feature=feature,
                            threshold=threshold, angle=angle, percent=percent,
                            duration=duration, parent=parent)

    def _on_done(ok: bool, text: str):
        if getattr(parent, "_closing", False):
            return                  # 编辑器已经关掉了，别再把弹窗叫出来
        parent.show()               # 拖之前把它藏起来了，现在放回来
        (QMessageBox.information if ok else QMessageBox.warning)(
            parent, "试拖结果", text)

    thread.done.connect(_on_done)
    thread.start()
    parent.hide()                   # 别让编辑器挡住（也可能挡住鼠标要拖的位置）
    return thread


def try_drag_web(parent, project_dir: Path, url: str, xpath: str,
                 loc_type: str = "xpath", angle: float = 0.0,
                 percent: int = 50, duration: float = 0.5,
                 on_log: Optional[Callable[[str], None]] = None) -> WebDragTry:
    """开浏览器真拖一次；结果弹窗报回来，浏览器留着给用户看。"""
    from PyQt6.QtWidgets import QMessageBox
    thread = WebDragTry(url, xpath, project_dir, loc_type=loc_type,
                        angle=angle, percent=percent, duration=duration,
                        parent=parent)
    if on_log is not None:
        thread.log.connect(on_log)

    def _on_done(ok: bool, text: str):
        if getattr(parent, "_closing", False):
            return                  # 编辑器已经关掉了，别再把弹窗叫出来
        (QMessageBox.information if ok else QMessageBox.warning)(
            parent, "试拖结果", text)

    thread.done.connect(_on_done)
    thread.start()
    return thread