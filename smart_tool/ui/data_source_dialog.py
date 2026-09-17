# -*- coding: utf-8 -*-
"""数据源面板：选文件/文件夹 → 读取预览 → 勾选要保存哪些变量。

原来是一个独立的【数据源设置】弹窗；现在并入【项目管理】的第一个页签
「数据源与字段」——数据源设置本来就是「初步确定变量从哪来」的那一步。

面板直接读写传进来的 ProjectStore，改动**立即落盘**（和变量清单一致，
不需要点保存）；父窗口收到 changed 信号后刷新另一个页签。

配置存到 steps.json 的 data_source 节；循环方式选「数据源」的循环节点，
其循环体会对读到的每一行重复执行，行内字段以 {{变量名}} 引用。
"""
from pathlib import Path
from typing import Dict, List, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from smart_tool.core import data_sources
from smart_tool.core.data_sources import DataSourceConfig
from smart_tool.core.project_store import ProjectStore

TYPE_OPTIONS = [
    ("excel", "Excel 表格（.xlsx/.xls，按表头列名循环）"),
    ("json", "JSON（对象数组，按键名循环）"),
    ("txt", "TXT 单个文件（内容作为 file.content）"),
    ("folder", "文件夹（按通配符遍历文件，递归，file.* 变量）"),
]
ENCODING_OPTIONS = ["auto", "utf-8", "gbk", "gb18030"]

# 变量表列：勾选 / 变量名 / 来源 / 取值
COL_KEEP, COL_NAME, COL_SRC, COL_SAMPLE = range(4)


def set_row_visible(form: QFormLayout, widget: QWidget, visible: bool):
    """整行显隐（连左侧标签一起）——用不到的选项不占地方。"""
    widget.setVisible(visible)
    label = form.labelForField(widget)
    if label is not None:
        label.setVisible(visible)


