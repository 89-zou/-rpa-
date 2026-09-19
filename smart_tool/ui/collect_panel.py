# -*- coding: utf-8 -*-
"""采集数据节点的配置面板：采一条还是采一列、每个字段怎么取。

字段表格四列：
- 字段名：存进记录 / 当作变量名用的名字（如 标题、正文、封面图）
- 取什么：文字 / 属性 / 链接 / HTML / 图片 / 文件 / 截图
- 定位：XPath；**列表模式下是在「当前行」里找**（Playwright 的嵌套 XPath
  就是元素内定位，写 //h2 或 .//h2 都行）
- 附加：**不是备注**，是「取什么」的可选参数——取属性时＝属性名（src / title /
  data-xxx）；截图时＝留空＝截这个元素、写「整页」、或写 x,y,宽,高 截区域。
  表头就写两个字，详细填法放在表头的悬停提示里，免得挤掉定位列。

列表模式的「每行的定位」可以直接用【捕获元素…】抓：打开浏览器点一下页面上
「一行」的位置就行（捕获到的通常带 [1] 这类序号，去掉它才能匹配所有行）。
"""
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from smart_tool.core.project_store import Step

# (key, 显示名, 附加那一列的提示)
KIND_OPTIONS = [
    ("text", "文字（元素里的文本）", "不用填"),
    ("attr", "属性（src / title / data-xxx）", "属性名，如 src"),
    ("link", "链接（href，自动补全成完整网址）", "留空＝取 href，也可填别的属性"),
    ("html", "HTML（元素内部源码）", "不用填"),
    ("image", "图片（下载到 data/files/）", "留空＝取 src，也可填别的属性"),
    ("file", "文件（下载到 data/files/）", "留空＝取 href，也可填别的属性"),
    ("shot", "截图（存到 data/files/）", "留空＝截这个元素；「整页」；或 x,y,宽,高"),
]
# 「＋ 添加字段」下拉里的常用字段：(菜单文字, 字段名, 取什么)
QUICK_FIELDS = [
    ("标题（文字）", "标题", "text"),
    ("正文（文字）", "正文", "text"),
    ("链接（自动补全成完整网址）", "链接", "link"),
    ("属性（src / data-xxx）", "属性", "attr"),
    ("HTML（元素内部源码）", "HTML", "html"),
    ("图片（下载到 data/files/）", "图片", "image"),
    ("文件（下载到 data/files/）", "文件", "file"),
    ("截图（存成 png）", "截图", "shot"),
]
EXTRA_TIP_DEFAULT = "取属性时＝属性名（如 src）；截图时＝留空＝截元素、「整页」、或 x,y,宽,高"
COL_NAME, COL_KIND, COL_LOC, COL_EXTRA = 0, 1, 2, 3

MODES = [
    ("page", "采当前页面（一条记录）"),
    ("list", "列表采集（页面上多行，每行一条）"),
]

HINT = (
    "· 「文字 / 属性 / 链接 / HTML」直接存成文本；「图片 / 文件」下载到 data/files/；"
    "「截图」存成 png。\n"
    "· 列表模式下，字段的定位是在「当前行」里找（写 //h2 就是这一行里的 h2）；"
    "采到的数据会变成 {{变量}}（JSON 数组），配「循环」节点逐行遍历，"
    "循环里用 {{loop.item.字段}}。\n"
    "· 「附加」不是备注：取属性时填属性名（如 src），截图时填「整页」或 x,y,宽,高。\n"
    "· 所有数据都会追加到项目的 data/records.jsonl，"
    "在【项目管理…】→【采集数据】里可以查看 / 导出 Excel。\n"
    "· XPath 小坑：@class='a' 是「class 整个等于 a」，元素写的是 "
    "class=\"star-rating Three\" 就匹配不上，要用 //p[contains(@class,'star-rating')]。"
)


