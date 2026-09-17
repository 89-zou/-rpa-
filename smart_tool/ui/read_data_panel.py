# -*- coding: utf-8 -*-
"""「读取数据」节点的配置面板（不依赖项目存储，直接给步骤编辑器用）。

选文件 / 文件夹 → 【读取预览】列出能读到的字段 → 勾选要保存的字段并命名。
勾选出来的字段就是每个文件的「字段名」，循环体里用 {{loop.item.字段}} 取用。

面板只负责界面与配置的收集，读写盘由调用方（步骤编辑器 / 执行器）负责。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from smart_tool.core import data_sources
from smart_tool.core.data_sources import DataSourceConfig
from smart_tool.core.project_store import FILE_VAR_SHORT

TYPE_OPTIONS = [
    ("folder", "文件夹（按通配符遍历文件，如 每天一个文件夹放 txt）"),
    ("txt", "单个文本文件"),
    ("excel", "Excel 表格（.xlsx/.xls，按表头列名）"),
    ("json", "JSON（对象数组，按键名）"),
]
ENCODING_OPTIONS = ["auto", "utf-8", "gbk", "gb18030"]

# 字段表列：勾选 / 字段 / 变量名
COL_KEEP, COL_SRC, COL_NAME = range(3)
# 一键常用命名（文件主名当标题、文件内容当正文）
COMMON_NAMES = {"file.stem": "标题", "file.content": "内容"}


def _short(text: str) -> str:
    """长内容截断显示。"""
    if not text:
        return ""
    one = " ".join(text.split())
    return one if len(one) <= 50 else one[:50] + f"…（共 {len(one)} 字）"


class ReadDataPanel(QWidget):
    """读取数据节点的配置：路径 / 类型 / 字段勾选。"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = DataSourceConfig()
        self._picked_before = False       # 之前挑过字段 → 新字段默认不勾
        self._vars_picked = False         # 挑过（哪怕勾成空的）＝只产出清单里的
        self._loading = False
        self._table_type: Optional[str] = None
        self._init_ui()
        self.load({})

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)

        self.type_combo = QComboBox()
        for key, label in TYPE_OPTIONS:
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.form.addRow("读取类型：", self.type_combo)

        path_row = QWidget()
        pl = QHBoxLayout(path_row)
        pl.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.path_edit.editingFinished.connect(self._on_path_edited)
        pl.addWidget(self.path_edit, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self._browse)
        pl.addWidget(self.btn_browse)
        self.form.addRow("路径：", path_row)

        self.sheet_edit = QLineEdit()
        self.sheet_edit.setPlaceholderText("留空取第一个工作表")
        self.sheet_edit.editingFinished.connect(self.changed.emit)
        self.form.addRow("工作表：", self.sheet_edit)
        self.header_check = QCheckBox("首行是表头（用列名当字段名）")
        self.header_check.setChecked(True)
        self.header_check.stateChanged.connect(self.changed.emit)
        self.form.addRow("", self.header_check)

        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(ENCODING_OPTIONS)
        self.encoding_combo.currentTextChanged.connect(self.changed.emit)
        self.form.addRow("文本编码：", self.encoding_combo)
        self.pattern_edit = QLineEdit("*.txt")
        self.pattern_edit.setPlaceholderText("*.txt（也可 *.md 等）")
        self.pattern_edit.editingFinished.connect(self.changed.emit)
        self.form.addRow("文件通配：", self.pattern_edit)
        self.recursive_check = QCheckBox("递归子文件夹（如 输出数据/标题/正文.txt 结构）")
        self.recursive_check.setChecked(True)
        self.recursive_check.stateChanged.connect(self.changed.emit)
        self.form.addRow("", self.recursive_check)
        root.addLayout(self.form)

        self.path_hint = QLabel("")
        self.path_hint.setWordWrap(True)
        self.path_hint.setStyleSheet("color: #888;")
        root.addWidget(self.path_hint)

        row = QHBoxLayout()
        self.btn_preview = QPushButton("读取预览")
        self.btn_preview.setToolTip("列出这个路径能读到的字段，然后勾选要用的")
        self.btn_preview.clicked.connect(self.preview)
        row.addWidget(self.btn_preview)
        self.btn_all = QPushButton("全选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        row.addWidget(self.btn_all)
        self.btn_none = QPushButton("全不选")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(self.btn_none)
        self.btn_rename = QPushButton("常用命名：标题 / 内容")
        self.btn_rename.clicked.connect(self._apply_common_names)
        row.addWidget(self.btn_rename)
        self.summary_label = QLabel("尚未读取")
        self.summary_label.setStyleSheet("color: #2f6fb3;")
        row.addWidget(self.summary_label, 1)
        root.addLayout(row)

        self.var_table = QTableWidget(0, 3)
        self.var_table.setHorizontalHeaderLabels(["保存", "字段（从文件里读到什么）", "变量名"])
        header = self.var_table.horizontalHeader()
        header.setSectionResizeMode(COL_KEEP, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SRC, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        self.var_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.var_table.setMinimumHeight(150)
        self.var_table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.var_table, 1)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #555;")
        root.addWidget(self.hint_label)

    # ------------------------------
    # 显隐联动
    # ------------------------------
    def _set_row_visible(self, widget: QWidget, visible: bool):
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _on_type_changed(self, _=None):
        t = self.type_combo.currentData()
        is_file = t in ("txt", "folder")
        self._set_row_visible(self.sheet_edit, t == "excel")
        self._set_row_visible(self.header_check, t == "excel")
        self._set_row_visible(self.encoding_combo, t != "excel")
        self._set_row_visible(self.pattern_edit, t == "folder")
        self._set_row_visible(self.recursive_check, t == "folder")
        if self._table_type is not None and self._table_type != t:
            self._clear_fields("读取类型改了，请重新【读取预览】再勾一次")
        self._table_type = t
        if t == "folder":
            self.path_edit.setPlaceholderText(r"每天换的文件夹，如 D:\输出数据\2026\09\16")
            self.path_hint.setText(
                "路径里可以用变量，例如 D:\\输出数据\\{{年}}\\{{月}}\\{{日}}"
                "（年/月/日在【项目管理…】里加一个自定义变量）。"
            )
        elif t == "txt":
            self.path_edit.setPlaceholderText(r"D:\数据\正文.txt")
            self.path_hint.setText("")
        elif t == "excel":
            self.path_edit.setPlaceholderText(r"D:\数据\文章.xlsx")
            self.path_hint.setText("")
        else:
            self.path_edit.setPlaceholderText(r"D:\数据\政策.json")
            self.path_hint.setText("")
        self.btn_rename.setVisible(is_file)
        self.changed.emit()

    def _clear_fields(self, hint: str):
        """清空字段清单（换路径 / 换类型＝清空重选）。"""
        self._vars_picked = True
        self._loading = True
        self.var_table.setRowCount(0)
        self._loading = False
        self.summary_label.setText(hint)
        self.hint_label.clear()

    def _on_path_edited(self):
        if self.path_edit.text().strip() == (self._cfg.path or ""):
            return
        self._clear_fields("路径已修改，请重新【读取预览】再勾选")
        self.changed.emit()

    def _browse(self):
        t = self.type_combo.currentData()
        cur = self.path_edit.text().strip()
        if t == "folder":
            path = QFileDialog.getExistingDirectory(
                self, "选择数据文件夹",
                cur if cur and Path(cur).is_dir() else str(Path.home()),
            )
        else:
            filt = {
                "excel": "Excel (*.xlsx *.xls)",
                "json": "JSON (*.json)",
                "txt": "文本 (*.txt *.md *.csv *.log);;所有文件 (*.*)",
            }.get(t, "所有文件 (*.*)")
            start = str(Path(cur).parent) if cur else str(Path.home())
            path, _ = QFileDialog.getOpenFileName(self, "选择数据文件", start, filt)
        if not path or path == cur:
            return
        self.path_edit.setText(path)
        self._clear_fields("路径已修改，请重新【读取预览】再勾选")
        self.changed.emit()

    # ------------------------------
    # 字段表
    # ------------------------------
    def _source_label(self, key: str) -> str:
        """字段来源说明：文件类显示中文字段名，表格/JSON 显示原列名。"""
        t = self.type_combo.currentData()
        if key.startswith("file."):
            return dict(data_sources.FILE_FIELDS).get(key[5:], key[5:])
        return key[5:] if key.startswith("row.") else key

    def _default_name(self, key: str) -> str:
        """字段默认叫什么变量名。"""
        if key.startswith("file."):
            return FILE_VAR_SHORT.get(key[5:], key[5:])
        return key[5:] if key.startswith("row.") else key

    def _add_row(self, key: str, var: str = "", checked: bool = True):
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)

        keep = QTableWidgetItem()
        keep.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsSelectable)
        keep.setCheckState(Qt.CheckState.Checked if checked
                           else Qt.CheckState.Unchecked)
        keep.setData(Qt.ItemDataRole.UserRole, key)
        self.var_table.setItem(row, COL_KEEP, keep)

        src = QTableWidgetItem(self._source_label(key))
        src.setFlags(src.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_SRC, src)

        self.var_table.setItem(row, COL_NAME,
                               QTableWidgetItem(var or self._default_name(key)))

    def _check_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._loading = True
        for r in range(self.var_table.rowCount()):
            item = self.var_table.item(r, COL_KEEP)
            if item is not None:
                item.setCheckState(state)
        self._loading = False
        self.changed.emit()

    def _apply_common_names(self):
        """一键命名：文件主名 → 标题，文件内容 → 内容。"""
        hit = 0
        self._loading = True
        for r in range(self.var_table.rowCount()):
            keep = self.var_table.item(r, COL_KEEP)
            name = self.var_table.item(r, COL_NAME)
            if keep is None or name is None:
                continue
            var = COMMON_NAMES.get(keep.data(Qt.ItemDataRole.UserRole))
            if var:
                name.setText(var)
                keep.setCheckState(Qt.CheckState.Checked)
                hit += 1
        self._loading = False
        if not hit:
            QMessageBox.information(
                self, "提示",
                "当前清单里没有「文件主名 / 文件内容」这两个字段，"
                "请先点【读取预览】（文件夹 / 文本类才有）。",
            )
            return
        self.changed.emit()

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() in (COL_KEEP, COL_NAME):
            self.changed.emit()

    def _collect_field_map(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for r in range(self.var_table.rowCount()):
            keep = self.var_table.item(r, COL_KEEP)
            name = self.var_table.item(r, COL_NAME)
            if keep is None or name is None:
                continue
            if keep.checkState() != Qt.CheckState.Checked:
                continue
            var = name.text().strip()
            src = keep.data(Qt.ItemDataRole.UserRole) or ""
            if var and src:
                out.append({"field": src, "var": var})
        return out

    # ------------------------------
    # 预览
    # ------------------------------
    def preview(self) -> bool:
        """读一遍路径 → 列出可勾选的字段。失败返回 False。"""
        cfg = self._config_obj()
        if not cfg.path:
            QMessageBox.warning(self, "提示", "请先填写或选择路径。")
            return False
        try:
            rows, total, columns = data_sources.preview(cfg, n=3)
        except Exception as e:
            self.summary_label.setText("读取失败")
            QMessageBox.critical(self, "读取失败", str(e))
            return False

        previous: Dict[str, Tuple[bool, str]] = {}
        for r in range(self.var_table.rowCount()):
            keep = self.var_table.item(r, COL_KEEP)
            name = self.var_table.item(r, COL_NAME)
            if keep is not None and name is not None:
                previous[keep.data(Qt.ItemDataRole.UserRole) or ""] = (
                    keep.checkState() == Qt.CheckState.Checked, name.text()
                )

        first = rows[0] if rows else {}
        self._loading = True
        self.var_table.setRowCount(0)
        for key in columns:
            checked, var = previous.get(key, (not self._picked_before, ""))
            self._add_row(key, var, checked=checked)
        self._loading = False

        unit = "个文件" if cfg.type == "folder" else "行数据"
        self.summary_label.setText(f"共 {total} {unit}，{len(columns)} 个字段")
        self._refresh_hint(first, total)
        self.changed.emit()
        return True

    def _refresh_hint(self, first: Dict[str, str], total: int) -> None:
        picked = self._collect_field_map()
        lines: List[str] = []
        if picked:
            names = "、".join(f"{{{{loop.item.{m['var']}}}}}" for m in picked[:6])
            lines.append(f"循环体里这样取：{names}" + ("…" if len(picked) > 6 else ""))
        lines.append(f"读取时每个文件产出 {len(picked)} 个字段；"
                     f"共 {total} 项（循环就跑这么多轮）。")
        if first and picked:
            lines.append("取值核对（第 1 项）：")
            for m in picked:
                src = m["field"]
                if src not in first:
                    lines.append(f"    {m['var']} ← 没读到（请检查配置）")
                    continue
                text = first[src]
                warn = "（空！请检查文件）" if not text.strip() else ""
                detail = (f"取值 {text}" if text.strip().isdigit()
                          else f"共 {len(text)} 字")
                lines.append(f"    {m['var']} ← {self._source_label(src)}，{detail}{warn}")
        self.hint_label.setText("\n".join(lines))

    # ------------------------------
    # 配置读 / 写
    # ------------------------------
    def load(self, cfg: Optional[Dict[str, Any]]):
        """按 data_cfg（DataSourceConfig.to_dict()）填充界面。"""
        self._cfg = DataSourceConfig.from_dict(cfg or {})
        c = self._cfg
        self._picked_before = bool(c.vars_picked)
        self._vars_picked = bool(c.vars_picked)
        self._loading = True
        if c.type:
            self.type_combo.setCurrentIndex(
                max(0, self.type_combo.findData(c.type)))
        self.path_edit.setText(c.path)
        self.sheet_edit.setText(c.sheet)
        self.header_check.setChecked(c.has_header)
        self.encoding_combo.setCurrentText(c.encoding or "auto")
        self.pattern_edit.setText(c.pattern or "*.txt")
        self.recursive_check.setChecked(c.recursive)
        self._table_type = self.type_combo.currentData()
        self.var_table.setRowCount(0)
        for m in c.field_map:
            key = data_sources.resolve_src_key(c.type, m.get("field", ""))
            if not key:
                continue
            var = (m.get("var") or "").strip()
            self._add_row(key, var or self._default_name(key), checked=True)
        self._loading = False
        if self.var_table.rowCount():
            self.summary_label.setText(
                f"已保存 {self.var_table.rowCount()} 个字段（点【读取预览】可复核取值）"
            )
        else:
            self.summary_label.setText("尚未读取")
        self._on_type_changed()

    def _config_obj(self) -> DataSourceConfig:
        """收集界面 → DataSourceConfig。"""
        return DataSourceConfig(
            type=self.type_combo.currentData(),
            path=self.path_edit.text().strip(),
            sheet=self.sheet_edit.text().strip(),
            has_header=self.header_check.isChecked(),
            encoding=self.encoding_combo.currentText(),
            pattern=self.pattern_edit.text().strip() or "*.txt",
            recursive=self.recursive_check.isChecked(),
            field_map=self._collect_field_map(),
            vars_picked=self._vars_picked or self.var_table.rowCount() > 0,
        )

    def config(self) -> Dict[str, Any]:
        """收集界面 → data_cfg（存进步骤里）。"""
        return self._config_obj().to_dict()

    def field_names(self) -> List[str]:
        """当前勾选的字段名（循环体里 {{loop.item.<名>}} 用的）。"""
        return [m["var"] for m in self._collect_field_map()]
