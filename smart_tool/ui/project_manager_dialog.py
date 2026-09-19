# -*- coding: utf-8 -*-
"""项目管理对话框：项目列表 + 变量清单 + 图片库 + 登录态 + 采集数据 + 函数库。

左边选项目，右边按页签看这个项目的东西：

- 【变量清单】变量从哪来——「读取数据」节点产出的列表变量，以及手工加的自定义
  变量。循环里的 {{loop.item}} / {{loop.index}} 是运行时自动有的，不在这里列。
  自定义变量可以增删改（改动立即保存）；读取节点产出的要改名/换路径，
  请去画布上双击那个「读取数据」节点。
- 【图片库】项目 img/ 目录里的元素截图：能预览、能看「被哪几步引用」、
  能导入/替换/删除。删除前先看引用列，免得删掉正在用的模板。
- 【登录态】cookie / localStorage：登录一次以后就不用再登（AuthDialog）。
- 【采集数据】「采集数据」节点采到的东西（DataDialog）：看记录、导出 Excel。
  后两个页签直接复用那两个面板（embedded=True），所以关掉主界面上的按钮也能用。
- 【函数库】写一次、到处调用的一段代码：流程里用「调用函数」节点引用它，
  改了这里所有调用一起变（改名会自动同步到那些节点）。
"""
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from smart_tool.core import data_sources, project_store, step_executor
from smart_tool.core.data_sources import DataSourceConfig
from smart_tool.core.project_store import ProjectStore, Step, list_projects
from smart_tool.ui.auth_dialog import AuthDialog
from smart_tool.ui.data_dialog import DataDialog
from smart_tool.ui.help_tip import help_row
from smart_tool.ui.step_editor_dialog import StepEditDialog

# 页签下标
TAB_VARS, TAB_IMAGES, TAB_AUTH, TAB_DATA, TAB_FUNCS = 0, 1, 2, 3, 4

#: 【?】里的完整说明（界面上只留一句摘要，其余收进弹窗）
VARS_HELP = (
    "步骤里用 {{变量名}} 引用。变量有四个来源：\n"
    "\n"
    "1) 读取节点\n"
    "   「读取数据」节点从文件 / 文件夹读到的：第一行是它产出的列表变量\n"
    "   （如 数据列表），下面几行是每个文件的字段（如 loop.item.标题）。\n"
    "\n"
    "2) 采集节点\n"
    "   「采集数据」节点从网页上采到的：列表采集配「循环」逐项遍历\n"
    "   （循环里用 {{loop.item.字段}}）；采当前页面则用 {{变量.字段}}。\n"
    "\n"
    "3) 元素定位\n"
    "   捕获元素时存下来的 XPath（名字 → XPath）。任何步骤的「定位路径」里\n"
    "   写 {{名字}} 就能复用它，改这一处、全项目跟着变。\n"
    "\n"
    "4) 自定义创建\n"
    "   手工加的，账号密码之类。\n"
    "\n"
    "【能不能改】前两类是只读展示：想改名 / 换路径，点那一行的【来源】\n"
    "跳进对应节点去改（改名后别处的引用会自动跟着改）。\n"
    "元素定位和自定义变量可以在这里直接改，改完立即保存。\n"
    "\n"
    "循环里的 {{loop.index}}（第几轮）是运行时自动有的，不用配置；\n"
    "元素定位和自定义变量如果重名，运行时按元素定位取值（状态栏会提醒）。"
)

IMAGES_HELP = (
    "项目 img/ 目录里的元素截图：用【捕获元素…】抓的、以及你自己裁剪的都在这。\n"
    "\n"
    "「用在哪」列出引用它的步骤——定位方式＝截图的，或者 XPath 步骤的兜底截图。\n"
    "\n"
    "删除前先看这一列：删掉正在用的图，那些步骤运行时会报「截图文件不存在」。\n"
    "如果是图过时了（页面改版），用【替换…】换一张就行，文件名不变、步骤不用改。\n"
    "\n"
    "一个技巧：XPath 是主定位、截图是兜底。页面小改动时截图还能顶一阵，\n"
    "但别只靠图——图对分辨率 / 缩放敏感，换台机器可能就匹配不上了。"
)

FUNCS_HELP = (
    "函数＝写一次、到处调用的一段代码。流程里用「调用函数」节点引用它，\n"
    "改这里的代码，所有调用它的地方一起变（不用满流程去找）。\n"
    "\n"
    "【什么时候用】同一段处理要在多个地方做（清洗标题、算价格、补零、\n"
    "失败重试…），或者流程里塞了太多「自由代码」节点看着乱的时候。\n"
    "只在一个地方用的话，直接用「自由代码」节点更省事。\n"
    "\n"
    "【入口参数】写形参名，逗号分隔（如 `单价, 倍数`）。\n"
    "调用那边按 `形参名=值` 传（值可以写 {{变量}}）；没传的形参＝空文本。\n"
    "\n"
    "【代码】和「自由代码」节点一样：\n"
    "· 用形参名拿参数；return 的东西就是「调用函数」节点的返回值；\n"
    "· vars / log() / page / current_url / project_dir 都能用；\n"
    "· Python 超时会真的掐断（JS 里同步死循环拦不住）。\n"
    "\n"
    "【语言】Python 在本机跑、JS 在网页里跑（JS 需要浏览器页面）。\n"
    "\n"
    "注意：函数改名后，已经用到它的「调用函数」节点会被一起改成新名字，\n"
    "所以放心改。删函数则会让那些节点报「找不到函数」，需要重新选一个。"
)

