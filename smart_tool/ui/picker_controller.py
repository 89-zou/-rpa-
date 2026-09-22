# -*- coding: utf-8 -*-
"""元素捕获控制器：捕获期间屏幕上只留这么一个小窗。

为什么要它：捕获网页元素时经常得「先自己操作页面」（翻页、登录、展开菜单），
但捕获脚本会把所有点击吃掉 —— 所以给它一个开关：

    捕获中 ──【打断】──▶ 已打断 ──【恢复】──▶ 捕获中
                          └────【结束】────▶ 关浏览器 + 主页回来

控制器就三种状态：捕获中 / 已打断 / 已结束（结束那一瞬恢复主页后自己消失）。
它长得跟「运行小窗」一个路子——右下角、无边框、永远置顶、可以拖着走，
所以不挡着你要点的地方。

【防卡死铁律】这套是踩坑换来的，改的时候别破：
1. 控制器里**绝不弹 QMessageBox / QInputDialog**。这个窗口是置顶的，而它弹出来的
   模态框**不继承**置顶（QMessageBox 就不继承），会藏在浏览器后面等一个看不见的
   回答，主线程就卡死在那儿——之前「Esc 也不管用」就是这么来的。
   所有反馈只走两条路：控制器自己的状态行 + 页面里的 toast。
2. 藏起来的工具窗口在 `finally` 里无条件恢复——中间出什么事都不能让主页回不来。
3. 关浏览器先问 `is_closed()` 再 `close()`，全程吞异常。用户自己把浏览器点掉时连接
   已经断了，还去 close() 会等一个永远不来的回应（这个坑也踩过）。
4. 页面没了由 Playwright 那侧轮询发现 → `gone` 信号 → 主线程自动走【结束】。
5. 主线程 `wait()` 一律带超时；超时就把线程挂到 `_ORPHANS` 里，别让它被回收
   （Qt 的线程对象在还跑着的时候被 GC 掉会直接崩进程，没有提示）。
6. 结束流程幂等：重复点【结束】、浏览器又刚好被关掉，都不该出事。

另外：**恢复主页在等线程退出之前做**。界面先回来、浏览器在后台慢慢关，
用户看到的就是「点一下立刻回来了」，而不是卡住等浏览器。
"""
import queue
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QEventLoop, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from smart_tool import paths
from smart_tool.core.element_picker import (
    ARM_JS, DISARM_JS, HIGHLIGHT_JS, PICKER_JS, TOAST_JS, next_shot_path,
)

# 小窗尺寸、离屏幕边缘的距离（四个按钮并排，得够宽）
WIDTH = 540
MARGIN = 18
# Playwright 那侧的节奏：多久看一次命令、多久喂一次引擎
CMD_WAIT_S = 0.08
POLL_MS = 40
NAV_TIMEOUT_MS = 120_000
# 结束时的等待上限（界面不等它，只用来决定要不要把线程挂起来）
QUIT_WAIT_MS = 3000
# 浏览器被关掉后，让用户看一眼原因再自动结束
GONE_LINGER_MS = 1200
# 校验时绿框停留多久（跟 HIGHLIGHT_JS 里的 2500ms 对应，仅用于文案）
CHECK_HOLD_MS = 2500

#: 超时没退出来的线程挂在这儿：Qt 的线程对象在还跑着的时候被 GC 掉会直接崩进程
_ORPHANS: List["PickerSession"] = []

#: 这些不是「工具窗口」，是 Qt 自己的临时浮层（菜单、提示气泡…），藏窗口时要跳过。
#: 特意放在模块级：名字写错了在 import 那一下就炸，不会被循环里的 try 悄悄吞掉
#: （踩过：写成 Qt.WindowType.Menu —— Qt 里根本没这个成员 —— 结果每轮都抛异常，
#: 窗口一个都没藏住，界面上却看不出任何毛病）。
SKIP_WINDOW_TYPES = (
    Qt.WindowType.Popup,
    Qt.WindowType.ToolTip,
    Qt.WindowType.SplashScreen,
)

PHASE_TITLE = {
    "capturing": "🔍 元素捕获 · 捕获中",
    "paused": "✋ 元素捕获 · 已打断",
    "closing": "■ 元素捕获 · 正在结束",
}

