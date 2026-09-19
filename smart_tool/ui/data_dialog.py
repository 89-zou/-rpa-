# -*- coding: utf-8 -*-
"""数据面板：看这次采集到了什么、导出成 Excel 能开的 CSV。

数据存在项目的 `data/` 里：
- `data/records.jsonl`：一行一条记录（「采集数据」节点追加写）
- `data/files/`：采集下来的图片、附件、截图

面板只是「读一眼」：运行中会自动刷新（文件一变就重读），
表格里双击一个 `files/…` 的格子会用系统程序打开那张图 / 那个文件。
"""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from smart_tool.core import datastore
from smart_tool.core.project_store import ProjectStore
from smart_tool.ui.help_tip import help_row

#: 表格最多显示多少条（多的请看文件 / 导出 CSV）
MAX_ROWS = 500
#: 自动刷新的间隔（毫秒）：只在文件真的变了才重读
REFRESH_MS = 1500

#: 【?】里的完整说明（界面上只留一句摘要，其余收进弹窗）
DATA_HELP = (
    "数据存在项目目录的 data/ 下：\n"
    "· records.jsonl ＝ 结构化数据，一行一条（「采集数据」节点追加写进去的）；\n"
    "· files/ ＝ 采集时下载的图片、附件，以及截图。\n"
    "每条记录都自动带 _time（采集时间）、_url（来源网址）、_step（第几步）三个\n"
    "元信息，不用自己采。\n"
    "\n"
    "【表格里能做什么】\n"
    "· 双击某一格：如果那一格是 files/… 的文件，就用系统程序打开它；\n"
    "· 表格最多显示最近 500 条，更多的请导出或直接看 records.jsonl。\n"
    "\n"
    "【导出】\n"
    "· 导出 Excel（.xlsx）：Excel 原生格式，表头冻结、列宽自动撑开，\n"
    "  长文本（比如文章正文）不会串行，单元格里的换行也原样保留；\n"
    "· 导出 CSV（.csv）：通用格式，各种软件都能开（用 utf-8-sig，中文不乱码）。\n"
    "超长文本（超过 Excel 单个单元格上限）会被截断并在末尾标注，\n"
    "完整内容始终在 records.jsonl 里。\n"
    "\n"
    "【删数据】「清空记录」只清 records.jsonl；files/ 里的图片、截图不会被删。\n"
    "想彻底重来，就自己删掉项目的 data 目录。"
)