class DataSourcePanel(QWidget):
    """数据源 + 字段勾选（嵌在【项目管理】的页签里）。"""

    changed = pyqtSignal()          # 配置已落盘，父窗口可刷新变量清单

    def __init__(self, store: ProjectStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._cfg = DataSourceConfig.from_dict(store.load_data_source())
        # 之前是否已经挑过变量：挑过的话，预览时「新发现的变量」默认不勾选，
        # 免得点一次【读取预览】就把用户之前不要的变量又加回来
        self._picked_before = bool(self._cfg.vars_picked)
        # 「挑过变量」这个状态要单独记住：清空字段后表格是空的，
        # 但不能退回「没挑过 → 产出全部变量」
        self._vars_picked = bool(self._cfg.vars_picked)
        self._loading = False
        self._var_table_type = None
        self._init_ui()
        self._load_cfg()
        self._on_type_changed(save=False)

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox("数据源（这里决定变量从哪个文件/文件夹来）")
        self.form = QFormLayout(box)

        self.type_combo = QComboBox()
        for key, label in TYPE_OPTIONS:
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.form.addRow("类型：", self.type_combo)

        path_row = QWidget()
        pl = QHBoxLayout(path_row)
        pl.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(r"预设路径，例如 D:\政策数据提取\输出数据")
        self.path_edit.editingFinished.connect(self._on_path_edited)
        pl.addWidget(self.path_edit, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self._browse)
        pl.addWidget(self.btn_browse)
        self.form.addRow("路径：", path_row)

        # Excel 专用
        self.sheet_edit = QLineEdit()
        self.sheet_edit.setPlaceholderText("留空取第一个工作表")
        self.sheet_edit.editingFinished.connect(self._save)
        self.form.addRow("工作表：", self.sheet_edit)
        self.header_check = QCheckBox("首行是表头（变量名为列名，如 {{row.标题}}）")
        self.header_check.setChecked(True)
        self.header_check.stateChanged.connect(self._save)
        self.form.addRow("", self.header_check)

        # 文本类专用（txt / folder / json 都要用编码）
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(ENCODING_OPTIONS)
        self.encoding_combo.currentTextChanged.connect(self._save)
        self.form.addRow("文本编码：", self.encoding_combo)
        self.pattern_edit = QLineEdit("*.txt")
        self.pattern_edit.setPlaceholderText("*.txt（也可 *.md 等）")
        self.pattern_edit.editingFinished.connect(self._save)
        self.form.addRow("文件通配：", self.pattern_edit)
        self.recursive_check = QCheckBox("递归子文件夹（如 输出数据/标题/正文.txt 结构）")
        self.recursive_check.setChecked(True)
        self.recursive_check.stateChanged.connect(self._save)
        self.form.addRow("", self.recursive_check)

        root.addWidget(box)
        root.addWidget(self._build_var_box(), 1)

    def _build_var_box(self) -> QGroupBox:
        box = QGroupBox("字段（勾选要保存的变量，可自由改名）")
        lay = QVBoxLayout(box)

        tip = QLabel(
            "点【读取预览】列出这个数据源能读到的全部字段，然后：\n"
            "· 勾选「保存」＝ 这个字段变成一个项目变量，步骤里用 {{变量名}} 引用\n"
            "· 「变量名」可直接双击改，例如把 文件内容 改名为 内容\n"
            "· 换了路径或类型，这里的清单会清空，需要重新读取预览再勾一次"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #777;")
        lay.addWidget(tip)

        row = QHBoxLayout()
        self.btn_preview = QPushButton("读取预览")
        self.btn_preview.clicked.connect(self._preview)
        row.addWidget(self.btn_preview)
        self.btn_all = QPushButton("全选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        row.addWidget(self.btn_all)
        self.btn_none = QPushButton("全不选")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(self.btn_none)
        self.btn_rename = QPushButton("常用命名：标题 / 正文")
        self.btn_rename.setToolTip(
            "把 父文件夹名 → 标题、文件内容 → 内容（仅文件类数据源）"
        )
        self.btn_rename.clicked.connect(self._apply_common_names)
        row.addWidget(self.btn_rename)
        self.summary_label = QLabel("尚未读取")
        self.summary_label.setStyleSheet("color: #666;")
        row.addWidget(self.summary_label, 1)
        lay.addLayout(row)

        self.var_table = QTableWidget(0, 4)
        self.var_table.setHorizontalHeaderLabels(["保存", "变量名", "来源", "取值"])
        header = self.var_table.horizontalHeader()
        header.setSectionResizeMode(COL_KEEP, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_SRC, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SAMPLE, QHeaderView.ResizeMode.Stretch)
        self.var_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.var_table.itemChanged.connect(self._on_var_item_changed)
        lay.addWidget(self.var_table, 1)

        self.var_hint = QTextEdit()
        self.var_hint.setReadOnly(True)
        self.var_hint.setFixedHeight(96)
        self.var_hint.setStyleSheet("color: #555; background: #f6f6f6;")
        lay.addWidget(self.var_hint)
        return box

    # ------------------------------
    # 变量表：增行 / 收集
    # ------------------------------
    def _add_var_row(self, src_key: str, var: str = "", checked: bool = True,
                     sample: str = ""):
        """插入一行变量：勾选 + 变量名（可改）+ 来源 + 取值。"""
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)

        keep = QTableWidgetItem()
        keep.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsSelectable)
        keep.setCheckState(Qt.CheckState.Checked if checked
                           else Qt.CheckState.Unchecked)
        keep.setData(Qt.ItemDataRole.UserRole, src_key)
        self.var_table.setItem(row, COL_KEEP, keep)

        self.var_table.setItem(row, COL_NAME, QTableWidgetItem(var or src_key))
        src_item = QTableWidgetItem(self._source_label(src_key))
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.var_table.setItem(row, COL_SRC, src_item)
        sample_item = QTableWidgetItem(_short(sample))
        sample_item.setFlags(sample_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sample_item.setToolTip(sample)
        self.var_table.setItem(row, COL_SAMPLE, sample_item)

    def _source_label(self, src_key: str) -> str:
        """来源说明：文件类显示中文字段名，表格/JSON 显示原列名。"""
        if src_key.startswith("file."):
            return dict(data_sources.FILE_FIELDS).get(src_key[5:], src_key[5:])
        kind = "表格列" if self.type_combo.currentData() == "excel" else "JSON 键"
        return f"{kind}：{src_key[5:]}"

    def _check_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._loading = True
        for r in range(self.var_table.rowCount()):
            item = self.var_table.item(r, COL_KEEP)
            if item is not None:
                item.setCheckState(state)
        self._loading = False
        self._save()

    def _apply_common_names(self):
        """一键把文件类数据源改成常用的 标题 / 内容。"""
        common = {"file.parent_name": "标题", "file.content": "内容"}
        hit = 0
        self._loading = True
        for r in range(self.var_table.rowCount()):
            keep = self.var_table.item(r, COL_KEEP)
            name = self.var_table.item(r, COL_NAME)
            if keep is None or name is None:
                continue
            var = common.get(keep.data(Qt.ItemDataRole.UserRole))
            if var:
                name.setText(var)
                keep.setCheckState(Qt.CheckState.Checked)
                hit += 1
        self._loading = False
        if not hit:
            QMessageBox.information(
                self, "提示",
                "当前表格里没有「父文件夹名 / 文件内容」这两个来源，"
                "请先点【读取预览】。",
            )
            return
        self._save()

    def _collect_field_map(self) -> List[Dict[str, str]]:
        """收集勾选的变量 → field_map（未勾选的不保存）。"""
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
    # 联动：只显示当前类型用得上的选项
    # ------------------------------
    def _on_type_changed(self, _=None, save: bool = True):
        t = self.type_combo.currentData()
        is_file = t in ("txt", "folder")

        set_row_visible(self.form, self.sheet_edit, t == "excel")
        set_row_visible(self.form, self.header_check, t == "excel")
        set_row_visible(self.form, self.encoding_combo, t != "excel")
        set_row_visible(self.form, self.pattern_edit, t == "folder")
        set_row_visible(self.form, self.recursive_check, t == "folder")

        # 类型变了：已勾的字段与类型不匹配，清空重来
        if self._var_table_type is not None and self._var_table_type != t:
            self._clear_fields("类型已修改，请重新【读取预览】再勾选")
        self._var_table_type = t

        if t == "excel":
            self.path_edit.setPlaceholderText(r"D:\数据\文章.xlsx")
        elif t == "json":
            self.path_edit.setPlaceholderText(r"D:\数据\政策.json")
        elif t == "txt":
            self.path_edit.setPlaceholderText(r"D:\数据\正文.txt")
        else:
            self.path_edit.setPlaceholderText(r"D:\政策数据提取\输出数据")
        self.btn_rename.setVisible(is_file)
        if save:
            self._save()

    def _clear_fields(self, hint: str):
        """清空字段清单（换路径 / 换类型时用：等于「清空重选」）。

        注意把「挑过变量」的状态留住：否则配置文件里会退回
        「没挑过 → 产出全部原始变量」，那就跟用户的意思正好相反了。
        """
        self._vars_picked = True
        self._loading = True
        self.var_table.setRowCount(0)
        self._loading = False
        self.summary_label.setText(hint)
        self.var_hint.clear()

    def _on_path_edited(self):
        """手改了路径：旧字段作废，清空重选。"""
        if self.path_edit.text().strip() == (self._cfg.path or ""):
            return
        self._clear_fields("路径已修改，请重新【读取预览】再勾选")
        self._save()

    def _browse(self):
        """换地址：按类型选文件夹 / 文件；选完清空字段重来。"""
        if not self.pick_path():
            return
        self._save()

    def pick_path(self) -> bool:
        """弹出选择框换路径；真的换了返回 True（字段清单同时清空）。"""
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
            start_dir = str(Path(cur).parent) if cur else str(Path.home())
            path, _ = QFileDialog.getOpenFileName(
                self, "选择数据文件", start_dir, filt
            )
        if not path or path == cur:
            return False
        self.path_edit.setText(path)
        self._clear_fields("路径已修改，请重新【读取预览】再勾选")
        self._save()
        return True

    # ------------------------------
    # 数据：读 / 存
    # ------------------------------
    def load(self, store: ProjectStore):
        """切换项目时重新载入。"""
        self._store = store
        self._cfg = DataSourceConfig.from_dict(store.load_data_source())
        self._picked_before = bool(self._cfg.vars_picked)
        self._vars_picked = bool(self._cfg.vars_picked)
        self._load_cfg()
        self._on_type_changed(save=False)

    def _load_cfg(self):
        c = self._cfg
        self._loading = True
        if c.type:
            self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(c.type)))
        self.path_edit.setText(c.path)
        self.sheet_edit.setText(c.sheet)
        self.header_check.setChecked(c.has_header)
        self.encoding_combo.setCurrentText(c.encoding if c.encoding else "auto")
        self.pattern_edit.setText(c.pattern or "*.txt")
        self.recursive_check.setChecked(c.recursive)
        self._var_table_type = self.type_combo.currentData()
        # 已挑选的变量：直接列出（取值待读取预览后补上）
        self.var_table.setRowCount(0)
        for m in c.field_map:
            # 旧配置里可能存的是短名（parent_name），统一成 file.xxx / row.xxx
            src = data_sources.resolve_src_key(c.type, m.get("field", ""))
            if src:
                self._add_var_row(src, m.get("var", ""), checked=True)
        self._loading = False
        if self.var_table.rowCount():
            self.summary_label.setText(
                f"已保存 {self.var_table.rowCount()} 个变量（点【读取预览】可复核取值）"
            )
        else:
            self.summary_label.setText("尚未读取")

    def config(self) -> DataSourceConfig:
        return DataSourceConfig(
            type=self.type_combo.currentData(),
            path=self.path_edit.text().strip(),
            sheet=self.sheet_edit.text().strip(),
            has_header=self.header_check.isChecked(),
            encoding=self.encoding_combo.currentText(),
            pattern=self.pattern_edit.text().strip() or "*.txt",
            recursive=self.recursive_check.isChecked(),
            field_map=self._collect_field_map(),
            # 挑过变量（表格里有行，或者刚被清空过）＝ 只产出清单里的；
            # 从没挑过才保持「产出全部原始变量」（兼容旧项目）
            vars_picked=self._vars_picked or self.var_table.rowCount() > 0,
        )

    def _save(self):
        """收集当前设置并落盘（改哪一项都会走到这里）。"""
        if self._loading:
            return
        self._cfg = self.config()
        self._store.save_data_source(self._cfg.to_dict())
        self.changed.emit()

    def _on_var_item_changed(self, item: QTableWidgetItem):
        if item.column() in (COL_KEEP, COL_NAME):
            self._save()

    def _preview(self):
        cfg = self.config()
        if not cfg.path:
            QMessageBox.warning(self, "提示", "请先填写或选择路径。")
            return
        try:
            rows, total, columns = data_sources.preview(cfg, n=5)
        except Exception as e:
            self.summary_label.setText("读取失败")
            QMessageBox.critical(self, "读取失败", str(e))
            return

        # 保留用户已做的勾选/改名，只按新的原始变量清单刷新
        previous: Dict[str, Tuple[bool, str]] = {}
        for r in range(self.var_table.rowCount()):
            keep = self.var_table.item(r, COL_KEEP)
            name = self.var_table.item(r, COL_NAME)
            if keep is not None and name is not None:
                key = keep.data(Qt.ItemDataRole.UserRole) or ""
                previous[key] = (keep.checkState() == Qt.CheckState.Checked,
                                 name.text())

        first = rows[0] if rows else {}
        self._loading = True
        self.var_table.setRowCount(0)
        for key in columns:
            # 已挑过的配置：新出现的列默认不勾（尊重之前的挑选结果）
            checked, name = previous.get(key, (not self._picked_before, key))
            self._add_var_row(key, name, checked=checked, sample=first.get(key, ""))
        self._loading = False

        self.summary_label.setText(f"共 {total} 行，发现 {len(columns)} 个字段")
        self._refresh_hint(first, total)
        self._save()

    def _refresh_hint(self, first: Dict[str, str], total: int):
        """底部说明：引用写法 + 各变量取到多少内容（含空值警告）。"""
        lines = []
        picked = self._collect_field_map()
        if picked:
            names = "、".join(f"{{{{{m['var']}}}}}" for m in picked[:6])
            lines.append(f"步骤里这样引用：{names}"
                         + ("…" if len(picked) > 6 else ""))
        lines.append(f"共 {total} 行数据，每行都会产出上面勾选的变量。")
        lines.append("{{loop.index}} 是当前行号（从 1 开始），无需勾选。")
        if first and picked:
            lines.append("")
            lines.append("取值核对（第 1 行）：")
            for m in picked:
                var, src = m["var"], m["field"]
                if src not in first:
                    lines.append(f"    {{{{{var}}}}} ← 来源 {src} 没读到（请检查配置）")
                    continue
                text = first[src]
                warn = "（空！请检查文件）" if not text.strip() else ""
                # 数量类变量的取值就是个数，别写成「共 1 字」
                detail = (f"取值 {text}" if text.strip().isdigit()
                          else f"共 {len(text)} 字")
                lines.append(
                    f"    {{{{{var}}}}} ← {self._source_label(src)}"
                    f"，{detail}{warn}"
                )
        self.var_hint.setPlainText("\n".join(lines))


def _short(text: str) -> str:
    """表格里显示用的短文本（长内容截断并标注总字数）。"""
    if not text:
        return ""
    one = " ".join(text.split())
    return one if len(one) <= 60 else one[:60] + f"…（共 {len(one)} 字）"
