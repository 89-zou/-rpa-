# -*- coding: utf-8 -*-
"""元素捕获窗口：**自动开浏览器**，你按住 Ctrl 点元素，抓到就自动收工。

跟以前不一样的地方（都是为了「一步接一步抓、不用反复开网页」）：
- 网址直接用这一步（或项目里第一个「打开网页」节点）的，不用手填、不用点开始；
- 捕获期间**主界面会收起来**，抓完自动回来；
- 页面里平时照常能点能滚，**按住 Ctrl** 才是「我要抓」（松开 Ctrl 就还给页面）；
- **浏览器不关** —— 下一次捕获直接接着用你当前停留的页面（见 picker_session）。

所以这个窗口只是一条很小的「捕获条」：告诉你现在是什么情况，并留一个取消的出口。
"""
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout,
)

from smart_tool.core.project_store import ProjectStore
from smart_tool.ui import picker_session
from smart_tool.ui.help_tip import HelpButton

#: 这次运行里「要不要自动重跑前面的节点」问过没有 / 用户怎么答的。
#: 问一次就记住：问太勤很烦，不问又可能把文章重发一遍（真事）。
_replay_consent = None

#: 【?】里的完整说明（用户点开看的是纯文本，别用 markdown 记号）
PICKER_HELP = (
    "【怎么抓】浏览器会自己开好（用的是这一步的网址，不用你填、也不用点开始）。\n"
    "· 按住 Ctrl 把鼠标划过页面 → 目标元素被橙框圈住，旁边显示这个写法\n"
    "  「命中几个」；命中好几个说明不够准，最好换个元素或者换个写法。\n"
    "· 按住 Ctrl 点一下 → 抓下它的 XPath，同时把元素截图存进项目 img/\n"
    "  （以后 XPath 失效时可以拿这张图兜底）。\n"
    "· 抓完浏览器里会弹一条绿提示，主界面自动回来。\n"
    "\n"
    "【松开 Ctrl 就恢复正常】不按 Ctrl 的时候页面照常能点能滚，翻页、展开菜单\n"
    "都不会被拦下来 —— 可以先正常操作到目标页面，再按住 Ctrl 抓。\n"
    "\n"
    "【浏览器不会关】抓到之后浏览器一直开着，下一次捕获直接接着用你当前停留的\n"
    "页面：不用重新打开、不用重新登录、也不用重新点到那一层。\n"
    "想把页面重新加载一遍，就把那个浏览器窗口关掉再抓一次。\n"
    "\n"
    "【抓到的是元素，不是坐标】抓下来的是 XPath，页面改版时换个元素重抓就行，\n"
    "不依赖屏幕位置；这也正是网页场景比桌面场景耐用的地方。\n"
    "\n"
    "【不想抓了】按 Esc，或者点这条上的【取消】，主界面一样会回来。"
)

#: 待命时状态栏那句话（几处都用它，免得写得不一样）
READY_HINT = (
    "按住 Ctrl 划过元素看橙框，按住 Ctrl 点一下＝捕获；"
    "不按 Ctrl 时页面照常能用。Esc＝不抓了。"
)