STYLE = """
#card { background: #1f2937; border: 1px solid #3f4b5b; border-radius: 10px; }
#title { color: #e5e7eb; font-size: 13px; font-weight: bold; }
#detail { color: #9ca3af; font-size: 11px; }
#element { color: #fbbf24; font-size: 12px; }
#hint { color: #9ca3af; font-size: 11px; }
#hintbad { color: #fbbf24; font-size: 11px; }
#hintok { color: #4ade80; font-size: 11px; }
#rule { background: #374151; }
QLineEdit {
    background: #111827; color: #e5e7eb; border: 1px solid #4b5563;
    border-radius: 6px; padding: 5px 8px;
    font: 12px Consolas, Menlo, monospace;
}
QLineEdit:focus { border: 1px solid #f08a24; }
QPushButton {
    background: #374151; color: #e5e7eb; border: none;
    border-radius: 6px; padding: 6px 12px; font-size: 12px;
}
QPushButton:hover { background: #4b5563; }
QPushButton:disabled { background: #2b3442; color: #6b7280; }
QPushButton#toggle { background: #b45309; }
QPushButton#toggle:hover { background: #d97706; }
QPushButton#save { background: #1d4ed8; }
QPushButton#save:hover { background: #2563eb; }
QPushButton#save:disabled { background: #2b3442; color: #6b7280; }
QPushButton#finish { background: #7f1d1d; }
QPushButton#finish:hover { background: #991b1b; }
"""


