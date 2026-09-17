# -*- coding: utf-8 -*-
"""变量管理对话框（含项目列表 + 数据源）。

- 左侧：项目列表，支持多选批量删除、打开项目（新建项目在主界面【新建项目…】）
- 右侧：变量清单。三类：
    数据源变量 —— 来自【数据源…】勾选的字段（运行时逐行填充）
    数据源计数 —— 文件夹读出来的行数 / 文件数（解释「循环为什么跑 N 次」）
    循环自动变量 —— loop.index / loop.zero_index / loop.item（每轮自动有值）
    项目变量 —— 手工变量（可增删改）
  这些增删改都立即保存，无需点保存按钮。
- 【换数据文件夹…】：每天数据放在不同目录时，跑之前在这里点一下就行，
  选完直接写进项目的数据源路径。
"""
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from smart_tool.core import data_sources
from smart_tool.core.data_sources import DataSourceConfig, list_columns
from smart_tool.core.project_store import ProjectStore, list_projects

# 循环自动注入的变量（不需要配置，循环里天然就有值）
LOOP_VARS = (
    ("loop.index", "第几轮（从 1 开始）"),
    ("loop.zero_index", "第几轮（从 0 开始）"),
    ("loop.item", "当前这一项（数据源循环＝当前这行）"),
)