# 变量行类型（存在「来源」列的 UserRole 里，用来区分增删改行为）
KIND_DATA, KIND_PROJECT, KIND_LOCATOR = "data", "project", "locator"

COL_NAME, COL_VALUE, COL_SRC = range(3)
PLACEHOLDER = "（运行时按项填充）"
# 图片库表格列
COL_IMG, COL_IMG_SIZE, COL_IMG_BYTES, COL_IMG_USE = range(4)
IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


class ProjectManagerDialog(QDialog):
    """项目管理：项目列表 + 变量清单 + 图片库。"""

    def __init__(self, current_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("项目管理")
        self.setMinimumSize(940, 580)
        self._stores: List[ProjectStore] = []
        self._open_name: Optional[str] = None
        self._store: Optional[ProjectStore] = None
        self._loading = False
        # 在这里改过哪个项目的步骤（改过当前项目 → 主界面要重新载入）
        self._changed_project: Optional[str] = None
        self._init_ui()
        self._reload(select_name=current_name)

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QHBoxLayout(self)

        # ---- 左：项目列表 ----
        left = QVBoxLayout()
        left.addWidget(QLabel("项目（按住 Ctrl 可多选，用于批量删除）"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        # 名字再长也不让它把右边挤变形：超长就省略号，鼠标停上去看完整信息
        self.list_widget.setMaximumWidth(300)
        self.list_widget.setWordWrap(False)
        self.list_widget.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._open_selected)
        left.addWidget(self.list_widget, 1)

        row_btns = QHBoxLayout()
        self.btn_delete = QPushButton("删除选中项目")
        self.btn_delete.clicked.connect(self._delete_selected)
        row_btns.addWidget(self.btn_delete)
        row_btns.addStretch()
        left.addLayout(row_btns)
        root.addLayout(left, 2)

        # ---- 右：变量清单 / 图片库 ----
        right = QVBoxLayout()
        self.header_label = QLabel("在左边选中一个项目，这里就是它的配置")
        self.header_label.setStyleSheet("color: #444; font-weight: bold;")
        right.addWidget(self.header_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_var_page(), "变量清单")
        self.tabs.addTab(self._build_image_page(), "图片库")
        # 这两个页签复用独立窗口里的同一套面板（embedded：不当独立窗口、不要关闭键）
        self.auth_panel = AuthDialog(None, None, self, embedded=True)
        self.tabs.addTab(self.auth_panel, "登录态")
        self.data_panel = DataDialog(None, self, embedded=True)
        self.tabs.addTab(self.data_panel, "采集数据")
        self.tabs.addTab(self._build_func_page(), "函数库")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        right.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.btn_open = QPushButton("打开项目")
        self.btn_open.setDefault(True)
        self.btn_open.clicked.connect(self._open_selected)
        bottom.addWidget(self.btn_open)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        bottom.addWidget(self.btn_close)
        right.addLayout(bottom)

        root.addLayout(right, 5)

    def _build_var_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)

        lay.addWidget(help_row("变量用 {{变量名}} 引用；下面按来源分组。",
                               "变量清单", VARS_HELP))

        self.data_label = QLabel("")
        self.data_label.setWordWrap(True)
        self.data_label.setStyleSheet("color: #0f766e;")
        lay.addWidget(self.data_label)

        self.var_table = QTableWidget(0, 3)
        self.var_table.setHorizontalHeaderLabels(["变量名", "值 / 示例", "来源"])
        header = self.var_table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_SRC, QHeaderView.ResizeMode.Stretch)
        self.var_table.itemChanged.connect(self._on_var_changed)
        self.var_table.cellClicked.connect(self._on_var_clicked)
        lay.addWidget(self.var_table, 1)

        btns = QHBoxLayout()
        self.btn_var_add = QPushButton("添加变量")
        self.btn_var_add.setToolTip("加一条自定义变量（账号密码之类）")
        self.btn_var_add.clicked.connect(self._add_var_row)
        btns.addWidget(self.btn_var_add)
        self.btn_locator_add = QPushButton("添加元素定位")
        self.btn_locator_add.setToolTip(
            "加一条「元素定位」（名字 → XPath），步骤的定位里写 {{名字}} 就能复用；\n"
            "平时用【捕获元素…】抓的时候也会自动问你要不要存一条。"
        )
        self.btn_locator_add.clicked.connect(self._add_locator_row)
        btns.addWidget(self.btn_locator_add)
        self.btn_var_del = QPushButton("删除选中变量")
        self.btn_var_del.clicked.connect(self._del_var_rows)
        btns.addWidget(self.btn_var_del)
        btns.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #2e7d32;")
        btns.addWidget(self.status_label)
        lay.addLayout(btns)
        return page

    # ------------------------------
    # 图片库（项目 img/ 目录）
    # ------------------------------
    def _build_image_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)

        lay.addWidget(help_row("项目 img/ 目录里的元素截图。",
                               "图片库", IMAGES_HELP))

        body = QHBoxLayout()

        self.img_table = QTableWidget(0, 4)
        self.img_table.setHorizontalHeaderLabels(["图片", "尺寸", "大小", "用在哪"])
        header = self.img_table.horizontalHeader()
        header.setSectionResizeMode(COL_IMG, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_IMG_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_IMG_BYTES, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_IMG_USE, QHeaderView.ResizeMode.Stretch)
        self.img_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.img_table.itemSelectionChanged.connect(self._show_image_preview)
        body.addWidget(self.img_table, 3)

        self.img_preview = QLabel("选中一张图，这里看大图")
        self.img_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_preview.setWordWrap(True)
        self.img_preview.setMinimumWidth(200)
        self.img_preview.setStyleSheet(
            "border: 1px dashed #bbb; border-radius: 4px; color: #999;"
        )
        body.addWidget(self.img_preview, 2)
        lay.addLayout(body, 1)

        btns = QHBoxLayout()
        self.btn_img_import = QPushButton("导入图片…")
        self.btn_img_import.setToolTip("把外面的图片拷进项目 img/（可多选）")
        self.btn_img_import.clicked.connect(self._import_images)
        btns.addWidget(self.btn_img_import)
        self.btn_img_replace = QPushButton("替换…")
        self.btn_img_replace.setToolTip(
            "用另一张图覆盖选中的这张（文件名不变，引用它的步骤不用改）"
        )
        self.btn_img_replace.clicked.connect(self._replace_image)
        btns.addWidget(self.btn_img_replace)
        self.btn_img_delete = QPushButton("删除选中")
        self.btn_img_delete.clicked.connect(self._delete_images)
        btns.addWidget(self.btn_img_delete)
        btns.addStretch()
        self.img_status = QLabel("")
        self.img_status.setStyleSheet("color: #2e7d32;")
        btns.addWidget(self.img_status)
        lay.addLayout(btns)
        return page

    # ------------------------------
    # 函数库（一处定义、多处调用）
    # ------------------------------
    def _build_func_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)

        lay.addWidget(help_row(
            "写一次、到处调用：流程里用「调用函数」节点引用它。",
            "函数库", FUNCS_HELP))

        body = QHBoxLayout()

        left = QVBoxLayout()
        self.func_list = QListWidget()
        self.func_list.setMaximumWidth(220)
        self.func_list.setWordWrap(False)
        self.func_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.func_list.itemSelectionChanged.connect(self._on_func_selected)
        left.addWidget(self.func_list, 1)
        fbtns = QHBoxLayout()
        self.btn_func_add = QPushButton("新建函数")
        self.btn_func_add.clicked.connect(self._add_function)
        fbtns.addWidget(self.btn_func_add)
        self.btn_func_del = QPushButton("删除选中")
        self.btn_func_del.clicked.connect(self._del_function)
        fbtns.addWidget(self.btn_func_del)
        left.addLayout(fbtns)
        body.addLayout(left, 2)

        right = QVBoxLayout()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.func_name = QLineEdit()
        self.func_name.setPlaceholderText("如：清洗标题（调用节点里按这个名字找）")
        self.func_name.editingFinished.connect(self._save_functions)
        form.addRow("函数名：", self.func_name)

        self.func_lang = QComboBox()
        self.func_lang.addItem("Python（本机执行）", "python")
        self.func_lang.addItem("JavaScript（在网页里执行）", "javascript")
        self.func_lang.currentIndexChanged.connect(self._save_functions)
        form.addRow("语言：", self.func_lang)

        self.func_params = QLineEdit()
        self.func_params.setPlaceholderText("形参名，逗号分隔，如：单价, 倍数（留空＝没有参数）")
        self.func_params.editingFinished.connect(self._save_functions)
        form.addRow("入口参数：", self.func_params)

        self.func_desc = QLineEdit()
        self.func_desc.setPlaceholderText("选填：一句话说明它干什么，调用的时候鼠标停上去能看到")
        self.func_desc.editingFinished.connect(self._save_functions)
        form.addRow("说明：", self.func_desc)
        right.addLayout(form)

        self.func_code = QPlainTextEdit()
        self.func_code.setPlaceholderText(
            "# 例：\n"
            "#   标题 = 标题.strip()\n"
            "#   return 标题 + '（已处理）'"
        )
        self.func_code.setMinimumHeight(150)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.func_code.setFont(mono)
        self.func_code.textChanged.connect(self._on_func_code_changed)
        right.addWidget(QLabel("函数代码："))
        right.addWidget(self.func_code, 1)

        fbottom = QHBoxLayout()
        fbottom.addStretch()
        self.func_save_btn = QPushButton("保存函数")
        self.func_save_btn.clicked.connect(self._save_functions)
        fbottom.addWidget(self.func_save_btn)
        right.addLayout(fbottom)
        body.addLayout(right, 5)
        lay.addLayout(body, 1)

        self.func_status = QLabel("")
        self.func_status.setStyleSheet("color: #2e7d32;")
        lay.addWidget(self.func_status)

        # 编辑时自动存：代码打字停下来 1 秒就写盘（不必记得点保存）
        self._funcs: List[Dict[str, str]] = []
        self._func_row = -1
        self._loading_func = False
        self._saved_funcs: List[Dict[str, str]] = []
        self._func_timer = QTimer(self)
        self._func_timer.setSingleShot(True)
        self._func_timer.setInterval(1000)
        self._func_timer.timeout.connect(self._save_functions)
        return page

    def _load_functions(self):
        """刷新函数库列表（第一个函数默认选中）。

        换项目 / 换页签时先把 _func_row 清掉：否则右边表单里留着上一个项目的
        内容，会被当成「这个函数被编辑了」写进新项目的函数库。
        """
        self._func_timer.stop()
        self._func_row = -1
        if self._store is None:
            self._funcs = []
            self._saved_funcs = []
            self._loading_func = True
            self.func_list.clear()
            self._loading_func = False
            self._show_function(None)
            return
        self._funcs = self._store.load_functions()
        self._saved_funcs = [dict(f) for f in self._funcs]
        self._loading_func = True
        self.func_list.clear()
        for f in self._funcs:
            self.func_list.addItem(self._func_item_text(f))
        self.func_list.setCurrentRow(0 if self._funcs else -1)
        self._loading_func = False
        self._show_function(self._funcs[0] if self._funcs else None,
                            0 if self._funcs else -1)
        self.func_status.setText(
            f"共 {len(self._funcs)} 个函数；改完自动保存。")

    @staticmethod
    def _func_item_text(f: Dict[str, str]) -> str:
        lang = "JS" if str(f.get("lang")).lower() == "javascript" else "Python"
        return f"{f.get('name') or '（未命名）'}（{lang}）"

    def _on_func_selected(self):
        if self._loading_func:
            return
        self._save_functions()          # 先把上一个函数正在编辑的内容存下来
        row = self.func_list.currentRow()
        self._show_function(
            self._funcs[row] if 0 <= row < len(self._funcs) else None, row)

    def _show_function(self, f: Optional[Dict[str, str]], row: int = -1):
        """把某个函数显示到右边表单（None＝没有可编辑的函数）。"""
        self._func_row = row
        self._loading_func = True
        has = f is not None
        for w in (self.func_name, self.func_params, self.func_desc,
                  self.func_lang, self.func_code, self.func_save_btn):
            w.setEnabled(has)
        self.func_name.setText((f or {}).get("name", ""))
        idx = self.func_lang.findData((f or {}).get("lang", "python"))
        self.func_lang.setCurrentIndex(max(0, idx))
        self.func_params.setText((f or {}).get("params", ""))
        self.func_desc.setText((f or {}).get("desc", ""))
        self.func_code.setPlainText((f or {}).get("code", ""))
        self._loading_func = False

    def _on_func_code_changed(self):
        if self._loading_func:
            return
        self._func_timer.start()

    def _collect_function(self) -> bool:
        """把右边表单写回 self._funcs；返回有没有改动。"""
        if self._loading_func or not (0 <= self._func_row < len(self._funcs)):
            return False
        new = {
            "name": self.func_name.text().strip(),
            "lang": self.func_lang.currentData() or "python",
            "params": self.func_params.text().strip(),
            "desc": self.func_desc.text().strip(),
            "code": self.func_code.toPlainText(),
        }
        if new == self._funcs[self._func_row]:
            return False
        self._funcs[self._func_row] = new
        return True

    def _save_functions(self):
        """写盘（顺便把「改了名字」同步到所有「调用函数」节点）。"""
        if self._store is None:
            return
        if self._func_timer.isActive():
            self._func_timer.stop()
        changed = self._collect_function()
        if not changed and self._funcs == self._saved_funcs:
            return
        row = self._func_row
        old_name = ""
        if 0 <= row < len(self._saved_funcs):
            old_name = str(self._saved_funcs[row].get("name") or "")
        new_name = str(self._funcs[row].get("name") or "") if row >= 0 else ""
        self._store.save_functions(self._funcs)
        note = ""
        if old_name and new_name and old_name != new_name:
            note = self._rename_in_steps(old_name, new_name)
        self._saved_funcs = [dict(f) for f in self._funcs]
        # 列表上的名字 / 语言跟着变
        if 0 <= row < self.func_list.count():
            self._loading_func = True
            self.func_list.item(row).setText(self._func_item_text(self._funcs[row]))
            self._loading_func = False
        self.func_status.setText(
            f"已保存 {len(self._funcs)} 个函数"
            + (f"；{note}" if note else "")
            + "。调用它的节点会自动用最新代码。"
        )

    def _rename_in_steps(self, old: str, new: str) -> str:
        """函数改名：把流程里所有调用它的节点一起改掉。"""
        steps = self._store.load_steps()
        hit = 0
        for s in steps:
            if s.action == "call" and (s.func_name or "").strip() == old:
                s.func_name = new
                hit += 1
        if not hit:
            return ""
        self._store.save(steps)
        self._changed_project = self._store.name
        return f"{hit} 个「调用函数」节点已改成新名字"

    def _add_function(self):
        if self._store is None:
            return
        self._save_functions()
        base = "新函数"
        names = {f.get("name") for f in self._funcs}
        name, i = base, 2
        while name in names:
            name, i = f"{base}{i}", i + 1
        self._funcs.append({"name": name, "lang": "python", "params": "",
                            "code": "", "desc": ""})
        self._store.save_functions(self._funcs)
        self._saved_funcs = [dict(f) for f in self._funcs]
        self._loading_func = True
        self.func_list.addItem(self._func_item_text(self._funcs[-1]))
        self._loading_func = False
        self.func_list.setCurrentRow(len(self._funcs) - 1)   # 触发 _on_func_selected
        self.func_name.setFocus()
        self.func_name.selectAll()
        self.func_status.setText("已新建一个空函数：先起名字、写参数和代码。")

    def _del_function(self):
        if self._store is None:
            return
        row = self.func_list.currentRow()
        if not (0 <= row < len(self._funcs)):
            return
        name = self._funcs[row].get("name") or ""
        users = [s for s in self._store.load_steps()
                 if s.action == "call" and (s.func_name or "").strip() == name]
        msg = f"删除函数「{name}」？"
        if users:
            msg += (f"\n\n注意：流程里有 {len(users)} 个「调用函数」节点在用它，"
                    "删掉后那些节点运行时会报「找不到函数」，需要重新选一个。")
        if QMessageBox.question(
                self, "删除函数", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._loading_func = True          # 别让切换事件把已删的又写回去
        del self._funcs[row]
        self._func_row = -1
        self._loading_func = False
        self._store.save_functions(self._funcs)
        self._saved_funcs = [dict(f) for f in self._funcs]
        self.func_list.blockSignals(True)
        self.func_list.takeItem(row)
        self.func_list.blockSignals(False)
        if self._funcs:
            self.func_list.setCurrentRow(min(row, len(self._funcs) - 1))
            self._on_func_selected()
        else:
            self._show_function(None)
        self.func_status.setText(f"已删除函数「{name}」。")

    def _load_images(self):
        """刷新图片库：列出 img/ 里的图片 + 各自被哪些步骤引用。"""
        if self._store is None:
            return
        steps = self._store.load_steps()
        usage = self._image_usage(steps)
        files: List[Path] = []
        if self._store.img_dir.exists():
            files = sorted(
                (p for p in self._store.img_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in IMG_EXTS),
                key=lambda p: p.name.lower(),
            )

        self._loading = True
        self.img_table.setRowCount(0)
        unused = 0
        for p in files:
            rel = f"img/{p.name}"
            users = usage.get(rel.lower(), [])
            if not users:
                unused += 1
            pix = QPixmap(str(p))
            size = (f"{pix.width()} × {pix.height()}"
                    if not pix.isNull() else "读不出")
            self._append_image_row(p.name, size, self._human_size(p), users)
        self._loading = False
        self._show_image_preview()
        self.img_status.setText(
            f"共 {len(files)} 张图" + (f"，其中 {unused} 张没被任何步骤用到" if unused else "")
        )

    def _append_image_row(self, name: str, size: str, human: str,
                          users: List[str]):
        row = self.img_table.rowCount()
        self.img_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, name)
        self.img_table.setItem(row, COL_IMG, name_item)
        for col, text in ((COL_IMG_SIZE, size), (COL_IMG_BYTES, human)):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.img_table.setItem(row, col, item)
        use_item = QTableWidgetItem("、".join(users) if users else "（没被用到）")
        use_item.setFlags(use_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        use_item.setToolTip("\n".join(users) if users else "没有任何步骤引用这张图")
        self.img_table.setItem(row, COL_IMG_USE, use_item)

    @staticmethod
    def _image_usage(steps: List[Step]) -> Dict[str, List[str]]:
        """图片 → 引用它的步骤（定位方式＝截图的，或 XPath 的兜底截图）。

        键统一成小写、斜杠统一的 img/xxx 形式，避免 Windows 上大小写不一致。
        """
        usage: Dict[str, List[str]] = {}

        def add(path: str, label: str):
            key = path.replace("\\", "/").strip().lower()
            if not key:
                return
            usage.setdefault(key, []).append(label)

        for s in steps:
            loc = s.locator
            if loc is None:
                continue
            who = f"{s.id}. {s.title or s.action}"
            if loc.type == "image" and loc.value:
                add(loc.value, who)
            if loc.image:
                add(loc.image, who)
        return usage

    @staticmethod
    def _human_size(path: Path) -> str:
        try:
            n = float(path.stat().st_size)
        except OSError:
            return ""
        for unit in ("B", "KB", "MB"):
            if n < 1024 or unit == "MB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} MB"

    def _selected_image(self) -> Optional[str]:
        """选中的图片文件名（没选中返回 None）。"""
        rows = {i.row() for i in self.img_table.selectedIndexes()}
        if len(rows) != 1:
            return None
        item = self.img_table.item(rows.pop(), COL_IMG)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _show_image_preview(self):
        name = self._selected_image()
        if not name or self._store is None:
            self.img_preview.setPixmap(QPixmap())
            self.img_preview.setText("选中一张图，这里看大图")
            return
        pix = QPixmap(str(self._store.img_dir / name))
        if pix.isNull():
            self.img_preview.setPixmap(QPixmap())
            self.img_preview.setText("（这张图读不出来）")
            return
        box = self.img_preview.size()
        self.img_preview.setPixmap(pix.scaled(
            max(120, box.width() - 12), max(120, box.height() - 12),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _import_images(self):
        """把外面的图片拷进 img/（重名自动加后缀）。"""
        if self._store is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的图片", str(Path.home()),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)",
        )
        if not paths:
            return
        self._store.img_dir.mkdir(parents=True, exist_ok=True)
        added, skipped = [], []
        for src in paths:
            src_path = Path(src)
            if src_path.suffix.lower() not in IMG_EXTS:
                skipped.append(src_path.name)
                continue
            dst = self._store.img_dir / src_path.name
            i = 1
            while dst.exists():
                dst = self._store.img_dir / f"{src_path.stem}_{i}{src_path.suffix}"
                i += 1
            try:
                shutil.copy2(src_path, dst)
                added.append(dst.name)
            except OSError as e:
                skipped.append(f"{src_path.name}（{e}）")
        self._load_images()
        msg = f"已导入 {len(added)} 张：{'、'.join(added)}" if added else "没有导入任何图片"
        if skipped:
            msg += f"；跳过 {len(skipped)} 个：{'、'.join(skipped)}"
        self.img_status.setText(msg)

    def _replace_image(self):
        """用另一张图覆盖选中的那张（文件名不变，引用它的步骤不用改）。"""
        if self._store is None:
            return
        name = self._selected_image()
        if not name:
            QMessageBox.information(self, "提示", "请先在表里选中一行要替换的图片。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"用哪张图替换 {name}", str(Path.home()),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)",
        )
        if not path:
            return
        src = Path(path)
        if src.suffix.lower() not in IMG_EXTS:
            QMessageBox.warning(self, "格式不支持", "请选择 png/jpg/bmp/webp/gif 图片。")
            return
        reply = QMessageBox.question(
            self, "确认替换",
            f"会用这张图覆盖 {name}：\n{src}\n\n"
            "文件名不变，所以引用它的步骤不用改；原来的图会被覆盖掉。\n确定吗？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(src, self._store.img_dir / name)
        except OSError as e:
            QMessageBox.critical(self, "替换失败", f"覆盖图片失败：\n{e}")
            return
        self._load_images()
        self.img_status.setText(f"已用新图覆盖 {name}")

    def _delete_images(self):
        """删除选中的图片；被步骤引用的会先提醒。"""
        if self._store is None:
            return
        rows = sorted({i.row() for i in self.img_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先在表里选中要删除的图片。")
            return
        names = []
        used_lines = []
        for r in rows:
            item = self.img_table.item(r, COL_IMG)
            if item is None:
                continue
            name = item.data(Qt.ItemDataRole.UserRole) or item.text()
            names.append(name)
            use_item = self.img_table.item(r, COL_IMG_USE)
            users = (use_item.toolTip() if use_item else "") or ""
            if users and "没被用到" not in users:
                used_lines.append(f"· {name} ← {users.replace(chr(10), '、')}")
        msg = f"确定删除这 {len(names)} 张图吗？\n" + "、".join(names)
        if used_lines:
            msg += ("\n\n注意：下面这些图正在被步骤使用，删了以后"
                    "那些步骤运行时会报「截图文件不存在」：\n" + "\n".join(used_lines))
        reply = QMessageBox.warning(
            self, "确认删除图片", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        failed = []
        for name in names:
            try:
                (self._store.img_dir / name).unlink()
            except OSError as e:
                failed.append(f"{name}（{e}）")
        self._load_images()
        self.img_status.setText(
            f"已删除 {len(names) - len(failed)} 张图"
            + (f"；失败 {len(failed)} 个：{'、'.join(failed)}" if failed else "")
        )

    # ------------------------------
    # 项目列表
    # ------------------------------
    def _reload(self, select_name: str = ""):
        self._stores = list_projects()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for s in self._stores:
            steps = s.load_steps()
            item = QListWidgetItem(f"{s.name}（{len(steps)} 步）")
            item.setData(Qt.ItemDataRole.UserRole, s.name)
            # 列表宽度有限，名字长会被省略号截掉——完整信息放悬停提示里
            item.setToolTip(
                f"{s.name}\n{len(steps)} 步 ｜ {s.image_count()} 张截图 ｜ "
                + ("桌面应用" if s.is_desktop else "网页自动化")
                + "\n双击打开这个项目"
            )
            self.list_widget.addItem(item)
            if s.name == select_name:
                item.setSelected(True)
        self.list_widget.blockSignals(False)
        self._on_selection_changed()

    def _selected_stores(self) -> List[ProjectStore]:
        names = {
            it.data(Qt.ItemDataRole.UserRole)
            for it in self.list_widget.selectedItems()
        }
        return [s for s in self._stores if s.name in names]

    def _on_selection_changed(self):
        stores = self._selected_stores()
        single = len(stores) == 1
        self.btn_open.setEnabled(single)
        self.btn_delete.setEnabled(len(stores) > 0)
        self.tabs.setEnabled(single)
        for w in (self.var_table, self.btn_var_add, self.btn_var_del,
                  self.btn_locator_add, self.img_table, self.btn_img_import,
                  self.btn_img_replace, self.btn_img_delete):
            w.setEnabled(single)
        one = stores[0] if single else None
        self._store = one
        if one is None:
            self.header_label.setText(
                "在左边选中**一个**项目，这里就是它的配置"
            )
            self._loading = True
            self.var_table.setRowCount(0)
            self.img_table.setRowCount(0)
            self._loading = False
            self.data_label.setText("")
            self.status_label.clear()
            self.img_status.clear()
            self._load_functions()      # 清空函数库那页（没有选中项目）
            self._show_image_preview()
            self.auth_panel.set_project(None, None)
            self.data_panel.set_project(None)
            return
        self.header_label.setText(
            f"项目：{one.name}　｜　场景："
            + ("桌面应用（截图定位 + 鼠标键盘）" if one.is_desktop
               else "网页自动化（浏览器）")
        )
        # 桌面项目没有浏览器，也就没有登录态可言
        self.tabs.setTabEnabled(TAB_AUTH, not one.is_desktop)
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, index: int):
        """切页签 / 换项目时刷新当前页，保证看到的都是同一份最新数据。"""
        if self._store is None:
            return
        if index == TAB_VARS:
            self._load_var_list()
        elif index == TAB_IMAGES:
            self._load_images()
        elif index == TAB_AUTH:
            self.auth_panel.set_project(self._store, self._store.load_steps())
        elif index == TAB_FUNCS:
            self._load_functions()
        else:
            self.data_panel.set_project(self._store)

    # ------------------------------
    # 变量清单
    # ------------------------------
    def _load_var_list(self):
        """刷新变量清单：读取 / 采集节点产出的 + 元素定位 + 自定义变量。"""
        if self._store is None:
            return
        steps = self._store.load_steps()
        variables = self._store.load_variables()
        locators = self._store.load_locators()

        self._loading = True
        self.var_table.setRowCount(0)
        readers = [s for s in steps if s.action == "read_data"]
        collectors = [s for s in steps
                      if s.action == "collect" and (s.output_var or "").strip()]
        for s in readers:
            self._append_data_node(s)
        for s in collectors:
            self._append_collect_node(s)
        for name, xpath in locators.items():
            self._append_row(name, xpath, "元素定位", KIND_LOCATOR)
        for k, v in variables.items():
            self._append_row(k, v, "自定义创建", KIND_PROJECT)
        self._loading = False
        self.data_label.setText(self._data_summary(readers))
        status = (f"{len(readers)} 个读取节点、{len(collectors)} 个采集节点、"
                  f"{len(locators)} 个元素定位、{len(variables)} 个自定义变量")
        dups = sorted(set(locators) & set(variables))
        if dups:
            status += (f"；注意 {'、'.join(dups)} 既是元素定位又是自定义变量"
                       "（运行时按元素定位取值，建议改掉一个）")
        self._set_status(status)

    def _append_data_node(self, node: Step):
        """一个「读取数据」节点：列出它产出的变量与读到的每个文件字段。

        这些都是展示，不能在这里改名/删除——改名请点【来源】跳进那个节点改。
        """
        cfg = DataSourceConfig.from_dict(node.data_cfg or {})
        var = (node.output_var or "").strip()
        where = Path(cfg.path).name if cfg.path else "（未选路径）"
        self._append_row(
            var or "（未填产出变量名）", PLACEHOLDER,
            f"读取节点：{where}\n循环节点里填 {{{{{var}}}}} 就能逐项遍历",
            KIND_DATA, node.id,
        )
        for m in (node.data_cfg or {}).get("field_map") or []:
            if not isinstance(m, dict):
                continue
            field = (m.get("var") or "").strip()
            if not field:
                continue
            self._append_row(
                f"loop.item.{field}", PLACEHOLDER,
                f"读取节点：{where} 的字段（每个文件一项，读的是 "
                f"{self._source_label(cfg.type, m.get('field', ''))}）",
                KIND_DATA, node.id,
            )

    def _append_collect_node(self, node: Step):
        """一个「采集数据」节点：列出它产出的变量与每个字段，同样只读展示。"""
        var = (node.output_var or "").strip()
        is_list = (node.collect_mode or "page") == "list"
        mode = "列表采集（每行一条）" if is_list else "采当前页面（一条记录）"
        fields = [str(f.get("name") or "").strip()
                  for f in (node.collect_fields or [])
                  if isinstance(f, dict)]
        fields = [f for f in fields if f]
        source = f"采集节点：{mode}，{len(fields)} 个字段"
        tip = (f"循环节点里填 {{{{{var}}}}} 就能逐项遍历" if is_list
               else "每条记录都自动带 _time / _url / _step")
        self._append_row(var, PLACEHOLDER, f"{source}\n{tip}", KIND_DATA, node.id)
        prefix = "loop.item." if is_list else f"{var}."
        for field in fields:
            self._append_row(
                f"{prefix}{field}", PLACEHOLDER,
                f"采集节点：{mode}的字段「{field}」→ 数据也在 data/ 里",
                KIND_DATA, node.id,
            )

    @staticmethod
    def _source_label(cfg_type: str, fld: str) -> str:
        """字段来源的中文说法（文件类显示中文字段名）。"""
        key = data_sources.resolve_src_key(cfg_type, fld)
        if key.startswith("file."):
            return dict(data_sources.FILE_FIELDS).get(key[5:], key[5:])
        return key[5:] if key.startswith("row.") else key

    @staticmethod
    def _data_summary(readers: List[Step]) -> str:
        """读取节点概况：讲清「循环为什么跑 N 次」。"""
        if not readers:
            return ("这个项目还没有「读取数据」节点："
                    "需要读文件/文件夹时，在【流程编辑…】里新增一个。")
        lines = []
        for s in readers:
            cfg = DataSourceConfig.from_dict(s.data_cfg or {})
            name = Path(cfg.path).name if cfg.path else "（未选路径）"
            where = cfg.path or ""
            if cfg.type == "folder":
                where += f"（通配 {cfg.pattern}{'，含子文件夹' if cfg.recursive else ''}）"
            lines.append(f"「{s.output_var or '未命名'}」← {name} {where}")
        return "\n".join(lines) + "\n循环节点里填 {{变量名}}，就按它读到的项数跑那么多次。"

    def _append_row(self, name: str, value: str, source: str, kind: str,
                    node_id: int = 0):
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)

        name_item = QTableWidgetItem(name)
        if kind == KIND_DATA:       # 读取 / 采集节点产出的变量不让在这里改
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_NAME, name_item)

        value_item = QTableWidgetItem(value)
        if kind not in (KIND_PROJECT, KIND_LOCATOR):    # 节点产出的是运行时填的
            value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        value_item.setToolTip(
            value if kind != KIND_LOCATOR else f"XPath：{value}")
        self.var_table.setItem(row, COL_VALUE, value_item)

        src_item = QTableWidgetItem(source)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        src_item.setData(Qt.ItemDataRole.UserRole, kind)
        src_item.setData(Qt.ItemDataRole.UserRole + 1, node_id)
        src_item.setToolTip(
            source + "\n（点一下可以跳进这个节点改名 / 换来源）"
            if kind == KIND_DATA else source
        )
        self.var_table.setItem(row, COL_SRC, src_item)

    def _row_kind(self, row: int) -> str:
        item = self.var_table.item(row, COL_SRC)
        return (item.data(Qt.ItemDataRole.UserRole) if item else "") or ""

    def _row_node_id(self, row: int) -> int:
        item = self.var_table.item(row, COL_SRC)
        return int(item.data(Qt.ItemDataRole.UserRole + 1) or 0) if item else 0

    def _on_var_changed(self, item: QTableWidgetItem):
        """改名 / 改值 → 立即保存（自定义变量与元素定位都算）。"""
        if self._loading or item.column() not in (COL_NAME, COL_VALUE):
            return
        kind = self._row_kind(item.row())
        if kind == KIND_PROJECT:
            self._save_variables()
        elif kind == KIND_LOCATOR:
            self._save_locators()

    def _on_var_clicked(self, row: int, col: int):
        """点「来源」列：跳进那个「读取数据」/「采集数据」节点去改配置。"""
        if col != COL_SRC or self._row_kind(row) != KIND_DATA:
            return
        node_id = self._row_node_id(row)
        if node_id:
            self._edit_reader_node(node_id)

    def _edit_reader_node(self, node_id: int):
        """打开产出变量的那个节点（读取 / 采集）；改完写回，顺带修引用。"""
        if self._store is None:
            return
        steps = self._store.load_steps()
        idx = next((i for i, s in enumerate(steps) if s.id == node_id), -1)
        if idx < 0:
            return
        old = steps[idx]
        dlg = StepEditDialog(
            self._store.dir, old, self,
            variable_names=step_executor.available_variables(
                steps, self._store.load_all_variables()),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_step = dlg.get_step()
        new_step.pos = old.pos
        steps[idx] = new_step
        notes = project_store.rename_field_refs(steps, old, new_step, skip=idx)
        for i, s in enumerate(steps, start=1):
            s.id = i
        self._store.save(steps)
        self._changed_project = self._store.name
        self._load_var_list()
        self._set_status(
            "已保存；" + "；".join(notes) if notes
            else "已保存（引用没变，不用改别处）"
        )

    def _add_var_row(self):
        if self._store is None:
            return
        self._loading = True
        self._append_row("", "", "自定义创建", KIND_PROJECT)
        self._loading = False
        row = self.var_table.rowCount() - 1
        self.var_table.setCurrentCell(row, COL_NAME)
        self.var_table.editItem(self.var_table.item(row, COL_NAME))

    def _add_locator_row(self):
        """加一条「元素定位」：名字 → XPath（步骤的定位里写 {{名字}} 复用）。"""
        if self._store is None:
            return
        self._loading = True
        self._append_row("", "", "元素定位", KIND_LOCATOR)
        self._loading = False
        row = self.var_table.rowCount() - 1
        self.var_table.setCurrentCell(row, COL_NAME)
        self.var_table.editItem(self.var_table.item(row, COL_NAME))

    def _save_variables(self):
        """立即保存「自定义创建」的变量。"""
        if self._store is None:
            return
        variables: Dict[str, str] = {}
        for r in range(self.var_table.rowCount()):
            if self._row_kind(r) != KIND_PROJECT:
                continue
            name = self.var_table.item(r, COL_NAME)
            value = self.var_table.item(r, COL_VALUE)
            key = name.text().strip() if name else ""
            if key:
                variables[key] = value.text() if value else ""
        self._store.save_variables(variables)
        self._set_status(f"已保存 {len(variables)} 个自定义变量")

    def _save_locators(self):
        """立即保存「元素定位」；没填 XPath 的行丢掉（只写了名字还不算数）。"""
        if self._store is None:
            return
        locators: Dict[str, str] = {}
        for r in range(self.var_table.rowCount()):
            if self._row_kind(r) != KIND_LOCATOR:
                continue
            name = self.var_table.item(r, COL_NAME)
            value = self.var_table.item(r, COL_VALUE)
            key = name.text().strip() if name else ""
            xpath = value.text().strip() if value else ""
            if key and xpath:
                locators[key] = xpath
        self._store.save_locators(locators)
        dup = sorted(set(locators) & set(self._store.load_variables()))
        self._set_status(
            f"已保存 {len(locators)} 个元素定位"
            + (f"；注意 {'、'.join(dup)} 与自定义变量重名，运行时按元素定位取值" if dup else "")
        )

    def _del_var_rows(self):
        """删除选中行：元素定位与自定义变量能删；节点产出的不让删。"""
        if self._store is None:
            return
        rows = sorted({i.row() for i in self.var_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        if any(self._row_kind(r) == KIND_DATA for r in rows):
            QMessageBox.information(
                self, "提示",
                "「读取 / 采集节点」的变量不在这里删：\n"
                "点它那一行的【来源】跳进节点，把对应字段删掉就行。",
            )
        editable = [r for r in rows
                    if self._row_kind(r) in (KIND_PROJECT, KIND_LOCATOR)]
        if editable:
            self._loading = True
            for r in editable:
                self.var_table.removeRow(r)
            self._loading = False
            self._save_variables()
            self._save_locators()
        self._load_var_list()

    def _set_status(self, text: str):
        self.status_label.setText(text)

    # ------------------------------
    # 删除项目 / 打开项目
    # ------------------------------
    def _delete_selected(self):
        stores = self._selected_stores()
        if not stores:
            return
        lines = []
        total_imgs = 0
        for s in stores:
            n = s.image_count()
            total_imgs += n
            lines.append(f"· {s.name}（{len(s.load_steps())} 步，{n} 张截图）")
        msg = (
            f"确定删除以下 {len(stores)} 个项目吗？\n"
            f"整个项目文件夹（含 steps.json 和 {total_imgs} 张截图）都会被删除，"
            f"且不可恢复：\n\n" + "\n".join(lines)
        )
        reply = QMessageBox.warning(
            self, "确认删除项目", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for s in stores:
            s.delete()
        keep_current = self._open_name not in {s.name for s in stores}
        self._reload()
        if not keep_current:
            self._open_name = None

    def _open_selected(self, *_):
        stores = self._selected_stores()
        if len(stores) != 1:
            QMessageBox.warning(self, "提示", "请选择单个项目再打开。")
            return
        self._open_name = stores[0].name
        self.accept()

    @property
    def open_project_name(self) -> Optional[str]:
        return self._open_name

    @property
    def changed_project_name(self) -> Optional[str]:
        """在这个弹窗里改过步骤的项目（主界面据此决定要不要重新载入）。"""
        return self._changed_project
