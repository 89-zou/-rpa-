# -*- coding: utf-8 -*-
"""桌面元素捕获器：鼠标划到哪就高亮哪个控件，点一下自动裁出它当模板。

和网页那边的【捕获元素…】对应的东西。桌面没有 XPath，但用 Windows 的
UI Automation 能拿到鼠标下控件的**精确矩形和名字**，所以能做到「点一下就抓」，
比手工拖框准得多。

怎么不打扰目标程序：
- 遮罩是**输入穿透**的（`WindowTransparentForInput`）：鼠标点得到下面的程序；
- 真正拦住点击的是低级鼠标钩子（WH_MOUSE_LL）：捕获期间左/右键被我们吃掉，
  不会让目标程序真的被点到；
- 因为遮罩不参与命中测试，UIA 查询照样能拿到下面的真控件（实测过）。

交互（遮罩上也会写一遍）：
- 移动鼠标 → 高亮鼠标下的控件，显示名字 / 类型 / 尺寸 / 所属窗口
- **左键** → 捕获这个控件（自动裁图存进 img/）
- **右键** → 选上一层（想框住整个容器时用）
- **按住左键拖** → 手动框选（UIA 认不出的自绘界面走这条路）
- **Esc** → 取消
"""
import ctypes
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QEventLoop, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from smart_tool.core import desktop
from smart_tool.core import desktop_uia
from smart_tool.core import crash_guard
from smart_tool.core.desktop_uia import UiControl

