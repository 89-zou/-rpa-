# -*- coding: utf-8 -*-
"""常驻的元素捕获会话：浏览器开着不放，一次捕获到下一次之间复用。

为什么要"常驻"：以前的捕获器是「开浏览器 → 抓一个 → 关浏览器」，于是每抓一步都要
重开一次窗口、重新登录、重新点到那个页面。现在浏览器由这个会话一直端着，
下次捕获直接接着用你当前停留的页面，不用再走一遍。

跑在独立线程里：Playwright 的同步 API 不能和 Qt 的事件循环挤在一个线程。
主线程只往命令队列里塞东西，所有 Playwright 调用都在这个线程里做。

页面那边的配合（见 core/element_picker.py 的 PICKER_JS）：
- 平时页面照常能点能滚；
- **按住 Ctrl** 才进入捕获待命（画橙框 + 显示命中几个），这时点一下才抓；
- 松开 Ctrl 或者按 Esc 就退出待命（Esc 还会回一条 cancel 让上层收工）。
"""
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from smart_tool.core.element_picker import (
    HIGHLIGHT_JS, PICKER_JS, TOAST_JS, next_shot_path,
)
from smart_tool.core.step_executor import StepExecutor

#: 打开网址的超时。这是**交互式**捕获，不是批量跑流程：站点真慢到这份上，
#: 与其让你对着一个「正在打开…」干等两分钟，不如早点说清、让你重试。
#: （跑流程那边还是 120 秒，见 step_executor.NAV_TIMEOUT_DEFAULT_S）
NAV_TIMEOUT_MS = 45_000
#: 主循环里「等命令」和「喂一下 Playwright」的节奏
CMD_WAIT_S = 0.05
#: 试运行时点/填的超时（毫秒）——比跑流程时短，就是看一眼效果
TRIAL_TIMEOUT_MS = 8_000
#: 试运行时先亮绿框多久再动手（让人看清是哪个元素）
TRIAL_PREVIEW_MS = 450
#: 「回放前面的节点」最多跑多久（秒）。到点就让执行器停下来，把页面交给捕获 ——
#: 交互式场景里干等没意义，宁可停下来让人看看到哪一步了。
REPLAY_MAX_S = 120


def _first_line(err: Exception, limit: int = 120) -> str:
    text = str(err).strip().splitlines()
    return (text[0] if text else type(err).__name__)[:limit]


