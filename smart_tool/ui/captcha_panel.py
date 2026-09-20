# -*- coding: utf-8 -*-
"""验证码节点的配置面板（滑块 / 文字点选 / 计算题）。

**验证码图本身用上面那行「定位路径 / 图片模板」框**（它是主定位），这里只配
几个辅助位置和重试策略：

- 滑块手柄  拖动从哪儿开始 —— 滑块必填，缺了会拖到莫名其妙的地方；
- 题干      页面上写着「请依次点击：xxx」的那个元素（文字点选用）；
            题干不是独立元素时，直接手写进「题干文字」；
- 答案填到  计算题的答案填进哪个输入框；留空＝直接敲进当前光标所在的地方；
- 换一张    识别没过时点它换张图再来（可选，配上更容易过）。

识别本身在 core/captcha.py：滑块和点选/计算题共用 ddddocr，其中**滑块没装
ddddocr 也能跑**（有纯 OpenCV 的兜底），所以没装时只在非滑块类型上出提示。
"""
from typing import Dict, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from smart_tool.core import captcha
from smart_tool.core.project_store import Locator, Step
from smart_tool.ui.help_tip import HelpButton

#: 辅助定位行：(字段名, 行标题, 只在哪种类型下出现（None＝都出现）, 悬停说明)
ROWS = [
    ("captcha_slider", "滑块手柄：", "slider",
     "滑块拼图那个能拖的东西。拖动就是从它的中心开始的。"),
    ("captcha_tip", "题干：", "click_text",
     "页面上写着「请依次点击：圈、流、伟」的那个元素。\n"
     "填了它就自动去那儿读文字；读不到就把字直接写进下面的「题干文字」。"),
    ("captcha_input", "答案填到：", "math",
     "算出来的答案要填进哪个输入框。\n留空＝直接敲进当前光标在的地方（常配在前面的「点击」之后）。"),
    ("captcha_refresh", "换一张：", None,
     "识别没过 / 拖过去没通过校验时，点它换一张图再试。留空＝原地重试。"),
]

CAPTCHA_HELP = (
    "【验证码图放哪】\n"
    "就是上面那行「定位路径」（桌面场景是「图片模板」）——框住那一整张验证码图\n"
    "（滑块的话是**带缺口的那张背景图**）。\n"
    "\n"
    "【三种类型】\n"
    "· 滑块拼图 —— 认缺口位置，再把滑块拖过去。要填「滑块手柄」（拖动起点）；\n"
    "· 文字点选 —— 认图上的字，按题干说的顺序依次点。要填题干或题干文字；\n"
    "· 计算题 —— 认算式、算出答案，再填进去。可以指定「答案填到」哪个输入框。\n"
    "\n"
    "【要不要装额外的库】\n"
    "文字点选和计算题要认字，得装 ddddocr（在项目的 .venv 里跑\n"
    "  .venv\\Scripts\\python -m pip install ddddocr）。\n"
    "滑块不需要它，没装也能跑（用 OpenCV 自己找缺口，只是没 ddddocr 稳）。\n"
    "\n"
    "【没过会怎样】\n"
    "处理完会看一眼验证码图还在不在：还在就点「换一张」重来，\n"
    "试满「最多试几次」还不过就报错停下（可以改成「暂停等人工」自己处理）。\n"
    "注意这只是旁证，站点真正怎么判是它自己的事 ——\n"
    "判不出来时不会瞎猜，会当成处理过了继续往下走。"
)

OCR_MISSING_TIP = (
    "这台机器上还没装 ddddocr ——「文字点选 / 计算题」跑不了（滑块不受影响）。\n"
    "装法：在项目的 .venv 里执行  .venv\\Scripts\\python -m pip install ddddocr"
)


