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
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from smart_tool.core.project_store import ProjectStore
from smart_tool.ui import picker_session
from smart_tool.ui.help_tip import HelpButton

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


class ElementPickerDialog(QDialog):
    """捕获条（accept 后用 result_data 取结果）。"""

    def __init__(self, url: str, project_dir: Path, parent=None):
        # 故意**不要父窗口**：捕获期间主界面要藏起来，有父子关系的话会把它一起带走
        super().__init__(None)
        self.setWindowTitle("元素捕获")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._url = (url or "").strip()
        self._home = parent.window() if parent is not None else None
        self._home_hidden = False
        self._wired: list = []
        self._started = False
        self.result_data: Optional[dict] = None
        self._init_ui()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        self.setFixedWidth(430)
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
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消（Esc）")
        self.btn_cancel.setToolTip(
            "不抓了。已经开着的浏览器不会关，下次捕获接着用。")
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

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
        self._wire(session)
        session.capture(self._url, self.img_dir)

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
        if self._home is None or self._home_hidden:
            return
        try:
            self._home.hide()
            self._home_hidden = True
        except Exception:
            pass

    def _show_home(self):
        if self._home is None or not self._home_hidden:
            return
        try:
            self._home.show()
            self._home.raise_()
            self._home.activateWindow()
        except Exception:
            pass
        finally:
            self._home_hidden = False

    # ------------------------------
    # 会话的回调
    # ------------------------------
    def _on_log(self, message: str):
        self.status.setText(message)

    def _on_opened(self, _url: str):
        self.status.setText(
            "页面已打开。按住 Ctrl 划过元素看橙框，按住 Ctrl 点一下＝捕获；"
            "不按 Ctrl 时页面照常能用。")

    def _on_reused(self, url: str):
        self.status.setText(
            f"接着用已经开着的浏览器（当前：{url or '空白页'}）。"
            "按住 Ctrl 点一下＝捕获。")

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

    def _on_canceled(self, reason: str):
        self._unwire()
        self._show_home()
        self.reject()

    def _on_gone(self, reason: str):
        """浏览器被关掉了：把话留在条上让人看见，别弹模态框。"""
        self._say_and_hold(reason)

    def _on_failed(self, message: str):
        """起不来（没网址、内核没装…）：同样留在条上。"""
        self._say_and_hold(message)

    def _say_and_hold(self, message: str):
        """出错时：主界面还回来，但这条**不关**，把原因写在上面让人读完再关。

        以前这里弹 QMessageBox：既挡视线、又在没有可见父窗口时不稳。
        写在条上更省事 —— 条是置顶的，一定看得见。
        """
        self._unwire()
        self._show_home()
        self.status.setText(message)
        self.status.setStyleSheet("color: #b45309;")
        self.btn_cancel.setText("关闭")
        self.btn_cancel.setToolTip("关掉这条提示。")

    # ------------------------------
    # 收尾
    # ------------------------------
    def accept(self):
        self._unwire()
        self._show_home()
        super().accept()

    def reject(self):
        """取消（含点右上角 ×、按 Esc）：主界面还回来，但**不关浏览器**。"""
        self._unwire()
        self._show_home()
        super().reject()

    def closeEvent(self, event):
        self._unwire()
        self._show_home()
        super().closeEvent(event)


def pick_element(parent, url: str, project_dir) -> Optional[dict]:
    """开捕获条让用户抓一个元素，返回它的信息（取消返回 None）。

    返回值就是 ElementPickerDialog.result_data：
    `{"xpath":…, "image":"img/xxx.png", "count":命中几个, "desc":元素描述}`。
    给「不想要元素截图、只要一个 XPath」的地方用（比如登录态体检、采集行定位）。
    """
    dlg = ElementPickerDialog(url, Path(project_dir), parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
        return None
    return dlg.result_data


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