class CollectPanel(QWidget):
    """采集节点的配置。"""

    changed = pyqtSignal()
    #: 点了【捕获元素…】（行定位）：由外面的步骤对话框去开捕获窗口
    capture_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._init_ui()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("采集方式："))
        self.mode_combo = QComboBox()
        for key, label in MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self.mode_combo)
        row.addWidget(QLabel("产出变量名在下面「产出变量」里填"))
        row.addStretch()
        root.addLayout(row)

        # 列表模式才有：每行的定位（可以直接捕获）
        self.row_area = QWidget()
        ra = QVBoxLayout(self.row_area)
        ra.setContentsMargins(0, 0, 0, 0)
        ra.setSpacing(4)
        rl = QHBoxLayout()
        rl.addWidget(QLabel("每行的定位："))
        self.row_edit = QLineEdit()
        self.row_edit.setPlaceholderText("XPath，能命中多行，如 //div[@class='item']")
        self.row_edit.textChanged.connect(self._on_edited)
        rl.addWidget(self.row_edit, 1)
        self.btn_row_capture = QPushButton("捕获元素…")
        self.btn_row_capture.setToolTip(
            "打开浏览器，点一下页面上「一行」的位置，XPath 自动填进来。\n"
            "捕获到的通常带 [1] 这样的序号——去掉它才能匹配所有行。"
        )
        self.btn_row_capture.clicked.connect(self.capture_requested.emit)
        rl.addWidget(self.btn_row_capture)
        ra.addLayout(rl)
        self.capture_hint = QLabel("")
        self.capture_hint.setWordWrap(True)
        self.capture_hint.setStyleSheet("color: #0f766e;")
        ra.addWidget(self.capture_hint)
        root.addWidget(self.row_area)

        btns = QHBoxLayout()
        self.btn_add = QPushButton("添加字段")
        self.btn_add.setToolTip("点开选一个常用字段；或选「空字段」自己填名字和定位")
        self.btn_add.setMenu(self._build_add_menu())
        btns.addWidget(self.btn_add)
        self.btn_del = QPushButton("删除选中行")
        self.btn_del.clicked.connect(self.remove_selected)
        btns.addWidget(self.btn_del)
        btns.addStretch()
        root.addLayout(btns)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["字段名", "取什么", "定位（XPath）", "附加"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_KIND, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LOC, QHeaderView.ResizeMode.Stretch)
        # 「附加」给个够用的固定宽度（别按内容撑，不然会挤掉定位列）
        header.setSectionResizeMode(COL_EXTRA, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(COL_EXTRA, 130)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(160)
        self.table.itemChanged.connect(lambda _i: self._on_edited())
        root.addWidget(self.table, 1)
        self._refresh_extra_hint()

        hint = QLabel(HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777777;")
        root.addWidget(hint)

    def _build_add_menu(self) -> QMenu:
        menu = QMenu(self)
        act = QAction("空字段（自己填名字和定位）", menu)
        act.triggered.connect(lambda: self.add_field())
        menu.addAction(act)
        menu.addSeparator()
        for label, name, kind in QUICK_FIELDS:
            act = QAction(label, menu)
            act.triggered.connect(
                lambda _=False, n=name, k=kind: self.add_field(n, k))
            menu.addAction(act)
        return menu

    def _on_mode_changed(self, _index=None):
        self._sync_row_visible()
        self._on_edited()

    def _sync_row_visible(self):
        self.row_area.setVisible(self.mode_combo.currentData() == "list")

    def _on_edited(self, *_a):
        if not self._loading:
            self.changed.emit()

    # ------------------------------
    # 字段行
    # ------------------------------
    def add_field(self, name: str = "", kind: str = "text", locator: str = ""):
        self._loading = True
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, COL_NAME, QTableWidgetItem(name))
        combo = QComboBox()
        for key, label, _tip in KIND_OPTIONS:
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(kind)))
        combo.currentIndexChanged.connect(self._on_edited)
        combo.currentIndexChanged.connect(self._refresh_extra_hint)
        self.table.setCellWidget(row, COL_KIND, combo)
        self.table.setItem(row, COL_LOC, QTableWidgetItem(locator))
        self.table.setItem(row, COL_EXTRA, QTableWidgetItem(""))
        self._loading = False
        self._refresh_extra_hint()
        self.table.selectRow(row)
        self._on_edited()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        self._loading = True
        for row in rows:
            self.table.removeRow(row)
        self._loading = False
        self._refresh_extra_hint()
        self._on_edited()

    def set_row_locator(self, xpath: str, note: str = ""):
        """捕获回来后填「每行的定位」（由步骤对话框调用）。"""
        self.row_edit.setText(xpath)
        self.capture_hint.setText(note)

    def _refresh_extra_hint(self, *_a):
        """「附加」表头保持短，详细填法放进悬停提示（免得挤掉定位列）。"""
        kinds = {self._kind_at(r) for r in range(self.table.rowCount())}
        tips: List[str] = []
        for key, _label, tip in KIND_OPTIONS:
            if key in kinds and tip != "不用填" and tip not in tips:
                tips.append(tip)
        item = QTableWidgetItem("附加")
        if tips:
            text = "这一列有用，不是备注：\n" + "\n".join(f"· {t}" for t in tips)
        else:
            text = "这一列有用，不是备注：" + EXTRA_TIP_DEFAULT
        item.setToolTip(text)
        self.table.setHorizontalHeaderItem(COL_EXTRA, item)

    def _kind_at(self, row: int) -> str:
        combo = self.table.cellWidget(row, COL_KIND)
        return combo.currentData() if isinstance(combo, QComboBox) else "text"

    @staticmethod
    def _text_at(table, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item is not None else ""

    # ------------------------------
    # 读写配置
    # ------------------------------
    def load(self, step: Optional[Step]):
        self._loading = True
        mode = (getattr(step, "collect_mode", "") or "page") if step else "page"
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(mode)))
        self.row_edit.setText(getattr(step, "collect_row", "") or "")
        self.table.setRowCount(0)
        self._loading = False
        items = (getattr(step, "collect_fields", None) or []) if step else []
        for item in items:
            if isinstance(item, dict):
                self.add_field(str(item.get("name") or ""),
                               str(item.get("kind") or "text"),
                               str(item.get("locator") or ""))
                row = self.table.rowCount() - 1
                self.table.item(row, COL_EXTRA).setText(str(item.get("extra") or ""))
        if not items:
            self.add_field()
        self._loading = True
        self._sync_row_visible()
        self._loading = False

    def config(self) -> Tuple[str, str, List[Dict[str, str]]]:
        """(采集方式, 每行定位, 字段清单)；没名字的行丢掉。"""
        fields: List[Dict[str, str]] = []
        for row in range(self.table.rowCount()):
            name = self._text_at(self.table, row, COL_NAME)
            if not name:
                continue
            fields.append({
                "name": name,
                "kind": self._kind_at(row),
                "locator": self._text_at(self.table, row, COL_LOC),
                "extra": self._text_at(self.table, row, COL_EXTRA),
            })
        return (self.mode_combo.currentData() or "page",
                self.row_edit.text().strip(), fields)

    def field_names(self) -> List[str]:
        return [f["name"] for f in self.config()[2]]

    def validate(self) -> Optional[str]:
        """配置有没有明显问题；没问题返回 None。"""
        mode, row, fields = self.config()
        if not fields:
            return "还没有要采集的字段：点【＋ 添加字段】加一个（如 标题 / 正文）。"
        if mode == "list" and not row:
            return "列表采集要填「每行的定位」（能命中多行的 XPath），\n" \
                   "字段的定位会在每一行里面找。"
        for f in fields:
            if f["kind"] == "attr" and not f["extra"]:
                return f"字段「{f['name']}」是取属性，请在「附加」里填属性名（如 src）。"
            if f["kind"] in ("text", "attr", "link", "html") and not f["locator"]:
                return f"字段「{f['name']}」还没填定位（XPath）。"
            if f["kind"] == "shot" and not f["locator"] and not f["extra"]:
                return (f"字段「{f['name']}」是截图：要么填元素定位，"
                        "要么在「附加」里写「整页」或 x,y,宽,高。")
        return None