class ElementPickerDialog(QDialog):
    """捕获条（accept 后用 result_data 取结果）。"""

    def __init__(self, url: str, project_dir: Path, parent=None,
                 replay=None):
        # 故意**不要父窗口**：捕获期间主界面要藏起来，有父子关系的话会把它一起带走
        super().__init__(None)
        self.setWindowTitle("元素捕获")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._url = (url or "").strip()
        # 「回放」用的料：{"steps": [...], "variables": {...}, "entry_url": "..."}
        # 有它就会在开抓之前先把前面的节点跑一遍，直接停在当前这一步的页面上
        self._replay = dict(replay or {})
        self._replaying = False
        self._home = parent.window() if parent is not None else None
        self._home_hidden = False
        self._also_hidden: list = []    # 藏主界面时一并收起来的其它窗口（步骤编辑器等）
        self._wired: list = []
        self._session = None
        self._started = False
        self.result_data: Optional[dict] = None
        self.error: str = ""            # 没抓成时的原因（给调用方拿去做提示）
        self._init_ui()
        # 看门狗：会话万一在后台死了（线程挂了 / 进程没了），也要把条收掉 ——
        # 绝不能留一个模态框在这儿干等，那会让人以为整个程序卡死了
        self._watch = QTimer(self)
        self._watch.setInterval(1000)
        self._watch.timeout.connect(self._check_session)
        self._watch.start()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        self.setFixedWidth(560)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("按住 Ctrl 点元素＝捕获")
        title.setStyleSheet("font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(HelpButton("元素捕获", PICKER_HELP))
        root.addLayout(head)

        self.status = QLabel("正在打开浏览器…")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #555;")
        root.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_replay = QPushButton("重跑前面的节点")
        self.btn_replay.setToolTip(
            "把当前这一步**之前**的节点在这个浏览器里跑一遍，直接停在\n"
            "当前这一步该在的页面上。注意那些节点会真的执行（真点、真填、真发）。")
        self.btn_replay.clicked.connect(self._on_replay_clicked)
        row.addWidget(self.btn_replay)

        self.btn_reload = QPushButton("重新加载")
        self.btn_reload.setToolTip("把这个页面重新加载一遍（等于按 F5）。")
        self.btn_reload.clicked.connect(self._on_reload_clicked)
        row.addWidget(self.btn_reload)

        self.btn_entry = QPushButton("回到入口页")
        self.btn_entry.setToolTip("打开流程里第一个【打开网页】节点的地址。")
        self.btn_entry.clicked.connect(self._on_entry_clicked)
        row.addWidget(self.btn_entry)

        row.addStretch(1)
        self.btn_cancel = QPushButton("取消（Esc）")
        self.btn_cancel.setToolTip(
            "不抓了。已经开着的浏览器不会关，下次捕获接着用。")
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)
        self._sync_buttons()

    # ------------------------------
    # 那排按钮
    # ------------------------------
    def _sync_buttons(self):
        """按「有没有得回放 / 是不是正在回放」调整按钮。

        回放期间【取消】变成【跳过回放】：这时候想退出，多半是想让流程停下来
        （而不是把捕获条关掉），所以复用同一个按钮，省一个位置。
        """
        busy = self._replaying
        self.btn_replay.setEnabled(
            bool(self._replay.get("steps")) and not busy)
        self.btn_reload.setEnabled(not busy)
        self.btn_entry.setEnabled(bool(self._replay.get("entry_url")) and not busy)
        if busy:
            self.btn_cancel.setText("跳过回放")
            self.btn_cancel.setToolTip("让回放停下来，直接进入捕获状态。")
        else:
            self.btn_cancel.setText("取消（Esc）")
            self.btn_cancel.setToolTip(
                "不抓了。已经开着的浏览器不会关，下次捕获接着用。")

    def _on_cancel_clicked(self):
        if self._replaying:
            if self._session is not None:
                self._session.skip_replay()
            self.status.setText("已请求跳过回放，这一步跑完就停…")
            return
        self.reject()

    def _on_replay_clicked(self):
        if not self._replaying and self._session is not None:
            self._ask_then_replay(manual=True)

    def _on_reload_clicked(self):
        if self._session is not None and not self._replaying:
            self._session.reload_page()

    def _on_entry_clicked(self):
        if self._session is not None and not self._replaying:
            self._session.open_entry(self._replay.get("entry_url") or "")

    def _ask_then_replay(self, manual: bool, why: str = ""):
        """回放前先问一句（每次运行只问一次）。

        为什么要问：回放就是把前面那些节点**真跑一遍**。你前面要是有点「发布」
        「提交」之类的节点，这就是真的会再发一次 —— 不打招呼就干这种事不行。
        """
        global _replay_consent
        steps = self._replay.get("steps") or []
        if not steps:
            self.status.setText("当前这一步前面没有节点，不用回放。")
            return
        if not manual:
            if _replay_consent is None:
                reply = QMessageBox.warning(
                    self, "要先把前面的节点跑一遍吗",
                    f"{why}\n"
                    f"要不要先把当前这一步「前面」的 {len(steps)} 个节点跑一遍，"
                    "让它自己走到当前这一步该在的页面上？\n\n"
                    "注意：那些节点会真的执行 —— 真点、真填、真提交。\n"
                    "如果里面有「发布 / 提交」这类节点，会再发一次。\n\n"
                    "（选「不要」就自己点过去；条上有【重跑前面的节点】随时可跑。）",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                _replay_consent = (reply == QMessageBox.StandardButton.Yes)
                if not _replay_consent:
                    self.status.setText(
                        "好，先不重跑。自己在浏览器里点到这一步的页面，"
                        "然后按住 Ctrl 点元素＝捕获（想重跑就点【重跑前面的节点】）。")
                    return
            elif not _replay_consent:
                return
        self._replaying = True
        self._sync_buttons()
        self._session.replay({
            "steps": steps,
            "variables": self._replay.get("variables") or {},
            "project_dir": self.project_dir,
        })

    def _on_replayed(self, ok: bool, message: str):
        self._replaying = False
        self._sync_buttons()
        self.status.setText(("✓ " if ok else "✗ ") + message
                            + "　现在按住 Ctrl 点元素＝捕获。")

    # ------------------------------
    # 开窗即开始捕获
    # ------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._started:
            self._started = True
            # 等窗口真正摆好再藏主界面，免得藏完自己被顺手收起
            QTimer.singleShot(0, self._start)

    def _start(self):
        if self._wired:
            return          # 已经在抓了：重复进来（show 了两次）别再接一遍信号
        self._hide_home()
        self._move_to_corner()
        session = picker_session.shared()
        self._session = session
        self._wire(session)
        # 有回放要做的话，先不装捕获脚本（免得回放途中被 Ctrl+点击抢走一个元素），
        # 等回放跑完由会话那边再装、再开始等捕获
        session.capture(self._url, self.img_dir,
                        arm=not self._replay.get("steps"))

    def _check_session(self):
        """会话在后台死了就把条收掉 —— 宁可不抓，也不留一个模态框把主界面卡住。"""
        if not self._wired or self._session is None:
            return
        try:
            alive = self._session.isRunning()
        except Exception:
            alive = False
        if not alive:
            self._finish_with_error(
                "捕获用的后台会话意外结束了，这次没抓成。\n"
                "再点一次【捕获元素…】就行（浏览器会重新开一个）。")

    def _wire(self, session):
        """接上会话的信号；收尾时必须摘掉 —— 会话是跨窗口复用的，
        不摘的话下一个捕获条开起来时，上一条也会收到消息。"""
        self._unwire()      # 兜底：万一是重复进来的，先把旧的摘干净再接
        self._wired = [
            (session.opened, self._on_opened),
            (session.reused, self._on_reused),
            (session.picked, self._on_picked),
            (session.canceled, self._on_canceled),
            (session.gone, self._on_gone),
            (session.failed, self._on_failed),
            (session.log, self._on_log),
            (session.replayed, self._on_replayed),
        ]
        for sig, slot in self._wired:
            sig.connect(slot)

    def _unwire(self):
        for sig, slot in self._wired:
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        self._wired = []

    def _move_to_corner(self):
        """摆到屏幕右上角：别挡住页面内容，也别跟浏览器抢地方。"""
        try:
            screen = self.screen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.top() + 24)
        except Exception:
            pass

    # ------------------------------
    # 主界面的藏 / 还
    # ------------------------------
    def _hide_home(self):
        """捕获期间把本程序所有露着的窗口都收起来，只留这条捕获条。

        为什么不挑「哪个是主界面」：从步骤编辑器里点捕获时，parent.window() 拿到的
        是**编辑器自己**（它本身就是顶层窗口）——只藏它的话主界面还在后面露着。
        而且编辑器是模态的：藏起来之后主界面虽然看得见，却点哪儿都没反应，
        看起来就是卡死了。所以索性全收走，收工时再逐个放回来。
        """
        if self._home_hidden or self._home is None:
            return
        try:
            self._also_hidden = [
                w for w in QApplication.topLevelWidgets()
                if w is not self and w.isVisible()
                and w.windowType() in (Qt.WindowType.Window,
                                       Qt.WindowType.Dialog)
            ]
            for w in self._also_hidden:
                w.hide()
            self._home_hidden = True
        except Exception:
            pass

    def _show_home(self):
        """收工：把刚才收起来的窗口都放回来（漏放一个就可能让人以为卡死了）。"""
        if not self._home_hidden:
            return
        shown, self._also_hidden = self._also_hidden, []
        self._home_hidden = False
        for w in shown:
            try:
                w.show()
                w.raise_()
            except Exception:
                pass
        # 焦点还给「刚才在用的那个」：模态的（步骤编辑器）优先，否则给主界面。
        # 不然回来之后还得自己点一下窗口才接着能操作。
        target = next((w for w in shown if w.isModal()), None) or self._home
        if target is not None:
            try:
                target.activateWindow()
            except Exception:
                pass

    # ------------------------------
    # 会话的回调
    # ------------------------------
    def _on_log(self, message: str):
        self.status.setText(message)

    def _on_opened(self, _url: str):
        if self._auto_replay("这次的浏览器是刚开的，页面还停在入口页。"):
            return
        self.status.setText(READY_HINT)

    def _on_reused(self, url: str):
        if self._auto_replay(f"浏览器里现在停在：{url or '空白页'}。"):
            return
        self.status.setText(
            f"接着用已经开着的浏览器（当前：{url or '空白页'}）。\n"
            + READY_HINT)

    def _auto_replay(self, why: str) -> bool:
        """问一句要不要先把前面的节点跑一遍（不自动跑：那可能真的又发一篇文章）。"""
        if not self._replay.get("steps"):
            return False
        self._ask_then_replay(manual=False, why=why)
        return True

    def _on_picked(self, payload: dict):
        self._unwire()
        self._show_home()
        xpath = payload.get("xpath") or ""
        self.result_data = {
            "xpath": xpath,
            "image": payload.get("image") or "",
            "count": payload.get("count", -1),
            "desc": payload.get("desc", ""),
        }
        self.accept()

    def _on_canceled(self, _reason: str):
        """用户在页面里按了 Esc：当取消处理（不算出错，不用提示）。"""
        self._unwire()
        self._show_home()
        self.reject()

    def _on_gone(self, reason: str):
        self._finish_with_error(reason or "浏览器窗口被关掉了。")

    def _on_failed(self, message: str):
        self._finish_with_error(message)

    def _finish_with_error(self, message: str):
        """出错 / 浏览器没了：**一定要把这条收掉**，并把原因交给调用方去显示。

        为什么不留着让人读：这条是模态窗口，留着的话主界面虽然显示回来了、
        却点哪儿都没反应 —— 看起来就是整个程序卡死了（这个坑踩过一次）。
        原因会写到步骤编辑器那行提示上，就在刚才那个按钮旁边，一样看得见。
        """
        self._watch.stop()
        self._unwire()
        self.error = message or "这次没抓成。"
        self._show_home()
        self.reject()

    # ------------------------------
    # 收尾
    # ------------------------------
    def keyPressEvent(self, event):
        """按 Esc 直接退出捕获状态。

        页面里也接了 Esc（浏览器有焦点时走那条），这里管的是「焦点在这条上」
        的情况 —— 两条路都通，随便按哪个都能出来。
        正在回放时，Esc 是「跳过回放」而不是把条关掉。
        """
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel_clicked()
            return
        super().keyPressEvent(event)

    def accept(self):
        self._watch.stop()
        self._unwire()
        self._show_home()
        super().accept()

    def reject(self):
        """取消（含点右上角 ×、按 Esc）：主界面还回来，但**不关浏览器**。"""
        self._watch.stop()
        self._unwire()
        self._show_home()
        super().reject()

    def closeEvent(self, event):
        self._watch.stop()
        self._unwire()
        self._show_home()
        super().closeEvent(event)


