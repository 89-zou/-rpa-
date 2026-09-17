# -*- coding: utf-8 -*-
"""项目管理对话框：项目列表 + 变量清单。

左边选项目，右边看这个项目的变量从哪来：

- 变量有两个来源——「读取数据」节点产出的列表变量（读文件/文件夹），
  以及手工加的自定义变量（账号密码之类）；
- 循环里的 {{loop.item}} / {{loop.index}} 是运行时自动有的，不在这里列，
  也不用手工配置（写错会在运行前检查里提示）；
- 自定义变量可以增删改，改动立即保存，没有「保存」按钮；
  读取节点产出的变量要改名/换路径，请去画布上双击那个「读取数据」节点。
"""
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from smart_tool.core import data_sources, project_store, step_executor
from smart_tool.core.data_sources import DataSourceConfig
from smart_tool.core.project_store import ProjectStore, Step, list_projects
from smart_tool.ui.step_editor_dialog import StepEditDialog

# 变量行类型（存在「来源」列的 UserRole 里，用来区分增删改行为）
KIND_DATA, KIND_PROJECT = "data", "project"

COL_NAME, COL_VALUE, COL_SRC = range(3)
PLACEHOLDER = "（运行时按项填充）"


class ProjectManagerDialog(QDialog):
    """项目管理：项目列表 + 变量清单。"""

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

        # ---- 右：变量清单 ----
        right = QVBoxLayout()
        self.header_label = QLabel("在左边选中一个项目，这里就是它的变量清单")
        self.header_label.setStyleSheet("color: #444; font-weight: bold;")
        right.addWidget(self.header_label)
        right.addWidget(self._build_var_page(), 1)

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

        tip = QLabel(
            "步骤里用 {{变量名}} 引用。变量只有两个来源：\n"
            "· 读取节点 ＝「读取数据」节点从文件/文件夹读到的："
            "第一行是它产出的列表变量（如 数据列表），"
            "下面几行是每个文件的字段（如 loop.item.标题）。\n"
            "  这两类都是**只读展示**，不能在这里改或删；"
            "点某行的【来源】就能跳进那个节点，换文件夹、改字段名都在那里做"
            "（改名后别处的引用会自动跟着改）。\n"
            "· 自定义创建 ＝ 手工加的（账号密码之类），可增删改，改完立即保存。\n"
            "循环里的 {{loop.index}}（第几轮）不用配置。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #777;")
        lay.addWidget(tip)

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
        self.var_table.setEnabled(single)
        self.btn_var_add.setEnabled(single)
        self.btn_var_del.setEnabled(single)
        one = stores[0] if single else None
        self._store = one
        if one is None:
            self.header_label.setText(
                "在左边选中**一个**项目，这里就是它的变量清单"
            )
            self.data_label.setText("")
            self._loading = True
            self.var_table.setRowCount(0)
            self._loading = False
            self.status_label.clear()
            return
        self.header_label.setText(f"项目：{one.name}")
        self._load_var_list()

    # ------------------------------
    # 变量清单
    # ------------------------------
    def _load_var_list(self):
        """刷新变量清单：读取节点产出的变量 + 自定义变量。"""
        if self._store is None:
            return
        steps = self._store.load_steps()
        variables = self._store.load_variables()

        self._loading = True
        self.var_table.setRowCount(0)
        readers = [s for s in steps if s.action == "read_data"]
        for s in readers:
            self._append_data_node(s)
        for k, v in variables.items():
            self._append_row(k, v, "自定义创建", KIND_PROJECT)
        self._loading = False
        self.data_label.setText(self._data_summary(readers))
        self._set_status(
            f"{len(readers)} 个读取节点、{len(variables)} 个自定义变量"
        )

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
        if kind == KIND_DATA:       # 读取节点产出的变量不让在这里改
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_NAME, name_item)

        value_item = QTableWidgetItem(value)
        if kind != KIND_PROJECT:    # 读取节点的值是运行时填的，不给改
            value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_VALUE, value_item)

        src_item = QTableWidgetItem(source)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        src_item.setData(Qt.ItemDataRole.UserRole, kind)
        src_item.setData(Qt.ItemDataRole.UserRole + 1, node_id)
        src_item.setToolTip(
            source + "\n（点一下可以跳进这个「读取数据」节点改名 / 换文件夹）"
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
        """改名 / 改值 → 立即保存（只对自定义变量生效）。"""
        if self._loading or item.column() not in (COL_NAME, COL_VALUE):
            return
        if self._row_kind(item.row()) != KIND_PROJECT:
            return
        self._save_variables()

    def _on_var_clicked(self, row: int, col: int):
        """点「来源」列：跳进那个「读取数据」节点（改名 / 换文件夹都在那做）。"""
        if col != COL_SRC or self._row_kind(row) != KIND_DATA:
            return
        node_id = self._row_node_id(row)
        if node_id:
            self._edit_reader_node(node_id)

    def _edit_reader_node(self, node_id: int):
        """打开「读取数据」节点；改完写回，并把它改名导致的引用一起改掉。"""
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
                steps, self._store.load_variables()),
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
        """删除选中变量：自定义的删掉；读取节点产出的不让删。"""
        if self._store is None:
            return
        rows = sorted({i.row() for i in self.var_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        if any(self._row_kind(r) == KIND_DATA for r in rows):
            QMessageBox.information(
                self, "提示",
                "「读取节点」的变量不在这里删：\n"
                "点它那一行的【来源】跳进节点，把对应字段的勾去掉就行。",
            )
        project_rows = [r for r in rows if self._row_kind(r) == KIND_PROJECT]
        if project_rows:
            self._loading = True
            for r in project_rows:
                self.var_table.removeRow(r)
            self._loading = False
            self._save_variables()
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