class PickerController(QWidget):
    """右下角的捕获控制器。三种状态：捕获中 / 已打断 / 正在结束。"""

    interrupt_requested = pyqtSignal()      # 【打断】
    resume_requested = pyqtSignal()         # 【恢复】
    verify_requested = pyqtSignal(str)      # 【校验元素】(XPath)
    save_requested = pyqtSignal()           # 【保存元素】：结束捕获 + 存进元素定位
    finish_requested = pyqtSignal()         # 【结束】：只结束，不存

    def __init__(self):
        super().__init__(None)
        self.setWindowTitle("元素捕获")
        # 置顶 + 无边框 + Tool：跟运行小窗一样，不占任务栏、不挡视线。
        # 注意**不加** WindowDoesNotAcceptFocus —— 这里有个 XPath 输入框要打字。
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self._phase = "capturing"
        self._has_element = False       # 抓到过元素才让点【保存元素】
        self._drag = None
        self._init_ui()
        self.set_phase("capturing")

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        self.title_label = QLabel()
        self.title_label.setObjectName("title")
        root.addWidget(self.title_label)

        self.detail_label = QLabel(
            "在页面上点一下＝捕获这个元素（可以连着点，最后一个算数）。\n"
            "想自己翻页／登录就先点【打断】，弄完点【恢复】。"
        )
        self.detail_label.setObjectName("detail")
        self.detail_label.setWordWrap(True)
        root.addWidget(self.detail_label)

        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        root.addWidget(rule)

        self.element_label = QLabel("（还没抓到元素）")
        self.element_label.setObjectName("element")
        self.element_label.setWordWrap(True)
        root.addWidget(self.element_label)

        self.xpath_edit = QLineEdit()
        self.xpath_edit.setPlaceholderText("定位路径（XPath）—— 可以手改，改完点【校验元素】")
        self.xpath_edit.returnPressed.connect(self._on_verify)
        root.addWidget(self.xpath_edit)

        self.hint_label = QLabel("在浏览器里点一下目标元素吧。")
        self.hint_label.setObjectName("hint")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_toggle = QPushButton("打断")
        self.btn_toggle.setObjectName("toggle")
        self.btn_toggle.setToolTip(
            "打断捕获状态：把页面还给你，随便翻页／登录／展开菜单。\n"
            "弄完再点【恢复】，就又能抓元素了。")
        self.btn_toggle.clicked.connect(self._on_toggle)
        row.addWidget(self.btn_toggle)
        self.btn_verify = QPushButton("校验元素")
        self.btn_verify.setToolTip(
            "拿上面那个 XPath 在当前页面里找一下：找到就把元素滚到眼前、画个绿框，\n"
            "并告诉你「命中几个」。命中多于 1 个说明写法不够准，改完再校验。")
        self.btn_verify.clicked.connect(self._on_verify)
        row.addWidget(self.btn_verify)
        self.btn_save = QPushButton("保存元素")
        self.btn_save.setObjectName("save")
        self.btn_save.setEnabled(False)
        self.btn_save.setToolTip(
            "结束捕获，并把这次抓到的元素存进项目的「元素定位」。\n"
            "存了以后，任何「定位路径」里写 {{名字}} 就能复用它。\n"
            "同一个元素（XPath 一样）再存一次＝覆盖原来那条，不会越存越多。")
        self.btn_save.clicked.connect(self.save_requested.emit)
        row.addWidget(self.btn_save)
        self.btn_finish = QPushButton("结束")
        self.btn_finish.setObjectName("finish")
        self.btn_finish.setToolTip(
            "结束捕获：关掉浏览器、把主页调回来。\n"
            "已经抓到的元素会写进当前这一步；要存进「元素定位」请用【保存元素】。")
        self.btn_finish.clicked.connect(self.finish_requested.emit)
        row.addWidget(self.btn_finish)
        root.addLayout(row)

        self.setFixedWidth(WIDTH)

    # ------------------------------
    # 状态
    # ------------------------------
    def set_phase(self, phase: str):
        """捕获中 / paused（已打断）/ closing（正在结束）。"""
        self._phase = phase
        self.title_label.setText(PHASE_TITLE.get(phase, phase))
        paused = phase == "paused"
        self.btn_toggle.setText("恢复" if paused else "打断")
        alive = phase != "closing"
        self.btn_toggle.setEnabled(alive)
        self.btn_verify.setEnabled(alive)
        # 还没抓到元素就没什么可存的
        self.btn_save.setEnabled(alive and self._has_element)
        self.detail_label.setText(
            "已打断：现在页面随便你操作（翻页、登录、展开菜单都行）。\n"
            "弄完点【恢复】继续抓元素。"
            if paused else
            "在页面上点一下＝捕获这个元素（可以连着点，最后一个算数）。\n"
            "想自己翻页／登录就先点【打断】，弄完点【恢复】。"
        )

    def phase(self) -> str:
        return self._phase

    def set_element(self, data: dict):
        """把刚抓到的元素填进来（XPath 可改，改完点【校验元素】）。"""
        self._has_element = True
        if self._phase != "closing":
            self.btn_save.setEnabled(True)
        self.element_label.setText(f"已捕获：{data.get('desc') or '元素'}")
        self.xpath_edit.setText(data.get("xpath") or "")
        count = data.get("count", -1)
        if count == 1:
            self.set_hint("命中 1 个，这个定位很稳。接着点页面可以再抓一个。", ok=True)
        elif count > 1:
            self.set_hint(f"命中 {count} 个——这个写法不够准，"
                          "建议改一下 XPath 再点【校验元素】。", ok=False)
        else:
            self.set_hint("这个元素没生成出 XPath，换个元素再点点看。", ok=False)

    def set_hint(self, text: str, ok: bool = True):
        """提示写在自己身上 —— 绝不弹框（见文件头的防卡死铁律）。"""
        self.hint_label.setObjectName("hintok" if ok else "hintbad")
        self.hint_label.setText(text or "")
        # 换了 objectName 要重新套一遍样式表，否则颜色不会变
        self.hint_label.style().unpolish(self.hint_label)
        self.hint_label.style().polish(self.hint_label)

    def xpath(self) -> str:
        return self.xpath_edit.text().strip()

    # ------------------------------
    # 按钮
    # ------------------------------
    def _on_toggle(self):
        if self._phase == "capturing":
            self.set_phase("paused")
            self.interrupt_requested.emit()
        elif self._phase == "paused":
            self.set_phase("capturing")
            self.resume_requested.emit()

    def _on_verify(self):
        if self._phase == "closing":
            return
        self.verify_requested.emit(self.xpath())

    # ------------------------------
    # 位置 / 拖动
    # ------------------------------
    def move_to_corner(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.right() - self.width() - MARGIN,
                  area.bottom() - self.height() - MARGIN)

    def mousePressEvent(self, event):
        """整张卡片可以拖着走 —— 它可能正好盖住你要点的地方。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.globalPosition().toPoint()
                          - self.frameGeometry().topLeft())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None


class PickerSession(QThread):
    """后台线程：开浏览器 → 注入脚本 → 听命令（打断/恢复/校验/结束）。

    Playwright 的同步 API 不能跟 Qt 事件循环挤在一个线程，所以整个浏览器都在这条
    线程上；主线程只通过命令队列使唤它、通过信号收结果。
    """

    opened = pyqtSignal(str)            # 页面已打开（当前 URL）
    picked = pyqtSignal(dict)           # 抓到元素
    verified = pyqtSignal(bool, str)    # 校验结果（ok, 说明）
    gone = pyqtSignal(str)              # 页面没了 / 浏览器被关掉
    log = pyqtSignal(str)               # 过程中的一句话

    def __init__(self, img_dir: Path, parent=None):
        super().__init__(parent)
        self._img_dir = Path(img_dir)
        self._cmd: "queue.Queue" = queue.Queue()
        self._picks: "queue.Queue" = queue.Queue()
        self._quit = False
        self._gone_sent = False
        self._page = None
        self._pw = None
        self._seq = 0
        self._want_armed = False

    # ------------------------------
    # 主线程调用（都只是往队列里放一条命令）
    # ------------------------------
    def start_page(self, url: str):
        self._cmd.put(("start", url or ""))

    def arm(self):
        self._cmd.put(("arm", None))

    def disarm(self):
        self._cmd.put(("disarm", None))

    def verify(self, xpath: str):
        self._cmd.put(("verify", xpath or ""))

    def finish(self):
        self._cmd.put(("finish", None))

    # ------------------------------
    # 线程内
    # ------------------------------
    def run(self):
        from playwright.sync_api import sync_playwright

        from smart_tool.core import browser_setup

        browser_setup.ensure_env()      # 内核可能在「程序目录旁的浏览器文件夹」里
        try:
            with sync_playwright() as p:
                self._pw = p
                while not self._quit:
                    self._tick()
        except Exception as e:
            self._page_gone(f"捕获会话出错：{_first_line(e)}")
        finally:
            self._close_browser_quietly()

    def _tick(self):
        """一轮：先看有没有命令，再喂一下 Playwright（页面回调靠它派发）。

        命令里出的异常一律就地吞掉：线程要是死在这儿，主线程那边等不到 gone，
        就真的卡死了。宁可把错报出去，也不能让线程没了。
        """
        try:
            name, arg = self._cmd.get(timeout=CMD_WAIT_S)
        except queue.Empty:
            name, arg = "", None
        try:
            if name == "start":
                self._do_start(arg)
            elif name == "arm":
                self._do_arm()
            elif name == "disarm":
                self._do_disarm()
            elif name == "verify":
                self._do_verify(arg)
            elif name == "finish":
                self._quit = True
        except Exception as e:
            self.log.emit(f"操作没成功：{_first_line(e)}")
        self._pump()

    def _pump(self):
        """让 Playwright 转起来，顺便看页面还在不在。"""
        page = self._page
        if page is None:
            return
        try:
            page.wait_for_timeout(POLL_MS)
        except Exception as e:
            self._page_gone(f"页面已关闭（{_first_line(e, 80)}）")
            return
        try:
            if page.is_closed():
                self._page_gone("浏览器窗口被关掉了。")
                return
        except Exception:
            self._page_gone("浏览器窗口被关掉了。")
            return
        self._drain()

    def _page_gone(self, reason: str):
        """页面没了：清干净、报一次（只报一次，别刷屏）。"""
        if self._gone_sent:
            return
        self._gone_sent = True
        page, self._page = self._page, None
        _close_page_quietly(page)
        self.gone.emit(reason)

    def _alive(self):
        page = self._page
        try:
            if page is not None and not page.is_closed():
                return page
        except Exception:
            pass
        self._page_gone("浏览器窗口已经被关掉了。")
        return None

    # ------------------------------
    # 开页面
    # ------------------------------
    def _do_start(self, url: str):
        if not url:
            self._page_gone(
                "这一步没有网址，也没有已经开着的浏览器。\n"
                "先在它前面放一个【打开网页】节点并填上网址。")
            return
        self.log.emit(f"正在打开 {url} …")
        browser = self._pw.chromium.launch(headless=False)
        context = browser.new_context()
        context.add_init_script(PICKER_JS)
        page = context.new_page()
        page.expose_function("__trae_pick", self._on_pick)
        self._page = page
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            self._page_gone(f"打开网址失败：{_first_line(e, 150)}")
            return
        self._arm_frames(page)
        self.opened.emit(_safe_url(page) or url)

    # ------------------------------
    # 打断 / 恢复
    # ------------------------------
    def _do_arm(self):
        self._want_armed = True
        page = self._alive()
        if page is None:
            return
        self._arm_frames(page)
        self.log.emit("已恢复捕获：划过元素画橙框，点一下＝捕获。")

    def _do_disarm(self):
        self._want_armed = False
        page = self._alive()
        if page is None:
            return
        self._each_frame(page, DISARM_JS)
        self.log.emit("已打断：页面现在随便你操作，弄完点【恢复】继续抓。")

    def _arm_frames(self, page):
        """每个 frame 都装一遍（iframe 里的元素也能抓），再把开关拨到「捕获中」。

        PICKER_JS 自己有 `__traePickerReady` 守卫，重复注入是空操作；
        真正决定干不干活的是 `__traePickerOn`（见 element_picker.py 的说明）。
        """
        self._want_armed = True
        for frame in list(page.frames):
            try:
                frame.evaluate(PICKER_JS)
                frame.evaluate(ARM_JS)
            except Exception:
                continue            # 跨域 iframe 注不进去，正常
        if self._page is page:
            try:
                page.bring_to_front()
            except Exception:
                pass

    @staticmethod
    def _each_frame(page, expression, arg=None):
        for frame in list(page.frames):
            try:
                frame.evaluate(expression, arg)
            except Exception:
                continue

    # ------------------------------
    # 校验
    # ------------------------------
    def _do_verify(self, xpath: str):
        xpath = (xpath or "").strip()
        if not xpath:
            self.verified.emit(False, "定位路径是空的 —— 先在输入框里写上（或抓一个元素）再校验。")
            return
        page = self._alive()
        if page is None:
            self.verified.emit(False, "浏览器没开着，校验不了。")
            return
        found, bad_syntax = 0, False
        for frame in list(page.frames):
            try:
                n = frame.evaluate(HIGHLIGHT_JS, xpath)
            except Exception:
                continue
            if n == -1:
                bad_syntax = True
            elif isinstance(n, int) and n > 0:
                found = n
                break
        if found == 0 and bad_syntax:
            ok, message = False, "XPath 写错了（语法不对），检查一下括号和引号。"
        elif found == 0:
            ok, message = False, (
                f"当前页面上找不到这个元素：{xpath[:90]}\n"
                "是不是页面已经跳走了？先翻回这一步所在的页面再校验。")
        elif found == 1:
            ok, message = True, "定位成功：页面上已经用绿框圈出来了（命中 1 个，很稳）。"
        else:
            ok, message = False, (
                f"定位到了，但命中 {found} 个 —— 写法不够准，"
                "建议加条件缩到 1 个（绿框圈的是第一个）。")
        self._toast(page, ok, message)
        self.verified.emit(ok, message)

    def _toast(self, page, ok: bool, text: str):
        """把结果贴在页面顶端：动作和结果都在浏览器里，反馈也该在那儿。

        贴不上就算了（控制器那一行还留着一份），不能因为这个出错。
        """
        payload = {"ok": bool(ok), "text": text}
        try:
            for frame in list(page.frames):
                try:
                    if frame.evaluate(TOAST_JS, payload):
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # ------------------------------
    # 抓到的元素
    # ------------------------------
    def _on_pick(self, payload):
        """页面里点中元素时由 Playwright 回调（跑在本线程）。"""
        self._picks.put(dict(payload or {}))

    def _drain(self):
        while True:
            try:
                payload = self._picks.get_nowait()
            except queue.Empty:
                return
            self._seq += 1
            payload["image"] = self._save_shot(payload)
            self.picked.emit(payload)
            # 抓完脚本会自收手；这里按当前意愿再武装一次，方便连着抓。
            # 用 _want_armed 而不是无条件恢复：万一用户刚好按了【打断】，
            # 不能被他这一下给盖回去。
            if self._want_armed:
                page = self._page
                try:
                    if page is not None and not page.is_closed():
                        self._arm_frames(page)
                except Exception:
                    pass

    def _save_shot(self, payload: dict) -> str:
        """把刚捕获的元素截下来，返回相对项目的路径（img/xxx.png）。

        用 locator.screenshot()：裁的就是元素精确边框，不受滚动/缩放影响。
        iframe 里的元素（主 page 找不到这个 XPath）跳过截图，交给上层提示。
        """
        xpath = (payload.get("xpath") or "").strip()
        page = self._page
        if not xpath or not payload.get("top") or page is None:
            return ""
        try:
            loc = page.locator(f"xpath={xpath}")
            if loc.count() < 1:
                return ""
            self._img_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = next_shot_path(self._img_dir, f"{stamp}_{self._seq}")
            loc.first.screenshot(path=str(path))
            return f"img/{path.name}"
        except Exception as e:
            self.log.emit(f"元素截图失败（XPath 仍然可用）：{_first_line(e, 100)}")
            return ""

    # ------------------------------
    # 收尾
    # ------------------------------
    def _close_browser_quietly(self):
        page, self._page = self._page, None
        _close_page_quietly(page)


# ------------------------------
# 模块级
# ------------------------------
class _SessionGlue(QObject):
    """会话信号 → 控制器之间的收口。**必须住在主线程**。

    为什么不在 capture_element 里直接写几个闭包接信号：信号落到「主线程里的 QObject
    方法」上，Qt 才会用排队连接把它送到主线程；接到普通函数/lambda 时，PyQt 有可能
    按直接连接处理，handler 就跑到 Playwright 那条线程上去了 —— 在那边碰 Qt 控件
    是要出事的（随机崩）。所以这里宁可多一个类。
    """

    def __init__(self, controller: PickerController, project_dir: Path,
                 finish_once):
        super().__init__(controller)        # 挂在控制器底下，生命周期跟着它
        self.controller = controller
        self.project_dir = Path(project_dir)
        self.data: Optional[dict] = None
        self.shots: List[Path] = []
        self._finish = finish_once

    def bind(self, session: PickerSession):
        session.opened.connect(self.on_opened)
        session.picked.connect(self.on_picked)
        session.verified.connect(self.on_verified)
        session.gone.connect(self.on_gone)
        session.log.connect(self.on_log)

    def on_opened(self, current: str):
        self.controller.set_phase("capturing")
        self.controller.set_hint(
            f"页面已打开：{current}\n在页面上点一下目标元素吧。", ok=True)

    def on_picked(self, payload: dict):
        xpath = (payload.get("xpath") or "").strip()
        count = payload.get("count", -1)
        self.data = {
            "xpath": xpath if count >= 0 else "",
            "image": payload.get("image") or "",
            "count": count,
            "desc": payload.get("desc", ""),
        }
        if self.data["image"]:
            self.shots.append(self.project_dir / self.data["image"])
        self.controller.set_element(self.data)

    def on_verified(self, ok: bool, message: str):
        self.controller.set_hint(message, ok=ok)

    def on_log(self, text: str):
        self.controller.set_hint(text, ok=True)

    def on_gone(self, reason: str):
        """浏览器被关掉了 —— 自动走【结束】，把主页还回来。

        留一小段时间让用户看清原因：小窗一闪而过的话，人不知道怎么就结束了。
        """
        trace_windows(f"浏览器没了：{reason}")
        self.controller.set_phase("closing")
        self.controller.set_hint(reason, ok=False)
        QTimer.singleShot(GONE_LINGER_MS, self._finish)


def capture_element(url: str, project_dir) -> Optional[dict]:
    """跑一次网页元素捕获：让开工具窗口 → 弹控制器 → 结束/浏览器被关就收尾。

    返回值：抓到了就是 `{"xpath", "image", "count", "desc", "want_save"}`，
    没抓到返回 None。`want_save` 表示用户点的是【保存元素】而不是【结束】——
    调用方据此决定要不要把它存进项目的「元素定位」。
    """
    project_dir = Path(project_dir)
    controller = PickerController()
    session = PickerSession(project_dir / "img")
    loop = QEventLoop()
    state = {"done": False, "want_save": False}

    def finish_once():
        """结束流程：幂等 —— 重复点【结束】、浏览器又刚好被关，都不该出事。"""
        if state["done"]:
            return
        state["done"] = True
        loop.quit()

    def finish_and_save():
        """【保存元素】：先记下「要存」，再走同一套结束流程。

        存的动作**不在这里做** —— 要等捕获彻底结束（浏览器关了、控制器收了、
        主页和编辑框都回来了）之后，由调用方去存。顺序很重要：
        保存要弹输入框（应用级模态），在界面还没恢复干净的时候弹，很容易
        把窗口状态搅乱（就是之前「点完确定界面就死了」那条路）。
        """
        state["want_save"] = True
        finish_once()

    glue = _SessionGlue(controller, project_dir, finish_once)
    glue.bind(session)

    # 控制器 → 会话：只是往线程安全的队列里放一条命令，不会阻塞界面
    controller.interrupt_requested.connect(session.disarm)
    controller.resume_requested.connect(session.arm)
    controller.verify_requested.connect(session.verify)
    controller.finish_requested.connect(finish_once)
    controller.save_requested.connect(finish_and_save)

    away, active = [], None
    trace_windows("capture_element 进入")
    try:
        away, active = _hide_tool_windows(controller)
        trace_windows(f"让开工具窗口（{len(away)} 个）")
        controller.move_to_corner()
        controller.show()
        controller.raise_()
        session.start()
        session.start_page(url)
        loop.exec()
    finally:
        # 结束的顺序是定死的，也是刻意的：
        #   ① 先关浏览器 —— 它是另一个进程，还占着前台；先把它送走，
        #      后面恢复窗口时才不会有别的进程跟着抢前台。
        #   ② 再关控制器 —— 一个永远置顶的小窗，得赶在主页回来之前收掉。
        #   ③ 最后才把主页／编辑框放回来，并确认焦点落在对的那一层。
        try:
            controller.set_phase("closing")
            controller.set_hint("正在关闭浏览器…", ok=True)
        except Exception:
            pass
        _shutdown_session(session)
        trace_windows("关完浏览器")
        try:
            controller.hide()
        finally:
            controller.deleteLater()
        trace_windows("关完控制器")
        _restore_windows(away, active)
        trace_windows("放回工具窗口")
        # 关完浏览器再确认一次 —— 理由见 _reactivate 的说明
        _reactivate(away, active)
        trace_windows("reactivate 之后")

    _drop_unused_shots(glue.shots, keep_image=(glue.data or {}).get("image"))
    data = glue.data
    if data is not None:
        data["want_save"] = bool(state["want_save"])
    trace_windows("capture_element 返回前")
    return data


def _hide_tool_windows(controller):
    """把工具自己的窗口都让开（主窗口、步骤编辑器…），屏幕上只留控制器。

    让开有两种做法，按窗口是不是「模态对话框」分：

    · 普通窗口 → `hide()`。干净，任务栏上也不留痕。
    · **模态对话框 → `showMinimized()`，绝不能用 hide()。**
      这是踩出来的大坑：`hide()` 一个正在 `exec()` 的对话框，会把它的 `exec()`
      直接结束掉（Qt 就这么设计的）。而捕获偏偏是从这个 `exec()` 里点出来的 ——
      于是捕获跑完、控制权一回到 `exec()`，它立刻返回；调用方以为「项目管理
      已经关掉了」，把那个对话框当垃圾回收掉。可它是当时的活动窗口，一删就留下
      「没有活动窗口」的残局：Qt 这边查什么都是正常的（没模态、主窗口可见可用），
      但 Windows 那边被销毁的前台窗口一去不回 —— 点主界面就是不理你，还「咚」。
      **症状看着像卡死，其实主线程一直活得好好的**（定时器照常触发）。

    为什么按「可见的顶层窗口」收，而不是 parent.window()：从步骤编辑器里点捕获时，
    parent.window() 拿到的是编辑器自己，结果只让开了编辑器、主窗口还杵在浏览器旁边
    （这个坑也踩过）。

    返回 (让开的窗口列表, 原来在最前面的那个)。列表里每项是 `(窗口, 怎么让开的)`，
    `怎么让开的` 用来决定放回来时该调哪个方法：`"hide"` / `"min"` / `"max"`。
    记住「原来最前面的是谁」是为了结束后把焦点还到用户离开时的那一层。
    """
    try:
        active = QApplication.activeWindow()
    except Exception:
        active = None
    away: List[tuple] = []
    for w in QApplication.topLevelWidgets():
        if w is controller or not w.isWindow() or not w.isVisible():
            continue
        if w.windowType() in SKIP_WINDOW_TYPES:
            continue
        try:
            if w.windowModality() != Qt.WindowModality.NonModal:
                was_max = bool(w.windowState() & Qt.WindowState.WindowMaximized)
                w.showMinimized()
                away.append((w, "max" if was_max else "min"))
            else:
                w.hide()
                away.append((w, "hide"))
        except Exception:
            continue                    # 已经销毁的窗口，跳过就好
    return away, active


def _put_back(w, how: str):
    """按当初让开的方式把窗口放回来。"""
    if how == "max":
        w.showMaximized()
    elif how == "min":
        w.showNormal()
    else:
        w.show()


def _restore_windows(away: List[tuple], active):
    """把刚才让开的窗口放回来，并把焦点还给原来那一层。"""
    for w, how in away:
        try:
            _put_back(w, how)
        except Exception:
            continue
    # 再核一遍：让开过、又该回来的窗口必须真的回来。真丢一个，用户看到的就是
    # 「界面全点不动、鼠标拖不动、点一下还咚」——这里补一次，别让它悄悄留在那儿。
    for w, how in away:
        try:
            if not w.isVisible():
                _put_back(w, how)
        except Exception:
            continue
    # 同一个坑的另一面：还挂着应用级模态、却又是看不见的窗口。它会把整个程序挡住，
    # 所以主动叫回来。看得见的模态是正常的（比如项目管理自己），不去碰。
    try:
        modal = QApplication.activeModalWidget()
        if modal is not None and not modal.isVisible():
            modal.show()
            modal.raise_()
    except Exception:
        pass
    target = active if any(w is active for w, _ in away) else (
        away[-1][0] if away else None)
    if target is None:
        return
    try:
        target.raise_()
        target.activateWindow()
    except Exception:
        pass


def _reactivate(away: List[tuple], active):
    """关完浏览器之后，再确认一次「界面回来了、而且是可用的」。

    为什么要在关完浏览器之后补这一下：浏览器是**另一个进程**，抓元素的时候它正占着
    前台，而我们是先还界面、后关浏览器。Windows／Qt 对「谁是当前活动窗口」的记账，
    在这种跨进程抢前台的情形下容易算乱（踩过）。

    这里做两件事：把该在前的窗口再抬一次；然后检查有没有「看不见的模态」残留，
    有就把界面解开。正常情况下这里什么都不用做。
    """
    target = active if any(w is active for w, _ in away) else (
        away[-1][0] if away else None)
    try:
        if target is not None and target.isVisible():
            target.raise_()
            target.activateWindow()
    except Exception:
        pass
    # 兜底：只要没有「看得见的模态」，我们让开过的窗口就不该是禁用或隐藏的
    try:
        modal = QApplication.activeModalWidget()
    except Exception:
        modal = None
    if modal is not None and modal.isVisible():
        return
    for w, how in away:
        try:
            if not w.isEnabled():
                w.setEnabled(True)
            if not w.isVisible():
                _put_back(w, how)
        except Exception:
            continue


def _shutdown_session(session: PickerSession):
    """让会话线程退出（它会顺手把浏览器关掉）。带超时，绝不死等。"""
    try:
        session.finish()
    except Exception:
        pass
    if session.wait(QUIT_WAIT_MS):
        try:
            session.deleteLater()
        except Exception:
            pass
        return
    # 还没退出来：挂个引用，等它自己结束再摘掉
    _ORPHANS.append(session)
    session.finished.connect(lambda: _forget(session))


def _drop_unused_shots(shots: List[Path], keep_image: str):
    """删掉这次试抓留下的、最后没用上的截图。

    抓一个点一个很容易试好几次，只有最后选中的那张会写进步骤，
    其余的留着只会在 img/ 里堆废图。
    """
    keep = None
    if keep_image:
        name = Path(keep_image).name
        keep = next((p for p in shots if p.name == name), None)
    for p in shots:
        if keep is not None and p == keep:
            continue
        try:
            p.unlink()
        except OSError:
            pass
    shots.clear()


def _close_page_quietly(page):
    """静默收掉这个页面所在的浏览器。

    先问 `is_closed()` 再关：用户直接把浏览器窗口点掉时连接已经断了，这时候还去
    `close()` 有可能卡在那儿等一个永远不会来的回应 —— 整个程序就跟着僵住（这个坑
    踩过）。已经没了就干脆什么都不做，让它随进程一起走。
    """
    if page is None:
        return
    try:
        if page.is_closed():
            return
        page.context.browser.close()
    except Exception:
        pass


def _forget(session: PickerSession):
    try:
        _ORPHANS.remove(session)
    except ValueError:
        pass


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _first_line(exc, limit: int = 120) -> str:
    """异常信息只留第一行（Playwright 的报错经常是几十行的调用栈）。"""
    text = str(exc).strip().splitlines()
    return (text[0] if text else exc.__class__.__name__)[:limit]


# ------------------------------
# 现场跟踪：专门用来抓「抓完元素之后界面点不动」这个毛病
# ------------------------------
# 为什么要有这一块：这种毛病只在真实 Windows 上出现（离屏测试连窗口的
# 启用/禁用都不模拟，怎么搭都复现不出来）。所以别猜了，把现场记下来：
# 谁可见、谁被禁用、模态栈顶上是谁、焦点在谁身上。
# 排查完之后这一整块可以删掉（日志本身是用户数据目录里的 picker_trace.log）。
TRACE_MAX_BYTES = 200_000


def _widget_desc(w) -> str:
    """一行说清一个窗口/控件当前的状态。"""
    if w is None:
        return "None"
    try:
        title = w.windowTitle()
    except Exception:
        title = ""
    try:
        return (f"{type(w).__name__}({title!r} 可见={w.isVisible()}"
                f" 可用={w.isEnabled()} 模态={w.windowModality().name}"
                f" 活跃={w.isActiveWindow()})")
    except Exception:
        return f"{type(w).__name__}(状态读不出来)"


def trace_windows(tag: str):
    """把当前所有窗口的状态记进 picker_trace.log（用户数据目录里）。

    记日志本身绝不能把程序带崩，所以整段都兜住异常。
    """
    try:
        path = paths.trace_log()
        if path.is_file() and path.stat().st_size > TRACE_MAX_BYTES:
            path.write_text("", encoding="utf-8")
        # tag 一律压成一行（有些原因说明是带换行的，不然日志会散掉）
        lines = [f"[{datetime.now():%H:%M:%S}] " + " ".join(str(tag).split())[:200]]
        lines.append("    模态栈顶=" + _widget_desc(QApplication.activeModalWidget())
                     + "  活动窗口=" + _widget_desc(QApplication.activeWindow())
                     + "  焦点=" + _widget_desc(QApplication.focusWidget()))
        for w in QApplication.topLevelWidgets():
            lines.append("    顶层 " + _widget_desc(w))
        for w in QApplication.allWidgets():
            try:
                if (isinstance(w, QDialog)
                        and w.windowModality() != Qt.WindowModality.NonModal):
                    lines.append("    模态 " + _widget_desc(w))
            except Exception:
                continue
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def trace_later(tag: str, delays_ms=(300, 1500, 4000)):
    """过一会儿再记一次现场 —— 这一步是**决定性的**。

    如果界面真的卡死（主线程被堵住），这些定时器根本不会触发：日志里看不到
    这几行，就说明问题不在「窗口状态/模态」，而是主线程被卡在某个地方。
    反过来，如果几行都记上了、而界面依然是死的，那就是窗口启用/模态的问题。
    """
    for delay in delays_ms:
        QTimer.singleShot(delay, lambda d=delay: trace_windows(f"{tag} +{d}ms"))


def release_stuck_modal():
    """兜底：如果还压着一个「看不见的模态」，把界面救回来。

    这是给「抓完元素界面点不动、拖不动、点一下还咚」留的最后一道闸 ——
    那种症状就是 Windows 对被禁用窗口的反应，而被禁用的原因是「有个应用级模态
    压着」，偏偏那个模态窗口你看不见。

    **要在保存框之类的模态都关完之后再叫**：早叫没用，那会儿挡人的东西还没出现。
    正常情况下这里什么都不做（有看得见的模态是正常的）。
    """
    try:
        modal = QApplication.activeModalWidget()
    except Exception:
        return
    if modal is None or modal.isVisible():
        return
    trace_windows(f"发现隐形模态（{type(modal).__name__}），解开它")
    try:
        modal.setWindowModality(Qt.WindowModality.NonModal)
        modal.hide()
    except Exception:
        pass
    for w in QApplication.topLevelWidgets():
        try:
            if w is modal or not w.isWindow():
                continue
            if w.windowType() not in (Qt.WindowType.Window, Qt.WindowType.Dialog):
                continue
            if not w.isEnabled():
                w.setEnabled(True)
            if not w.isVisible():
                w.show()
        except Exception:
            continue
    trace_windows("解开之后")