def pick_element_result(parent, url: str, project_dir, replay=None):
    """跑一次捕获 → (结果, 出错原因)。用户取消时是 (None, "")。

    出错原因要交回去显示：捕获那条一定会自己关掉（模态窗口留着会把主界面卡住），
    所以原因得由调用方写在自己的界面上。

    replay 给了就让浏览器先「回放前面的节点」走到当前这一步的页面，见
    ElementPickerDialog 的同名参数。
    """
    dlg = ElementPickerDialog(url, Path(project_dir), parent, replay=replay)
    dlg.exec()
    if dlg.result_data:
        return dlg.result_data, ""
    return None, dlg.error


def pick_element(parent, url: str, project_dir, replay=None) -> Optional[dict]:
    """开捕获条让用户抓一个元素，返回它的信息（取消返回 None）。

    返回值就是 ElementPickerDialog.result_data：
    `{"xpath":…, "image":"img/xxx.png", "count":命中几个, "desc":元素描述}`。
    给「不想要元素截图、只要一个 XPath」的地方用（比如登录态体检、采集行定位）。
    需要知道「为什么没抓成」的，用 pick_element_result()。
    """
    data, _err = pick_element_result(parent, url, project_dir, replay=replay)
    return data


def drop_capture_image(project_dir, data: dict) -> None:
    """「只要 XPath、不要元素截图」的地方收尾用：把那张图删掉。

    捕获时顺手截的那张元素图，很多地方（登录态体检、采集行定位、验证码的辅助
    位置）根本用不上，留着只会在项目 img/ 里堆废图。
    """
    rel = (data or {}).get("image") or ""
    if not rel:
        return
    try:
        (Path(project_dir) / rel).unlink()
    except OSError:
        pass