class ProjectManagerDialog(QDialog):
    """变量管理 + 项目列表。"""

    SOURCE_DATA = "数据源"
    SOURCE_PROJECT = "项目变量"
    SOURCE_COUNT = "数据源计数"
    SOURCE_LOOP = "循环自动注入"
    # 只看的行（不写进项目变量，也不能删）
    READONLY_SOURCES = (SOURCE_DATA, SOURCE_COUNT, SOURCE_LOOP)

    def __init__(self, current_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("变量管理")
        self.setMinimumSize(820, 540)
        self._stores: List[ProjectStore] = []
        self._open_name: Optional[str] = None
        self._loading = False
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
        root.addLayout(left, 1)

        # ---- 右：变量 ----
        right = QVBoxLayout()

        var_box = QGroupBox("变量（选中单个项目时可见）")
        var_layout = QVBoxLayout(var_box)
        tip = QLabel(
            "步骤里用 {{变量名}} 引用，如 {{标题}}、{{row.正文}}。\n"
            "「数据源」行＝【数据源…】里勾选的字段，运行时逐行填充；"
            "在这里删掉它＝不再保存该变量。\n"
            "「数据源计数」「循环自动注入」只是给你看的（不能改）："
            "循环方式选「数据源」时，数据源有几行就循环几次。\n"
            "「项目变量」是手工变量，可增删改，适合放账号密码这类固定值。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #777;")
        var_layout.addWidget(tip)

        # 数据源概况 + 换数据文件夹（每天换目录时在这里点一下）
        ds_row = QHBoxLayout()
        self.data_label = QLabel("")
        self.data_label.setWordWrap(True)
        self.data_label.setStyleSheet("color: #2f6fb3;")
        ds_row.addWidget(self.data_label, 1)
        self.btn_pick_folder = QPushButton("换数据文件夹…")
        self.btn_pick_folder.setToolTip(
            "选一个文件夹当数据源（默认递归读里面的 *.txt）。\n"
            "每天数据放在不同文件夹时，跑之前点一下就行，不用改项目。"
        )
        self.btn_pick_folder.clicked.connect(self._pick_data_folder)
        ds_row.addWidget(self.btn_pick_folder)
        var_layout.addLayout(ds_row)

        self.var_table = QTableWidget(0, 3)
        self.var_table.setHorizontalHeaderLabels(["变量名", "值", "来源"])
        header = self.var_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.var_table.itemChanged.connect(self._on_var_changed)
        var_layout.addWidget(self.var_table, 1)

        var_btns = QHBoxLayout()
        self.btn_var_add = QPushButton("添加变量")
        self.btn_var_add.clicked.connect(self._add_var_row)
        var_btns.addWidget(self.btn_var_add)
        self.btn_var_del = QPushButton("删除选中变量")
        self.btn_var_del.clicked.connect(self._del_var_rows)
        var_btns.addWidget(self.btn_var_del)
        var_btns.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #2e7d32;")
        var_btns.addWidget(self.status_label)
        var_layout.addLayout(var_btns)
        right.addWidget(var_box, 1)

        # 打开 / 关闭
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

        root.addLayout(right, 1)

    # ------------------------------
    # 列表
    # ------------------------------
    def _reload(self, select_name: str = ""):
        self._stores = list_projects()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for s in self._stores:
            n_steps = len(s.load_steps())
            n_img = s.image_count()
            item = QListWidgetItem(
                f"{s.name}    （{n_steps} 步，{n_img} 张截图）"
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
        if single:
            self._load_variables(stores[0])
        else:
            self._loading = True
            self.var_table.setRowCount(0)
            self._loading = False
            self.status_label.clear()

    # ------------------------------
    # 删除项目（二次确认，显示截图数）
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

    # ------------------------------
    # 变量：读取
    # ------------------------------
    def _load_variables(self, store: ProjectStore):
        """列出全部可用变量：数据源字段 + 数据源计数 + 循环变量 + 项目变量。"""
        data_cols = self._data_columns(store)
        variables = store.load_variables()
        info, counts = self._data_info(store)

        self._loading = True
        self.data_label.setText(info)
        self.var_table.setRowCount(0)
        for name in data_cols:
            note = "（每行由数据源填充）"
            if name in variables:
                note = "（每行由数据源填充，会盖掉下面的同名项目变量）"
            self._append_var_row(name, note, self.SOURCE_DATA, editable=False)
        for name, value in counts:
            self._append_var_row(name, value, self.SOURCE_COUNT, editable=False)
        for name, meaning in LOOP_VARS:
            self._append_var_row(name, meaning, self.SOURCE_LOOP, editable=False)
        for k, v in variables.items():
            mark = "（被同名数据源变量覆盖）" if k in data_cols else ""
            self._append_var_row(k, v, self.SOURCE_PROJECT + mark)
        self._loading = False
        self._set_status(f"{len(variables)} 个项目变量、{len(data_cols)} 个数据源变量")

    @staticmethod
    def _data_info(store: ProjectStore) -> Tuple[str, List[Tuple[str, str]]]:
        """数据源概况（讲清「循环为什么跑 N 次」）+ 计数字段的值。"""
        cfg = DataSourceConfig.from_dict(store.load_data_source())
        if not cfg.configured:
            return ("还没有配置数据源：点右边【换数据文件夹…】或上面的【数据源…】",
                    [])
        name = Path(cfg.path).name or cfg.path
        head = f"数据源：{name}"
        if cfg.type == "folder":
            head += (f"（文件夹，通配 {cfg.pattern}"
                     f"{'，含子文件夹' if cfg.recursive else ''}）")
        elif cfg.type == "txt":
            head += "（单个文本文件）"
        try:
            raw_rows, total, _cols = data_sources.preview(cfg, 1)
        except Exception as e:
            return f"{head}\n读不出来：{str(e).splitlines()[0]}", []
        unit = "个文件（每个文件一行）" if cfg.type == "folder" else "行数据"
        head += (f"\n匹配到 {total} {unit}"
                 f" → 循环方式选「数据源」时就跑 {total} 次")
        counts: List[Tuple[str, str]] = []
        if raw_rows:
            first = raw_rows[0]
            for key in ("file.total", "file.folder_count"):
                if first.get(key):
                    counts.append((key, first[key]))
        return head, counts

    def _pick_data_folder(self):
        """选一个文件夹当数据源（每天换目录时不用改项目设置）。"""
        stores = self._selected_stores()
        if len(stores) != 1:
            QMessageBox.warning(self, "提示", "请先在左边选中一个项目。")
            return
        store = stores[0]
        cur = (store.load_data_source() or {}).get("path") or ""
        start = cur if cur and Path(cur).is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择数据文件夹", start)
        if not folder:
            return
        ds = dict(store.load_data_source() or {})
        ds["type"] = "folder"
        ds["path"] = folder.replace("\\", "/")
        if not ds.get("pattern"):
            ds["pattern"] = "*.txt"
        if "recursive" not in ds:
            ds["recursive"] = True
        store.save_data_source(ds)
        self._load_variables(store)
        self._set_status(f"数据文件夹已切换：{folder}")

    @staticmethod
    def _data_columns(store: ProjectStore) -> list:
        """该项目的变量名清单（数据源挑选的变量 + 项目变量）。"""
        ds = store.load_data_source()
        if not ds:
            return []
        try:
            return list_columns(DataSourceConfig.from_dict(ds))
        except Exception:
            return []

    def _append_var_row(self, key: str, value: str, source: str,
                        editable: bool = True):
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)
        name_item = QTableWidgetItem(key)
        val_item = QTableWidgetItem(value)
        src_item = QTableWidgetItem(source)
        # 数据源行只读；来源列始终只读，作为行的身份标记
        if not editable:
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, 0, name_item)
        self.var_table.setItem(row, 1, val_item)
        self.var_table.setItem(row, 2, src_item)

    # ------------------------------
    # 变量：增 / 删 / 改（立即保存）
    # ------------------------------
    def _add_var_row(self):
        if not self._selected_stores():
            return
        self._loading = True
        self._append_var_row("", "", self.SOURCE_PROJECT)
        self._loading = False
        row = self.var_table.rowCount() - 1
        self.var_table.setCurrentCell(row, 0)
        self.var_table.editItem(self.var_table.item(row, 0))

    def _on_var_changed(self, item: QTableWidgetItem):
        """改名字或改值 → 立即保存。"""
        if self._loading or item.column() not in (0, 1):
            return
        self._save_variables()

    def _del_var_rows(self):
        """删除选中变量：项目变量直接删；数据源变量同步改数据源设置。"""
        stores = self._selected_stores()
        if len(stores) != 1:
            return
        rows = sorted({i.row() for i in self.var_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        project_rows, data_vars = [], []
        skipped = 0
        for r in rows:
            src = self._row_source(r)
            name = self.var_table.item(r, 0)
            if src == self.SOURCE_DATA:
                if name is not None and name.text().strip():
                    data_vars.append(name.text().strip())
            elif self._is_readonly_source(src):
                skipped += 1        # 计数 / 循环变量只是给你看的，删不了
            else:
                project_rows.append(r)

        if project_rows:
            self._loading = True
            for r in project_rows:
                self.var_table.removeRow(r)
            self._loading = False
            self._save_variables()
        if data_vars:
            self._remove_data_variables(stores[0], data_vars)
        elif skipped and not project_rows:
            self._set_status("「数据源计数」「循环自动注入」只是给你看的，不用删。")

    def _remove_data_variables(self, store: ProjectStore, names: List[str]):
        """把变量从数据源设置里去掉（＝不再保存这些变量）。"""
        cfg = DataSourceConfig.from_dict(store.load_data_source())
        failed = []
        for name in names:
            new_cfg = data_sources.without_variable(cfg, name)
            if new_cfg is None:
                failed.append(name)
            else:
                cfg = new_cfg
        if failed:
            QMessageBox.warning(
                self, "无法处理",
                "这些变量没法从数据源里删除（读不到数据源文件？）：\n"
                + "\n".join(f"· {n}" for n in failed)
                + "\n\n请先到【数据源…】确认路径可读。",
            )
        if cfg.to_dict() != store.load_data_source():
            store.save_data_source(cfg.to_dict())
            self._load_variables(store)
            self._set_status(f"已从数据源中移除：{'、'.join(names)}")

    def _save_variables(self):
        """立即保存项目变量（数据源行不写进 variables）。"""
        stores = self._selected_stores()
        if len(stores) != 1:
            return
        variables = self._collect_variables()
        stores[0].save_variables(variables)
        self._set_status(f"已保存 {len(variables)} 个项目变量")

    def _row_source(self, row: int) -> str:
        item = self.var_table.item(row, 2)
        return item.text() if item else ""

    @classmethod
    def _is_readonly_source(cls, source: str) -> bool:
        """数据源 / 计数 / 循环变量这几类只是「给你看的」，不写进项目变量。"""
        return any(source.startswith(s) for s in cls.READONLY_SOURCES)

    def _collect_variables(self) -> Dict[str, str]:
        """只收集项目变量行；其余几类由数据源 / 循环决定，不写进 variables。"""
        result: Dict[str, str] = {}
        for r in range(self.var_table.rowCount()):
            if self._is_readonly_source(self._row_source(r)):
                continue
            key_item = self.var_table.item(r, 0)
            val_item = self.var_table.item(r, 1)
            key = key_item.text().strip() if key_item else ""
            if not key:
                continue  # 空 key 行直接忽略
            result[key] = val_item.text() if val_item else ""
        return result

    def _set_status(self, text: str):
        self.status_label.setText(text)

    # ------------------------------
    # 打开
    # ------------------------------
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
