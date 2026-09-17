# -*- coding: utf-8 -*-
"""项目管理对话框：项目列表 + 数据源与字段 + 变量清单。

左边选项目，右边编辑这个项目：

- 页签【数据源与字段】：选文件/文件夹 → 读取预览 → 勾选要保存的变量。
  这一步就是「变量从哪来」的初步确定（原来的【数据源设置】）。
- 页签【变量清单】：一张表看全部变量，来源写得清清楚楚——
  来自文件的显示文件地址（点一下可以重新选地址＝清空重选）；
  运行时变量显示它依托哪个循环产生；手写的显示「自定义创建」。
  除运行时变量外都能增删改，改动立即保存，没有「保存」按钮。

（原来独立的【数据源设置】弹窗已并入这里，主界面只留一个【项目管理…】）
"""
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from smart_tool.core import data_sources
from smart_tool.core.data_sources import DataSourceConfig, list_columns
from smart_tool.core.project_store import ProjectStore, list_projects
from smart_tool.ui.data_source_dialog import DataSourcePanel

# 变量行类型（存在「来源」列的 UserRole 里，用来区分增删改行为）
KIND_DATA, KIND_LOOP, KIND_PROJECT = "data", "loop", "project"

COL_NAME, COL_VALUE, COL_SRC = range(3)
# 循环自动注入的变量（不需要配置，循环里天然就有值）
LOOP_VARS = (
    ("loop.index", "第几轮（从 1 开始）"),
    ("loop.zero_index", "第几轮（从 0 开始）"),
    ("loop.item", "当前这一项（数据源循环＝当前这行）"),
)
RUNNING_FILLED = "（运行时逐行填充）"