class CaptchaPanel(QWidget):
    """验证码节点的辅助配置：类型 + 几个位置 + 重试策略。"""

    #: 某个字段想用【捕获元素…】时发出（带上字段名，如 captcha_slider）
    capture_requested = pyqtSignal(str)
    #: 类型换了（外面的表单要重排一下高度）
    changed = pyqtSignal()

    def __init__(self, desktop: bool = False, parent=None):
        super().__init__(parent)
        self._desktop = bool(desktop)
        self._edits: Dict[str, QLineEdit] = {}
        self._types: Dict[str, str] = {}
        self._rows: Dict[str, QWidget] = {}
        self._build()
        self._sync()

    # ------------------------------
    # 搭界面
    # ------------------------------
    def _build(self):
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        head = QHBoxLayout()
        head.addWidget(QLabel("类型："))
        self.kind_combo = QComboBox()
        for key, label in captcha.KINDS:
            self.kind_combo.addItem(label, key)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        head.addWidget(self.kind_combo, 1)
        head.addWidget(HelpButton("验证码怎么配", CAPTCHA_HELP))
        box.addLayout(head)

        for key, label, _only, tip in ROWS:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            name = QLabel(label)
            name.setMinimumWidth(72)
            edit = QLineEdit()
            edit.setToolTip(tip)
            edit.setPlaceholderText(
                "img/xxx.png" if self._desktop
                else "//div[@class='captcha-tip']")
            btn = QPushButton("定位匹配…" if self._desktop else "捕获元素…")
            btn.setToolTip(tip)
            btn.clicked.connect(
                lambda _checked=False, k=key: self.capture_requested.emit(k))
            lay.addWidget(name)
            lay.addWidget(edit, 1)
            lay.addWidget(btn)
            self._edits[key] = edit
            self._rows[key] = row
            box.addWidget(row)

        # 手写题干（只给文字点选用：题干不是页面元素时直接写）
        self.prompt_row = QWidget()
        pl = QHBoxLayout(self.prompt_row)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pname = QLabel("题干文字：")
        pname.setMinimumWidth(72)
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setPlaceholderText("直接手写要点的字，如：圈、流、伟")
        self.prompt_edit.setToolTip(
            "手上已经知道题干是什么时直接写在这儿，就不去页面上读了。\n"
            "填了它，「题干」那一行会被忽略。")
        pl.addWidget(pname)
        pl.addWidget(self.prompt_edit, 1)
        box.addWidget(self.prompt_row)

        tail = QHBoxLayout()
        tail.addWidget(QLabel("最多试几次："))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(1, 20)
        self.retry_spin.setValue(3)
        self.retry_spin.setToolTip(
            "含第一次在内，一共最多处理几次。每次之间会点一下「换一张」（配了的话）。")
        tail.addWidget(self.retry_spin)
        tail.addStretch(1)
        self.warn = QLabel(OCR_MISSING_TIP)
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color:#b45309;")
        tail.addWidget(self.warn)
        box.addLayout(tail)

    # ------------------------------
    # 显隐
    # ------------------------------
    def kind(self) -> str:
        return self.kind_combo.currentData() or "slider"

    def _on_kind_changed(self, _index):
        self._sync()
        self.changed.emit()

    def _sync(self):
        kind = self.kind()
        for key, _label, only, _tip in ROWS:
            self._rows[key].setVisible(only is None or only == kind)
        self.prompt_row.setVisible(kind == "click_text")
        # 滑块有纯 OpenCV 兜底，没装 ddddocr 也能跑；另外两种必须认字。
        # 这里用「包装了没」这个便宜判断（不 import、不起进程）—— 真能不能加载
        # 由运行时去探，探不出来会给一句说明怎么修的报错。
        self.warn.setVisible(not captcha.ocr_installed() and kind != "slider")
        self.updateGeometry()

    # ------------------------------
    # 存 / 取
    # ------------------------------
    def load(self, s: Step):
        self.kind_combo.setCurrentIndex(max(
            0, self.kind_combo.findData(s.captcha_kind or "slider")))
        for key, edit in self._edits.items():
            loc = getattr(s, key, None)
            if isinstance(loc, Locator):
                edit.setText(loc.value or loc.image or "")
                self._types[key] = loc.type or "xpath"
            else:
                edit.setText("")
                self._types.pop(key, None)
        self.prompt_edit.setText(s.captcha_prompt or "")
        self.retry_spin.setValue(max(1, min(20, int(s.captcha_retry or 3))))
        self._sync()

    def save(self, s: Step):
        s.captcha_kind = self.kind()
        s.captcha_prompt = self.prompt_edit.text().strip()
        s.captcha_retry = max(1, int(self.retry_spin.value()))
        for key, edit in self._edits.items():
            text = edit.text().strip()
            setattr(s, key, Locator(type=self._types.get(key, "xpath"),
                                    value=text) if text else None)

    def set_captured(self, target: str, value: str, type_: str = "xpath"):
        """捕获回来之后把结果填进对应的那一行。"""
        edit = self._edits.get(target)
        if edit is None:
            return
        edit.setText(value or "")
        self._types[target] = type_ or "xpath"

    def validate(self) -> Optional[str]:
        """明显缺东西就在保存时提醒（别等跑起来才报错）。"""
        kind = self.kind()
        if kind == "slider" and not self._edits["captcha_slider"].text().strip():
            return ("滑块验证码还要填「滑块手柄」—— 拖动就是从那儿开始的，"
                    "缺了它不知道从哪拖")
        if kind == "click_text":
            has_tip = bool(self._edits["captcha_tip"].text().strip())
            if not has_tip and not self.prompt_edit.text().strip():
                return ("文字点选要知道「点哪些字」：填「题干」"
                        "（页面上写着那句话的元素），或者直接把字写进「题干文字」")
        return None