class PickerSession(QThread):
    """一个进程里只用一个（见下面的 shared()）。"""

    opened = pyqtSignal(str)              # 页面已就绪（当前 URL）
    reused = pyqtSignal(str)              # 复用了一个已经开着的浏览器（当前 URL）
    picked = pyqtSignal(dict)             # 捕获到一个元素
    canceled = pyqtSignal(str)            # 用户取消了这次捕获（原因）
    gone = pyqtSignal(str)                # 浏览器 / 页面没了（原因）
    failed = pyqtSignal(str)              # 起浏览器失败之类的硬错误
    log = pyqtSignal(str)
    tried = pyqtSignal(bool, str)         # 试运行结果（成功?, 给人看的说明）
    replayed = pyqtSignal(bool, str)      # 回放前面的节点结果（成功?, 说明）
    aborted = pyqtSignal(str)             # 页面里按了 Esc：要求强制退出

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cmd: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._picks: "queue.Queue[dict]" = queue.Queue()
        self._quit = False
        self._page = None
        self._seq = 0
        self._shot_dir: Optional[Path] = None
        self._lock = threading.Lock()
        self._busy = False                # 正在等一次捕获
        self._was_reused = False          # 这一轮是复用了已有的浏览器？
        self._replay_exec = None          # 正在跑的回放（跳过时要用）

    # ------------------------------
    # 主线程调用：只往队列里放命令，绝不碰 Playwright
    # ------------------------------
    def capture(self, url: str, img_dir: Path):
        """开始一次捕获：没有浏览器就开（并打开 url），有就直接复用。

        一进来就把捕获脚本装上（每个 frame），**不等回放**：这样按住 Ctrl 能抓、
        按 Esc 能退 —— 上一版的错就在这儿，回放前不装脚本，用户一旦不回放，
        页面里就既抓不了也退不出来，看着就是卡死。
        """
        self._shot_dir = Path(img_dir)
        self._cmd.put(("capture", url or ""))

    def force_abort(self):
        """**强制打断**：关掉浏览器、丢掉还没执行的命令，让一切回到干净状态。

        主线程直接调用（不走队列）：正卡在 `goto` / `reload` 里时，队列要等它
        出来才轮得到，那就不叫「强制」了。这里先把该清的标记清掉、该丢的命令丢掉，
        浏览器由会话线程腾出手来立刻关（关不掉也不影响界面已经恢复）。
        """
        self._drop_pending()
        ex = self._replay_exec
        if ex is not None:
            try:
                ex._stop = True
            except Exception:
                pass
        with self._lock:
            self._busy = False
        self._picks = queue.Queue()
        self._cmd.put(("abort", None))

    def _drop_pending(self):
        """把还没执行的命令全丢掉（强制停止时，排队里的 reload/goto 不该再跑）。"""
        keep = []
        while True:
            try:
                keep.append(self._cmd.get_nowait())
            except queue.Empty:
                break
        # 只留「退出程序」那条，其余一律作废
        for item in keep:
            if item and item[0] == "quit":
                self._cmd.put(item)

    def replay(self, spec: Dict[str, Any]):
        """把当前这一步**之前**的节点在这个浏览器里跑一遍，然后停在那个页面上。

        spec = {"steps": [Step...], "variables": {...}, "project_dir": Path}
        """
        self._cmd.put(("replay", dict(spec or {})))

    def skip_replay(self):
        """【跳过回放】：让正在跑的回放停下来。

        **故意不走命令队列**：回放期间会话线程正卡在执行器里，队列要等它跑完
        才轮到 —— 那就白点了。这里直接给执行器置停止标记（它每步之间会看这个
        bool，跨线程写一个是安全的）。
        """
        ex = self._replay_exec
        if ex is None:
            return
        try:
            ex._stop = True
        except Exception:
            pass

    def reload_page(self):
        """【重新加载】当前页面。"""
        self._cmd.put(("reload", None))

    def open_entry(self, url: str):
        """【回到入口页】：打开流程里第一个「打开网页」的地址。"""
        self._cmd.put(("entry", url or ""))

    def trial(self, spec: Dict[str, Any]):
        """试运行：在当前页面里对捕获到的元素跑一次动作。"""
        self._cmd.put(("trial", dict(spec or {})))

    def shutdown(self):
        """收工：关掉浏览器、结束线程（退出程序时用）。"""
        self._cmd.put(("quit", None))
        self.wait(6000)

    # ------------------------------
    # 线程内
    # ------------------------------
    def _on_pick(self, payload):
        """页面里按 Ctrl 点中元素时由 Playwright 回调（跑在本线程）。"""
        self._picks.put(dict(payload or {}))

    def run(self):
        from playwright.sync_api import sync_playwright

        from smart_tool.core import browser_setup

        browser_setup.ensure_env()        # 内核可能在「程序目录旁的浏览器文件夹」里
        try:
            with sync_playwright() as p:
                self._pw = p
                while not self._quit:
                    self._tick()
        except Exception as e:
            self.failed.emit(f"捕获会话出错：{_first_line(e)}")
        finally:
            self._close_browser_quietly()

    def _tick(self):
        """一轮：先看有没有命令，再喂一下 Playwright（它要跑才能收到页面回调）。"""
        try:
            name, arg = self._cmd.get(timeout=CMD_WAIT_S)
        except queue.Empty:
            name, arg = "", None
        if name == "quit":
            self._quit = True
        elif name == "capture":
            self._do_capture(arg)
        elif name == "replay":
            self._do_replay(arg)
        elif name == "reload":
            self._do_navigate("reload")
        elif name == "entry":
            self._do_navigate("entry", arg)
        elif name == "abort":
            self._do_abort()
        elif name == "trial":
            self._do_trial(arg)
        self._pump()

    def _pump(self):
        """让 Playwright 转起来（页面回调靠它派发），顺便看看页面还在不在。"""
        page = self._page
        if page is None:
            return
        try:
            page.wait_for_timeout(40)
        except Exception:
            if self._page is not None:
                self.go_end("浏览器窗口被关掉了。")
            return
        try:
            if page.is_closed():
                self.go_end("浏览器窗口被关掉了。")
                return
        except Exception:
            self.go_end("浏览器窗口被关掉了。")
            return
        self._drain()

    def go_end(self, reason: str):
        """页面没了：清干净再看有没有人在等，等着的话告诉他一声。"""
        self._close_browser_quietly()
        with self._lock:
            waiting = self._busy
            self._busy = False
        if waiting:
            self.gone.emit(reason)

    # ------------------------------
    # 捕获
    # ------------------------------
    def _do_capture(self, url: str):
        page = self._ensure_page(url)
        if page is None:
            return
        # 先挂上「在等结果」的牌子，再去装脚本 —— 装脚本会喂一次 Playwright，
        # 万一这时候回调到了，也不能被当成上一轮的残留丢掉
        with self._lock:
            self._busy = True
        self._drop_stale_picks()
        self._arm(page)
        try:
            current = page.url or ""
        except Exception:
            current = ""
        if self._was_reused:
            self.log.emit(
                f"浏览器还开着，直接接着用（当前页面：{current or '（空白页）'}）；"
                "要重新加载就把那个浏览器窗口关掉再抓。")
            self.reused.emit(current)
        else:
            self.opened.emit(current)

    def _do_abort(self):
        """强制停止的落地：关浏览器、清干净，下次捕获重新开一个。"""
        self._close_browser_quietly()
        with self._lock:
            self._busy = False
        self._picks = queue.Queue()
        self.log.emit("已强制停止：浏览器关掉了，下次捕获会重新开一个。")

    def _drop_stale_picks(self):
        """清掉上一轮可能残留的回调，免得「一按捕获就蹦出个旧结果」。"""
        while True:
            try:
                self._picks.get_nowait()
            except queue.Empty:
                return

    def _ensure_page(self, url: str):
        """保证有一个能用的页面。

        已经开着就**不重新导航** —— 你点到一半的页面状态（翻过的页、展开的菜单、
        登录过的账号）才是值钱的东西，重开一次全没了。要重来就把浏览器关了再抓。
        """
        self._was_reused = False
        page = self._page
        try:
            if page is not None and not page.is_closed():
                self._was_reused = True
                page.bring_to_front()
                return page
        except Exception:
            pass
        if not url:
            self.failed.emit(
                "这一步没有网址，也没有已经开着的浏览器。\n"
                "先在它前面放一个【打开网页】节点并填上网址"
                "（或先捕获别的一步，把浏览器开起来）。")
            return None
        try:
            browser = self._pw.chromium.launch(headless=False)
            context = browser.new_context()
            context.add_init_script(PICKER_JS)
            page = context.new_page()
            page.expose_function("__trae_pick", self._on_pick)
            self._page = page
            self.log.emit(f"正在打开 {url} …")
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page
        except Exception as e:
            self._close_browser_quietly()
            self.failed.emit(f"打开网址失败：{_first_line(e, 150)}")
            return None

    def _arm(self, page):
        """让当前页面进入捕获待命（每个 frame 都装一遍，iframe 里的元素也能抓）。"""
        self.log.emit("按住 Ctrl 划过页面＝高亮，按住 Ctrl 点一下＝捕获该元素；Esc＝取消。")
        try:
            page.evaluate(PICKER_JS)
        except Exception as e:
            self.log.emit(f"注入捕获脚本失败：{_first_line(e)}")
        for frame in list(page.frames):
            if frame is page.main_frame:
                continue
            try:
                frame.evaluate(PICKER_JS)
            except Exception:
                continue                # 跨域的 iframe 注不进去，正常

    def _drain(self):
        """把捕获结果发出去（抓一个就收工，不是连着抓）。"""
        while True:
            try:
                payload = self._picks.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                if not self._busy:
                    continue            # 上一次已经收工了，这条是迟到的
                self._busy = False
            kind = (payload.get("kind") or "pick").strip()
            if kind == "abort":
                self.aborted.emit(payload.get("reason") or "已强制退出")
                continue
            if kind == "cancel":
                self.canceled.emit(payload.get("reason") or "已取消")
                continue
            self._seq += 1
            payload["image"] = self._save_shot(payload)
            self.picked.emit(payload)

    def _save_shot(self, payload: dict) -> str:
        """把刚捕获的元素截下来，返回相对项目的路径（img/xxx.png）。

        用 locator.screenshot()：它裁的就是元素精确边框，不受滚动/缩放影响。
        iframe 里的元素（主 page 找不到这个 XPath）跳过截图，交给上层提示。
        """
        xpath = (payload.get("xpath") or "").strip()
        if not xpath or not payload.get("top") or self._shot_dir is None:
            return ""
        try:
            loc = self._page.locator(f"xpath={xpath}")
            if loc.count() < 1:
                return ""
            self._shot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = next_shot_path(self._shot_dir, f"{stamp}_{self._seq}")
            loc.first.screenshot(path=str(path))
            return f"img/{path.name}"
        except Exception as e:
            self.log.emit(f"元素截图失败（XPath 仍然可用）：{_first_line(e, 100)}")
            return ""

    # ------------------------------
    # 回放 / 重新加载 / 回到入口页
    # ------------------------------
    def _alive_page(self, what: str):
        page = self._page
        try:
            if page is not None and not page.is_closed():
                return page
        except Exception:
            pass
        self.log.emit(f"浏览器没开着，{what}不了。点【捕获元素…】会重新开一个。")
        return None

    def _do_replay(self, spec: Dict[str, Any]):
        """把当前这一步**之前**的节点，在这个浏览器里跑一遍。

        用**真正的执行器**跑（借用我们这个页面），所以行为跟跑流程完全一致：
        循环、条件、变量替换、步骤后等待都照常。代价是那些节点会有真实副作用
        （真点、真填、真发），所以条上留了【跳过回放】随时能中断。
        """
        page = self._alive_page("回放")
        if page is None:
            self.replayed.emit(False, "浏览器没开着，没法回放。")
            return
        steps = list(spec.get("steps") or [])
        if not steps:
            self.replayed.emit(True, "当前这一步前面没有节点，不用回放。")
            return
        # 回放没法等人工（那会在这儿挂住），遇到就停在那儿，剩下的交给用户
        cut = next((i for i, s in enumerate(steps)
                    if getattr(s, "action", "") == "pause_for_human"), None)
        note = ""
        if cut is not None:
            steps, note = steps[:cut], "；后面有「暂停等人工」，回放到它前面为止"
        if not steps:
            self.replayed.emit(True, f"前面的步骤要人工操作，回放跳过{note}。")
            return
        self.log.emit(f"重跑前面的 {len(steps)} 个节点 —— 会真的点、真的发，"
                      f"跑完停在当前这一步的页面{note}。不想等就点【跳过回放】。")
        ex = StepExecutor(steps, spec.get("variables") or {}, headless=False,
                          project_dir=spec.get("project_dir"),
                          log=self.log.emit, page=page)
        self._replay_exec = ex
        # 兜底刹车：万一卡在某个页面上，到点让执行器自己停（它每步之间看 _stop）
        timer = threading.Timer(
            REPLAY_MAX_S, lambda: setattr(ex, "_stop", True))
        timer.daemon = True
        timer.start()
        try:
            ex.run()
            ok, msg = True, f"前面的 {len(steps)} 个节点跑完了{note}。"
        except Exception as e:
            ok, msg = False, f"回放中途出错：{_first_line(e)}"
        finally:
            timer.cancel()
            self._replay_exec = None
        # 回放跑完（或中途被跳过）就把捕获脚本装上、正式开始等捕获
        try:
            if not page.is_closed():
                with self._lock:
                    self._busy = True
                self._drop_stale_picks()
                self._arm(page)
                page.bring_to_front()
        except Exception:
            pass
        self.replayed.emit(ok, msg)

    def _do_navigate(self, mode: str, url: str = ""):
        """重新加载当前页 / 回到流程入口页。"""
        page = self._alive_page("刷新" if mode == "reload" else "打开入口页")
        if page is None:
            return
        try:
            page.bring_to_front()
            if mode == "reload":
                page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                self.log.emit("已重新加载当前页面。")
            else:
                if not url:
                    self.log.emit("流程里还没有「打开网页」的地址，没地方回。")
                    return
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                self.log.emit(f"已回到入口页：{url}")
        except Exception as e:
            self.log.emit(f"没弄成：{_first_line(e, 150)}")

    # ------------------------------
    # 试运行
    # ------------------------------
    def _do_trial(self, spec: Dict[str, Any]):
        page = self._page
        try:
            alive = page is not None and not page.is_closed()
        except Exception:
            alive = False
        if not alive:
            self.tried.emit(
                False,
                "浏览器已经关了。点【捕获元素…】会重新开一个，或者自己再开一个页面。")
            return
        xpath = (spec.get("xpath") or "").strip()
        action = (spec.get("action") or "click").strip()
        value = str(spec.get("value") or "")
        if not xpath:
            self.tried.emit(False, "还没有定位路径（先捕获一个元素）。")
            return
        try:
            page.bring_to_front()
            page.wait_for_timeout(120)
            if not self._highlight(page, xpath):
                self._finish_trial(
                    page, False,
                    f"当前页面上找不到这个元素：{xpath}\n"
                    "是不是页面已经跳走了？先把浏览器点回这一步所在的页面再试。")
                return
            page.wait_for_timeout(TRIAL_PREVIEW_MS)   # 先让人看清绿框
            loc = page.locator(f"xpath={xpath}").first
            if action == "fill":
                loc.fill(value, timeout=TRIAL_TIMEOUT_MS)
                note = "（里面还写着 {{变量}}，试运行就是原样填进去）" \
                    if "{{" in value else ""
                self._finish_trial(page, True, f"已把「{value}」填进去了{note}")
            elif action == "select":
                self._select_option(loc, value)
                self._finish_trial(page, True, f"已选中「{value}」")
            else:
                loc.click(timeout=TRIAL_TIMEOUT_MS)
                self._finish_trial(
                    page, True, "已点击。看看页面反应对不对（跳走了也算正常）。")
        except Exception as e:
            self._finish_trial(page, False, f"试运行失败：{_first_line(e, 150)}")

    def _finish_trial(self, page, ok: bool, message: str):
        """结果既回给程序（写在那行提示上），也贴在页面顶端 —— 动作发生在页面上，
        反馈就该出现在页面上，不然用户盯着程序看，不知道页面里到底动了没动。"""
        text = ("✓ " if ok else "✗ ") + message
        for frame in list(page.frames):
            try:
                if frame.evaluate(TOAST_JS, {"ok": bool(ok), "text": text}):
                    break
            except Exception:
                continue
        self.tried.emit(ok, message)

    def _highlight(self, page, xpath: str) -> bool:
        """先在页面里把元素圈出来（含 iframe），圈到了返回 True。"""
        for frame in list(page.frames):
            try:
                if frame.evaluate(HIGHLIGHT_JS, xpath):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _select_option(loc, value: str):
        """尽量像「下拉选择」节点那样选：先按显示文字，不行再按 value。"""
        try:
            loc.select_option(label=value, timeout=TRIAL_TIMEOUT_MS)
            return
        except Exception:
            pass
        loc.select_option(value=value, timeout=TRIAL_TIMEOUT_MS)

    # ------------------------------
    # 收尾
    # ------------------------------
    def _close_browser_quietly(self):
        """把浏览器收掉。

        先问一句 `is_closed()` 再关：用户直接把浏览器窗口点掉时，连接已经断了，
        这时候还去调 `close()` 有可能卡在那儿等一个永远不会来的回应 —— 整个程序
        就跟着僵住（这个坑踩过）。已经没了就干脆什么都不做，让它随进程一起走。
        """
        page, self._page = self._page, None
        if page is None:
            return
        try:
            if page.is_closed():
                return
            page.context.browser.close()
        except Exception:
            pass


#: 全进程共用一个会话（浏览器开着不放，跨对话框复用）
_SHARED: Optional[PickerSession] = None


def shared() -> PickerSession:
    """拿到共用的捕获会话（第一次调用时把线程起起来）。"""
    global _SHARED
    if _SHARED is None or not _SHARED.isRunning():
        _SHARED = PickerSession()
        _SHARED.start()
    return _SHARED


def shutdown():
    """退出程序时调用：把浏览器关干净。"""
    global _SHARED
    if _SHARED is not None:
        _SHARED.shutdown()
        _SHARED = None