class ProjectManagerDialog(QDialog):
    """项目管理：项目列表 + 数据源与字段 + 变量清单。"""

    def __init__(self, current_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("项目管理")
        self.setMinimumSize(1040, 660)
        self._stores: List[ProjectStore] = []
        self._open_name: Optional[str] = None
        self._store: Optional[ProjectStore] = None
        self._loading = False
        self.panel: Optional[DataSourcePanel] = None
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

        # ---- 右：数据源与字段 / 变量清单 ----
        right = QVBoxLayout()
        self.header_label = QLabel("在左边选中一个项目，这里就能编辑它的数据源与变量")
        self.header_label.setStyleSheet("color: #444; font-weight: bold;")
        right.addWidget(self.header_label)

        self.tabs = QTabWidget()
        self._tab1 = QWidget()
        t1 = QVBoxLayout(self._tab1)
        t1.setContentsMargins(0, 6, 0, 0)
        self.tab1_hint = QLabel("先在左边选中一个项目。")
        self.tab1_hint.setStyleSheet("color: #888;")
        t1.addWidget(self.tab1_hint)
        t1.addStretch()
        self.tabs.addTab(self._tab1, "数据源与字段")
        self.tabs.addTab(self._build_var_tab(), "变量清单")
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

    def _build_var_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)

        tip = QLabel(
            "步骤里用 {{变量名}} 引用。来源说明：\n"
            "· 文件地址 ＝ 这个变量从哪个文件/文件夹读出来（点它就能重新选地址）；\n"
            "· 运行时   ＝ 循环里自动产生，不用配置，也不能改；\n"
            "· 自定义创建 ＝ 手工加的变量（账号密码之类），来源不可改。\n"
            "除运行时变量外都能改名；改完立即保存。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #777;")
        lay.addWidget(tip)

        self.data_label = QLabel("")
        self.data_label.setWordWrap(True)
        self.data_label.setStyleSheet("color: #2f6fb3;")
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
        self.btn_var_add.clicked.connect(self._add_var_row)
        btns.addWidget(self.btn_var_add)
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
    # 项目列表
    # ------------------------------
    def _reload(self, select_name: str = ""):
        self._stores = list_projects()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for s in self._stores:
            item = QListWidgetItem(
                f"{s.name}    （{len(s.load_steps())} 步，{s.image_count()} 张截图）"
            )
            item.setData(Qt.ItemDataRole.UserRole, s.name)
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
        one = stores[0] if single else None
        self._store = one
        if one is None:
            self.header_label.setText(
                "在左边选中**一个**项目，这里就能编辑它的数据源与变量"
            )
            self.data_label.setText("")
            self._loading = True
            self.var_table.setRowCount(0)
            self._loading = False
            self.status_label.clear()
            return
        self.header_label.setText(f"项目：{one.name}")
        if self.panel is None:
            self.panel = DataSourcePanel(one, self)
            self.panel.changed.connect(self._load_var_list)
            self.tab1_hint.hide()
            self._tab1.layout().insertWidget(0, self.panel, 1)
        else:
            self.panel.load(one)
        self._load_var_list()

    def _on_tab_changed(self, index: int):
        """切页签时重新读一遍，保证两个页签看到的是同一份最新数据。"""
        if self._store is None:
            return
        if index == 0 and self.panel is not None:
            self.panel.load(self._store)
        elif index == 1:
            self._load_var_list()

    # ------------------------------
    # 变量清单
    # ------------------------------
    def _load_var_list(self):
        """刷新变量清单：数据源变量 + 运行时变量 + 自定义变量。"""
        if self._store is None:
            return
        ds = DataSourceConfig.from_dict(self._store.load_data_source())
        cols = list_columns(ds) if ds.configured else []
        variables = self._store.load_variables()
        src_text = ds.path or "（未设置数据源）"
        loop_from = self._loop_depends_on(ds)

        self._loading = True
        self.data_label.setText(self._data_summary(ds))
        self.var_table.setRowCount(0)
        for name in cols:
            self._append_row(name, RUNNING_FILLED, src_text, KIND_DATA)
        for name, meaning in LOOP_VARS:
            self._append_row(name, meaning, loop_from, KIND_LOOP)
        for k, v in variables.items():
            self._append_row(k, v, "自定义创建", KIND_PROJECT)
        self._loading = False
        self._set_status(
            f"{len(variables)} 个自定义变量、{len(cols)} 个数据源变量"
        )

    def _append_row(self, name: str, value: str, source: str, kind: str):
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)

        name_item = QTableWidgetItem(name)
        if kind == KIND_LOOP:       # 运行时变量不让改
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_NAME, name_item)

        value_item = QTableWidgetItem(value)
        if kind != KIND_PROJECT:    # 数据源的值是运行时填的，不给改
            value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_VALUE, value_item)

        src_item = QTableWidgetItem(source)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        src_item.setData(Qt.ItemDataRole.UserRole, kind)
        src_item.setToolTip(source)
        if kind == KIND_DATA:
            src_item.setToolTip(
                f"{source}\n点一下可以重新选数据文件/文件夹"
                "（原来的变量会清空，需要重新读取预览勾选）"
            )
        self.var_table.setItem(row, COL_SRC, src_item)

    def _row_kind(self, row: int) -> str:
        item = self.var_table.item(row, COL_SRC)
        return (item.data(Qt.ItemDataRole.UserRole) if item else "") or ""

    @staticmethod
    def _data_summary(ds: DataSourceConfig) -> str:
        """数据源概况：讲清「循环为什么跑 N 次」。"""
        if not ds.configured:
            return "还没有配置数据源：切到【数据源与字段】页签选文件/文件夹。"
        name = Path(ds.path).name or ds.path
        head = f"数据源：{name}"
        if ds.type == "folder":
            head += (f"（文件夹，通配 {ds.pattern}"
                     f"{'，含子文件夹' if ds.recursive else ''}）")
        elif ds.type == "txt":
            head += "（单个文本文件）"
        try:
            _rows, total, _cols = data_sources.preview(ds, 1)
        except Exception as e:
            return f"{head}\n读不出来：{str(e).splitlines()[0]}"
        unit = "个文件（每个文件一行）" if ds.type == "folder" else "行数据"
        return (f"{head}\n匹配到 {total} {unit}"
                f" → 循环方式选「数据源」时就跑 {total} 次")

    def _loop_depends_on(self, ds: DataSourceConfig) -> str:
        """运行时变量依托什么产生（给「来源」列写清楚）。"""
        loops = [s for s in self._store.load_steps() if s.action == "loop_start"]
        if len(loops) == 1:
            src = loops[0].loop_source or "data"
            if src == "range":
                name = f"索引范围 {loops[0].loop_range or '（未填）'}"
            elif src == "list":
                name = "变量 / 手动列表"
            else:
                name = (f"数据源 {Path(ds.path).name}" if ds.configured
                        else "数据源（未配置）")
            return f"运行时：跟随「{name}」逐行产生"
        if not loops:
            return "运行时：循环里自动有值（当前项目还没有循环）"
        return "运行时：循环里自动有值（本项目有多个循环）"

    def _on_var_changed(self, item: QTableWidgetItem):
        """改名 / 改值 → 立即保存。"""
        if self._loading or item.column() not in (COL_NAME, COL_VALUE):
            return
        row, kind = item.row(), self._row_kind(item.row())
        if kind == KIND_LOOP:
            return                      # 运行时变量不让改
        if kind == KIND_DATA:
            self._rename_data_var(row, item.text().strip())
            return
        self._save_variables()

    def _rename_data_var(self, row: int, new_name: str):
        """改数据源变量的名字 → 同步写回数据源的字段清单。"""
        if not new_name:
            self._load_var_list()
            return
        ds = DataSourceConfig.from_dict(self._store.load_data_source())
        items = [dict(m) for m in ds.field_map]
        # 第 row 行对应 field_map 的第 row 项（数据源行排在最前面）
        if 0 <= row < len(items):
            items[row]["var"] = new_name
            ds.field_map = items
            self._store.save_data_source(ds.to_dict())
            if self.panel is not None:
                self.panel.load(self._store)
            self._set_status(f"已改名：{new_name}")

    def _on_var_clicked(self, row: int, col: int):
        """点「来源」列：文件类的变量可以重新选地址（清空重选）。"""
        if col != COL_SRC or self._row_kind(row) != KIND_DATA:
            return
        if self.panel is None:
            return
        self.tabs.setCurrentIndex(0)        # 换地址在「数据源与字段」页签做
        if self.panel.pick_path():
            self._set_status("地址已改，原数据源变量已清空，请【读取预览】再勾一次")
        self._load_var_list()

    def _add_var_row(self):
        if self._store is None:
            return
        self._loading = True
        self._append_row("", "", "自定义创建", KIND_PROJECT)
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

    def _del_var_rows(self):
        """删除选中变量：自定义的删掉；数据源的＝不再保存这个字段；运行时的不让删。"""
        if self._store is None:
            return
        rows = sorted({i.row() for i in self.var_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        loop_rows = [r for r in rows if self._row_kind(r) == KIND_LOOP]
        if loop_rows:
            QMessageBox.information(
                self, "提示",
                "「运行时」变量是循环自动产生的，删不掉、也不用删。",
            )
        data_rows = [r for r in rows if self._row_kind(r) == KIND_DATA]
        project_rows = [r for r in rows if self._row_kind(r) == KIND_PROJECT]
        if data_rows:
            self._drop_data_vars(sorted(data_rows))
        if project_rows:
            self._loading = True
            for r in project_rows:
                self.var_table.removeRow(r)
            self._loading = False
            self._save_variables()
        self._load_var_list()

    def _drop_data_vars(self, rows: List[int]):
        """把这几行数据源变量从字段清单里去掉（＝不再保存这个变量）。"""
        ds = DataSourceConfig.from_dict(self._store.load_data_source())
        items = [dict(m) for m in ds.field_map]
        new_items = [m for i, m in enumerate(items) if i not in set(rows)]
        ds.field_map = new_items
        self._store.save_data_source(ds.to_dict())
        if self.panel is not None:
            self.panel.load(self._store)

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