class DataDialog(QDialog):
    """采集结果面板。"""

    def __init__(self, store: ProjectStore = None, parent=None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setWindowTitle("采集数据（采集结果）")
        self.store = store
        self._records: List[Dict] = []
        self._stamp = (0, 0.0)
        self._embedded = embedded
        self._init_ui()
        if embedded:
            # 作为【项目管理】里的一个页签用：不当独立窗口，也不要自己的「关闭」
            self.setWindowFlags(Qt.WindowType.Widget)
            self.btn_close.setVisible(False)
        else:
            self.setMinimumSize(860, 560)
        self.refresh(force=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        if self.store is not None:
            self.timer.start(REFRESH_MS)

    def set_project(self, store: Optional[ProjectStore]):
        """换项目（项目管理里切换左边列表时调）。"""
        self.store = store
        self._stamp = (0, 0.0)
        self.refresh(force=True)
        if store is None:
            self.timer.stop()
        elif self.chk_auto.isChecked():
            self.timer.start(REFRESH_MS)

    def keyPressEvent(self, event):
        """嵌在【项目管理】里当页签时，Esc 别自己咽掉（否则页签会变空白）。

        QDialog 默认把 Esc 当「关闭窗口」＝hide()，交给外面的对话框处理才对。
        """
        if self._embedded and event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)

        root.addWidget(help_row("「采集数据」节点采到的东西都在这里。",
                                "采集数据", DATA_HELP))

        row = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet("color:#0f766e;")
        row.addWidget(self.summary, 1)
        self.chk_auto = QCheckBox("自动刷新")
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip("运行中每 1.5 秒看一眼：记录了才重读，不占资源")
        self.chk_auto.stateChanged.connect(self._on_auto_toggled)
        row.addWidget(self.chk_auto)
        root.addLayout(row)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        btns.addWidget(self.btn_refresh)
        self.btn_folder = QPushButton("打开 data 文件夹")
        self.btn_folder.clicked.connect(self._open_folder)
        btns.addWidget(self.btn_folder)
        self.btn_export = QPushButton("导出 Excel…")
        self.btn_export.setToolTip(
            "导出成 Excel 原生格式 .xlsx：表头冻结、列宽自动撑开，\n"
            "长文本（文章正文）不会乱行")
        self.btn_export.clicked.connect(self._export_xlsx)
        btns.addWidget(self.btn_export)
        self.btn_export_csv = QPushButton("导出 CSV…")
        self.btn_export_csv.setToolTip(
            "导出成 CSV（utf-8-sig，中文不乱码）：通用格式，各种软件都能开")
        self.btn_export_csv.clicked.connect(self._export_csv)
        btns.addWidget(self.btn_export_csv)
        self.btn_clear = QPushButton("清空记录")
        self.btn_clear.setToolTip("只清 records.jsonl；files/ 里的图片文件不动")
        self.btn_clear.clicked.connect(self._clear)
        btns.addWidget(self.btn_clear)
        btns.addStretch()
        self.btn_close = QPushButton("关闭")
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        root.addWidget(self.table, 1)

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color:#888888;")
        root.addWidget(self.path_label)

    # ------------------------------
    # 刷新
    # ------------------------------
    def _on_auto_toggled(self, _state=None):
        if self.chk_auto.isChecked():
            self.timer.start(REFRESH_MS)
        else:
            self.timer.stop()

    def _on_tick(self):
        if self.store is None:
            return
        if datastore.records_stamp(self.store.dir) != self._stamp:
            self.refresh(force=True)

    def refresh(self, force: bool = False):
        """重新读记录并铺表格（文件没变就不做）。"""
        if self.store is None:
            self._records = []
            self.summary.setText("还没有选项目")
            self.path_label.setText("")
            self.table.clear()
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            return
        stamp = datastore.records_stamp(self.store.dir)
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        self._records = datastore.read_records(self.store.dir)
        files = datastore.list_files(self.store.dir)
        total = len(self._records)
        head = f"共 {total} 条记录"
        if total > MAX_ROWS:
            head += f"（表格只显示最近 {MAX_ROWS} 条，全部请看文件 / 导出 CSV）"
        head += f"　files/ 里有 {len(files)} 个文件"
        self.summary.setText(head)
        self.path_label.setText(
            f"数据目录：{datastore.data_dir(self.store.dir)}"
        )
        self._fill_table()

    def _fill_table(self):
        rows = self._records[-MAX_ROWS:]
        cols = datastore.columns(rows)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        for r, record in enumerate(rows):
            for c, name in enumerate(cols):
                value = record.get(name, "")
                if isinstance(value, (dict, list)):
                    text = json.dumps(value, ensure_ascii=False)
                else:
                    text = "" if value is None else str(value)
                item = QTableWidgetItem(text)
                if len(text) > 40:
                    item.setToolTip(text)
                self.table.setItem(r, c, item)
        if cols:
            self.table.resizeColumnsToContents()
            for c in range(len(cols)):
                if self.table.columnWidth(c) > 260:
                    self.table.setColumnWidth(c, 260)

    # ------------------------------
    # 操作
    # ------------------------------
    def _on_cell_double_clicked(self, row: int, col: int):
        item = self.table.item(row, col)
        text = item.text().strip() if item is not None else ""
        if not text.startswith(datastore.FILES_DIR_NAME + "/"):
            return
        path = datastore.data_dir(self.store.dir) / text
        if not path.exists():
            QMessageBox.information(self, "文件不在了", f"找不到：{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_folder(self):
        folder = datastore.data_dir(self.store.dir)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _export_xlsx(self):
        if not self._records:
            QMessageBox.information(self, "没有数据", "现在还没有采集到任何记录。")
            return
        default = f"采集数据_{time.strftime('%Y%m%d_%H%M')}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出成 Excel（.xlsx）", default, "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        try:
            n = datastore.export_xlsx(self.store.dir, Path(path))
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(self, "导出完成",
                                f"已导出 {n} 条记录到：\n{path}")

    def _export_csv(self):
        if not self._records:
            QMessageBox.information(self, "没有数据", "现在还没有采集到任何记录。")
            return
        default = f"采集数据_{time.strftime('%Y%m%d_%H%M')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出成 CSV（Excel 可直接打开）", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            n = datastore.export_csv(self.store.dir, Path(path))
        except OSError as e:
            QMessageBox.critical(self, "导出失败", str(e))
            return
        QMessageBox.information(self, "导出完成",
                                f"已导出 {n} 条记录到：\n{path}")

    def _clear(self):
        if not self._records:
            return
        reply = QMessageBox.question(
            self, "清空记录",
            f"确定清空 records.jsonl 吗？（现在 {len(self._records)} 条）\n"
            "files/ 里的图片、附件不会被删。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        datastore.clear(self.store.dir)
        self.refresh(force=True)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