# 鼠标位置轮询间隔（毫秒）；太快会一直查 UIA，太慢会拖影
POLL_MS = 40
# 「让开屏幕」时给不能 hide() 的窗口用的坐标（挪到虚拟桌面外面）
PARK_XY = -32000
# 拖动超过这么多物理像素才算「手动框选」（否则当成点击）
DRAG_MIN = 12
# 框选最小尺寸
MIN_SIZE = 6
# 虚拟键
VK_ESCAPE = 0x1B

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
MOUSE_EVENTS = {WM_LBUTTONDOWN: "left_down", WM_LBUTTONUP: "left_up",
                WM_RBUTTONDOWN: "right_down", WM_RBUTTONUP: "right_up"}


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class InputHook:
    """低级输入钩子：捕获期间把鼠标点击和 Esc 都截下来、并且吃掉。

    - 鼠标：左右键的按下/抬起 → "left_down" / "left_up" / "right_down" / "right_up"
    - 键盘：Esc → "esc"

    为什么不轮询 `GetAsyncKeyState`：本机实测它对鼠标和键盘**一律返回 0**
    （按着不放也报未按下），完全靠不住，所以按键状态全部由钩子事件来定。

    钩子回调会跑在装钩子的线程（Qt 事件循环负责派发），所以回调里只往队列里
    塞一个字符串就完事——回调变慢会被系统摘掉钩子。
    """

    #: 鼠标按下/抬起消息 → 事件名
    MOUSE_EVENTS = MOUSE_EVENTS

    def __init__(self, on_event: Callable[[str], None]):
        self._on_event = on_event
        self._mouse_proc = None
        self._kbd_proc = None
        self._mouse_handle = None
        self._kbd_handle = None

    @property
    def installed(self) -> bool:
        return bool(self._mouse_handle)

    def install(self) -> bool:
        if not hasattr(ctypes, "WINFUNCTYPE"):
            return False
        user32 = ctypes.windll.user32
        # 必须显式声明参数类型：默认按 32 位 int 转换，指针值会溢出报错；
        # 而回调里抛异常等于「已处理」，会把鼠标消息默默吞掉。
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

        self._mouse_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )(self._mouse_cb)
        self._mouse_handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_proc, None, 0)
        self._kbd_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )(self._kbd_cb)
        self._kbd_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._kbd_proc, None, 0)
        return bool(self._mouse_handle)

    def uninstall(self):
        user32 = ctypes.windll.user32
        for h in (self._mouse_handle, self._kbd_handle):
            if h:
                user32.UnhookWindowsHookEx(h)
        self._mouse_handle = self._kbd_handle = None
        self._mouse_proc = self._kbd_proc = None

    def _emit(self, name: str) -> int:
        try:
            self._on_event(name)
        except Exception:
            pass
        return 1                        # 吃掉，别让目标程序收到

    def _next(self, code, wparam, lparam) -> int:
        """转给下一个钩子。

        **绝对不能抛异常**：异常一旦逃出 Windows 回调，系统会以
        STATUS_FATAL_USER_CALLBACK_EXCEPTION（0xc000041d）把整个进程杀掉，
        而且不会留 Python 的崩溃日志（事件日志里只会看到 qwindows.dll）。
        """
        try:
            return ctypes.windll.user32.CallNextHookEx(
                None, code, wparam, _as_signed(lparam))
        except Exception:
            return 0

    def _mouse_cb(self, code, wparam, lparam):
        try:
            if code >= 0 and wparam in MOUSE_EVENTS:
                return self._emit(MOUSE_EVENTS[wparam])
            return self._next(code, wparam, lparam)
        except Exception:
            return self._next(code, wparam, lparam)

    def _kbd_cb(self, code, wparam, lparam):
        try:
            if code >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                info = ctypes.cast(
                    lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                if info.vkCode == VK_ESCAPE:
                    return self._emit("esc")
            return self._next(code, wparam, lparam)
        except Exception:
            return self._next(code, wparam, lparam)


def _as_signed(value: int) -> int:
    """把指针值从无符号大整数转回有符号（Windows 回调给的是无符号的）。"""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _cursor_pos() -> Tuple[int, int]:
    """当前光标的屏幕物理坐标。"""
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


class _Overlay(QWidget):
    """全屏半透明遮罩：画高亮框和提示条（不吃输入）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        geo = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geo)
        self._origin = geo.topLeft()
        self.dpr = float(QGuiApplication.primaryScreen().devicePixelRatio() or 1)
        self.box: Optional[QRect] = None      # 控件高亮（控件坐标）
        self.title = ""
        self.info = ""
        self.hint = ""
        self.drag_box: Optional[QRect] = None
        # 截图取模板的那一瞬间置 True：什么都不画＝完全透明，
        # 免得裁出来的模板带上遮罩的暗色和橙框
        self.blank = False

    def to_local(self, rect: Tuple[int, int, int, int]) -> QRect:
        """屏幕物理像素 → 遮罩自己的坐标。"""
        x, y, r, b = rect
        return QRect(
            int(x / self.dpr) - self._origin.x(),
            int(y / self.dpr) - self._origin.y(),
            max(1, int((r - x) / self.dpr)),
            max(1, int((b - y) / self.dpr)),
        )

    def paintEvent(self, _event):
        # 画东西时出错只丢一帧，绝不能让异常逃进 Qt 的事件分发（会直接终止进程）
        try:
            self._paint()
        except Exception:
            pass

    def _paint(self):
        if self.blank:
            return                  # 完全透明（截图那一瞬间）
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 46))

        box = self.drag_box or self.box
        if box is not None:
            p.setPen(QPen(QColor("#f08a24"), 3))
            p.drawRect(box)

        # 左上角提示条
        font = QFont("Microsoft YaHei", 10)
        p.setFont(font)
        lines = [ln for ln in (self.title, self.info, self.hint) if ln]
        if not lines:
            return
        metrics = p.fontMetrics()
        w = max(metrics.horizontalAdvance(ln) for ln in lines) + 24
        h = len(lines) * (metrics.height() + 2) + 16
        panel = QRect(self.rect().left() + 16, self.rect().top() + 16, w, h)
        p.fillRect(panel, QColor(20, 24, 33, 225))
        p.setPen(QColor("#ffffff"))
        y = panel.top() + 8
        for i, ln in enumerate(lines):
            p.setPen(QColor("#f6ad55") if i == 0 else QColor("#e5e7eb"))
            p.drawText(panel.left() + 12, y + metrics.ascent(), ln)
            y += metrics.height() + 2


class DesktopPickerDialog(QDialog):
    """桌面元素捕获窗口。accept 后用 result_path / result_text 取结果。"""

    def __init__(self, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("桌面元素捕获")
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self.result_path = ""       # img/xxx.png
        self.result_text = ""       # 控件描述（写进步骤编辑器的提示）
        self.window_title = ""      # 控件所属窗口标题（可以填进「激活窗口」）
        self.window_path = ""       # 顺手存下来的整窗截图（运行时认窗口用）
        self.offset: list = []      # 红框相对窗口左上角的位置 [dx,dy,宽,高]

        self._point: Optional[Tuple[int, int]] = None
        self._ctrl: Optional[UiControl] = None
        self._up = 0                # 「上一层」的层数
        self._dirty = True
        self._pending: list = []    # 钩子记下的输入事件
        self._press_at: Optional[Tuple[int, int]] = None
        self._drag_to: Optional[Tuple[int, int]] = None
        self._left_down = False     # 左键按住中（由钩子事件维护）
        self._captured = False
        self._loop: Optional[QEventLoop] = None
        self._hidden: list = []     # 捕获期间藏起来的自家窗口
        self._parked: list = []     # 挪走的自家窗口：[(窗口, 原几何, 原窗口状态)]
        self._error = ""            # 上一轮出错的原因（显示在遮罩上）

        self.overlay = _Overlay(self)
        self.hook = InputHook(self._pending.append)
        self._hooked = False        # 钩子在 run() 里才装（中途退出也要保证能卸掉）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # ------------------------------
    # 生命周期
    # ------------------------------
    def run(self) -> bool:
        """打开遮罩开始捕获；返回是否拿到了结果。

        捕获期间会**临时把本工具自己的窗口都藏起来**：不然它们挡在目标程序前面，
        截出来的模板就是自家界面（而且用户也看不全目标程序）。
        本类自己是 QDialog，但从不显示（只借它的父子关系），
        停在那儿等结果的是个 QEventLoop。
        """
        if not desktop.available():
            QMessageBox.critical(self, "缺少依赖", desktop.missing_hint())
            return False
        if not desktop_uia.available():
            QMessageBox.information(
                self, "没法做元素捕获",
                "这台机器上用不了 UI Automation（uiautomation 没装）。\n"
                "可以改用【截屏取模板…】手工框选。\n\n" + desktop_uia.missing_hint(),
            )
            return False

        self._hooked = self.hook.install()
        try:
            self._clear_screen()
            crash_guard.note("桌面捕获：显示遮罩")
            self.overlay.show()
            self.overlay.raise_()
            self.timer.start(POLL_MS)
            self._loop = QEventLoop()
            self._loop.exec()
        finally:
            self._loop = None
            self.timer.stop()
            self.hook.uninstall()       # 漏卸的话它会一直吃掉鼠标点击
            self.overlay.hide()
            self._restore_screen()
        return bool(self.result_path)

    # ------------------------------
    # 让开屏幕 / 还原
    # ------------------------------
    def _clear_screen(self):
        """捕获期间把本工具自己的窗口让开，好看清目标程序。

        普通窗口直接 `hide()`；但**正在 exec() 的模态对话框绝对不能 hide()**：
        Qt 的嵌套事件循环会当场返回 0（不是 Accepted），之后点【保存】就再也没
        反应了，而且 Qt 的模态列表里会留下一个僵尸条目，反复几次整个界面就会
        闪退。这类窗口改成「挪到虚拟桌面外面」——可见性没变，模态状态不受影响。
        """
        for w in QApplication.topLevelWidgets():
            if w is self.overlay or w is self or not w.isVisible():
                continue
            try:
                if w.isModal():
                    self._park(w)
                else:
                    crash_guard.note("桌面捕获：临时隐藏本工具窗口")
                    w.hide()
                    self._hidden.append(w)
            except Exception:
                pass

    def _park(self, w: QWidget):
        """把窗口挪到屏幕外（原几何与窗口状态记下来，事后还原）。"""
        state = w.windowState()
        self._parked.append((w, w.geometry(), state))
        if state & Qt.WindowState.WindowMaximized:
            w.setWindowState(Qt.WindowState.WindowNoState)
        w.move(PARK_XY, PARK_XY)

    def _restore_screen(self):
        for w in self._hidden:
            try:
                w.show()
                w.raise_()
                w.activateWindow()
            except Exception:
                pass
        self._hidden = []
        for w, geo, state in self._parked:
            try:
                w.setWindowState(Qt.WindowState.WindowNoState)
                w.setGeometry(geo)
                if state:
                    w.setWindowState(state)
                w.raise_()
                w.activateWindow()
            except Exception:
                pass
        self._parked = []

    # ------------------------------
    # 主循环
    # ------------------------------
    def _tick(self):
        """定时器槽：查鼠标位置、更新高亮、处理钩子事件。

        整体包一层 try/except：Qt 槽里抛异常在 PyQt6 里是「致命」的
        （会走 abort，界面直接消失），所以这里宁可记一笔也别让它逃出去。
        """
        try:
            self._tick_inner()
        except Exception as e:
            try:
                self._error = f"{type(e).__name__}: {e}"
                self.overlay.title = "桌面元素捕获"
                self.overlay.info = "出错了（已跳过这一轮）"
                self.overlay.hint = f"{self._error}　Esc＝退出"
                self.overlay.blank = False
                self.overlay.update()
            except Exception:
                pass            # 连提示都画不出来就算了，千万别往外抛

    def _tick_inner(self):
        if self._captured:
            return
        pos = _cursor_pos()
        if pos != self._point:
            self._point = pos
            if not self._left_down:
                self._up = 0            # 换位置就把「上一层」归零（拖框中不动它）
            self._dirty = True
            crash_guard.note(f"桌面捕获：查询控件 ({pos[0]},{pos[1]})")
        if self._left_down:
            self._drag_to = pos         # 拖框中：跟着鼠标画框

        while self._pending:
            ev = self._pending.pop(0)
            if ev == "esc":
                self.reject()
                return
            if ev == "right_down":
                self._up += 1
                self._dirty = True
            elif ev == "left_down":
                self._left_down = True
                self._press_at = pos
                self._drag_to = pos
            elif ev == "left_up":
                self._left_down = False
                start = self._press_at or pos
                self._press_at = self._drag_to = None
                # 拖得够远＝手动框选，否则＝点选高亮的控件
                if max(abs(pos[0] - start[0]),
                       abs(pos[1] - start[1])) >= DRAG_MIN:
                    self._finish_manual(start, pos)
                elif self._ctrl is not None:
                    self._finish_control(self._ctrl)
                else:
                    self.overlay.hint = (
                        "这里没识别到控件（自绘界面？）——"
                        "按住左键拖一个框，或用【截屏取模板…】")
                    self.overlay.update()
                return

        if self._dirty:
            self._dirty = False
            self._refresh()

    def _refresh(self):
        """重新查一次鼠标下的控件，刷新提示。"""
        self._ctrl = None
        if self._point:
            self._ctrl = desktop_uia.control_at(*self._point, up=self._up)
        ov = self.overlay
        if self._left_down and self._press_at and self._drag_to:
            # 拖框中：画拖出来的框，不再高亮控件
            x1, y1 = self._press_at
            x2, y2 = self._drag_to
            ov.drag_box = ov.to_local((min(x1, x2), min(y1, y2),
                                       max(x1, x2), max(y1, y2)))
            ov.box = None
            ov.title = "桌面元素捕获"
            ov.info = f"手动框选：{abs(x2 - x1)}×{abs(y2 - y1)}"
            ov.hint = "松开左键就取这块    Esc＝取消"
            ov.update()
            return

        ov.drag_box = None
        if self._ctrl is None:
            ov.box = None
            ov.title = "桌面元素捕获"
            ov.info = "移动鼠标到目标控件上（这里没识别到控件）"
            ov.hint = self._hint_line()
        else:
            ov.box = ov.to_local(self._ctrl.rect)
            ov.title = "桌面元素捕获"
            up_txt = f"（已上移 {self._up} 层）" if self._up else ""
            ov.info = f"当前：{self._ctrl.describe()}{up_txt}"
            ov.hint = self._hint_line()
        ov.update()

    def _hint_line(self) -> str:
        extra = "" if self._hooked else "   ⚠ 钩子没装上，点击会落到目标程序上"
        return ("左键＝捕获它   右键＝选上一层   按住左键拖＝手动框选   "
                f"Esc＝取消{extra}")

    # ------------------------------
    # 出结果
    # ------------------------------
    def _finish_control(self, ctrl: UiControl):
        self._capture(ctrl.rect)
        if self.result_path:
            self.window_title = ctrl.window_title
            self.result_text = ctrl.describe()
            if ctrl.window_title:
                self.result_text += f"　窗口：{ctrl.window_title}"
        self.accept()

    def _finish_manual(self, a: Tuple[int, int], b: Tuple[int, int]):
        """手动拖出来的框（按住了左键拖）。"""
        x1, y1 = min(a[0], b[0]), min(a[1], b[1])
        x2, y2 = max(a[0], b[0]), max(a[1], b[1])
        if x2 - x1 < MIN_SIZE or y2 - y1 < MIN_SIZE:
            self.overlay.hint = "框太小了，再拖大一点"
            self.overlay.update()
            return
        self._capture((x1, y1, x2, y2))
        if self.result_path:
            self.result_text = f"手动框选 {x2 - x1}×{y2 - y1}"
            if self._ctrl is not None and self._ctrl.window_title:
                self.window_title = self._ctrl.window_title
                self.result_text += f"　窗口：{self._ctrl.window_title}"
        self.accept()

    def _capture(self, rect: Tuple[int, int, int, int]):
        """把这块屏幕裁下来存进 img/（rect 是屏幕物理像素）。

        截之前先把遮罩「清空」成完全透明：不然裁出来的模板会带上遮罩的暗色和橙框，
        匹配置信度明显变差（实测从 1.00 掉到 0.89）。
        """
        self.overlay.blank = True
        self.overlay.update()
        QApplication.processEvents()
        time.sleep(0.08)                # 等这一帧真的合成上去
        crash_guard.note("桌面捕获：截屏取模板")
        try:
            img = desktop.grab_screen()
        except Exception as e:
            self._show_overlay()
            QMessageBox.critical(self, "截屏失败", str(e))
            return
        ox, oy = desktop.screen_origin()
        box = (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)
        box = (max(0, box[0]), max(0, box[1]),
               min(img.width, box[2]), min(img.height, box[3]))
        if box[2] - box[0] < MIN_SIZE or box[3] - box[1] < MIN_SIZE:
            self._show_overlay()
            QMessageBox.information(self, "这块太小了",
                                    "选中的区域太小，认不准。请换一块更大的控件。")
            return
        self.img_dir.mkdir(parents=True, exist_ok=True)
        stem = "cap_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.img_dir / f"{stem}.png"
        i = 1
        while path.exists():
            path = self.img_dir / f"{stem}_{i}.png"
            i += 1
        try:
            img.crop(box).save(str(path))
        except Exception as e:
            self._show_overlay()
            QMessageBox.critical(self, "保存失败", f"模板图存不进去：\n{e}")
            return
        self._captured = True
        self.result_path = f"img/{path.name}"
        # 顺手存一张整窗截图 + 红框相对窗口的位置：运行时先认窗口，
        # 把找控件的范围缩到一个窗口里；认不到就退回全屏匹配。
        try:
            from smart_tool.ui.screen_capture import save_window_template
            self.window_path, self.offset = save_window_template(
                img, box, (ox, oy), self.img_dir, path.stem)
        except Exception:
            self.window_path, self.offset = "", []

    def _show_overlay(self):
        """把遮罩恢复成可见状态（截图失败、要继续选时用）。"""
        self.overlay.blank = False
        self.overlay.update()

    # ------------------------------
    # 关窗
    # ------------------------------
    def accept(self):
        """拿到结果：结束等待循环（本窗口自己不显示）。"""
        if self._loop is not None:
            self._loop.quit()
            return
        super().accept()

    def reject(self):
        """取消（Esc 或出错）：结束等待循环。"""
        if self._loop is not None:
            self._loop.quit()
            return
        super().reject()