def guess_locator_name(data: dict, taken=None) -> str:
    """从捕获结果的元素描述里猜一个名字（给它个像样的默认值，不用现想）。

    desc 长这样：`<a>#menu-posts “文章”`、`<input>#user_login`、`<div.item> “书”`。
    优先用元素上的文字，其次用 id；重名就往后加 2、3…
    """
    import re
    desc = str((data or {}).get("desc") or "")
    m = re.search(r"[“\"](.+?)[”\"]", desc)
    base = re.sub(r"\s+", "", m.group(1)) if m else ""
    if not base:
        m = re.search(r"#([A-Za-z0-9_-]+)", desc)
        base = m.group(1) if m else ""
    base = base[:12] or "元素"
    name, i = base, 2
    while taken and name in taken:
        name = f"{base}{i}"
        i += 1
    return name


def save_captured_locator(parent, project_dir, data: dict) -> str:
    """把这次捕获到的 XPath 存进项目的「元素定位」，返回变量名（没存返回 ""）。

    - 同一个 XPath 已经存过 → 不再重复问，直接复用原来那个名字
    - 名字留空或取消 → 不存（只填在当前这个字段里）
    """
    from PyQt6.QtWidgets import QInputDialog

    xpath = (data or {}).get("xpath") or ""
    xpath = xpath.strip()
    if not xpath:
        return ""
    store = ProjectStore(Path(project_dir))
    locators = store.load_locators()
    for name, value in locators.items():
        if value.strip() == xpath:
            return name
    name, ok = QInputDialog.getText(
        parent, "存成「元素定位」",
        "要不要把这次抓到的元素存下来？\n"
        "存了以后，任何「定位路径」里写 {{名字}} 就能复用它，"
        "改一处全项目都跟着变。\n"
        "（留空 = 不存，只填在当前这个字段里）\n\n"
        f"XPath：{xpath[:150]}",
        text=guess_locator_name(data, locators),
    )
    name = (name or "").strip()
    if not ok or not name:
        return ""
    locators[name] = xpath
    store.save_locators(locators)
    return name
