# -*- coding: utf-8 -*-
"""登录态管理窗口：登录一次，以后直接复用 cookie / localStorage。

里面干三件事：
1. 选运行时用哪个登录态（名字可以现起——第一次运行会自动创建这个文件）
2. 填「登录后才有的元素」（XPath）——执行器靠它判断登录态还有没有效
3. 管理已有登录态文件：看保存时间与有效期、导入、改名、删除、打开文件夹

改动即时保存（和这个工具里其它窗口一致），关掉即生效。
"""
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from smart_tool.core import auth_store, blocks
from smart_tool.core.project_store import ProjectStore, Step

NO_AUTH_TEXT = "（不使用登录态）"


def _fmt_time(stamp: Optional[float]) -> str:
    if not stamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


class AuthDialog(QDialog):
    """登录态管理。"""

    def __init__(self, store: ProjectStore, steps: List[Step], parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录态（cookie / localStorage）")
        self.setMinimumSize(720, 520)
        self.store = store
        self.steps = list(steps or [])
        self.changed = False
        self._init_ui()
        self.refresh()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)

        tip = QLabel(
            "登录一次，以后就不用再登了：运行时会把这里保存的 cookie（含 httpOnly 的、"
            "以及 localStorage 里的 token）一起带上，直接进后台。\n"
            "· 第一次运行：还没有登录态 → 正常走完整登录流程，「跑完自动存一份」；\n"
            "· 以后运行：带上登录态 → 查一下「登录后才有的元素」在不在，在就跳过登录；\n"
            "· 失效了：元素不在 → 自动清掉、把整条流程重跑一遍（走完整登录）再存新的。\n"
            "要让登录那几步被跳过，得在【流程编辑】里把它们收进一个组合，"
            "再右键把这个组合标记为「登录用」。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555555;")
        root.addWidget(tip)

        # 运行时使用哪个
        row = QHBoxLayout()
        row.addWidget(QLabel("运行时使用："))
        self.combo = QComboBox()
        self.combo.setEditable(True)          # 允许现起名字（第一次运行会创建它）
        self.combo.setMinimumWidth(220)
        self.combo.lineEdit().setPlaceholderText(auth_store.DEFAULT_NAME)
        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        self.combo.lineEdit().editingFinished.connect(self._save_config)
        row.addWidget(self.combo)
        self.state_label = QLabel("")
        self.state_label.setStyleSheet("color:#0f766e;")
        row.addWidget(self.state_label, 1)
        root.addLayout(row)

        # 体检用的元素
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("登录后才有的元素："))
        self.check_edit = QLineEdit()
        self.check_edit.setPlaceholderText('XPath，例如 //*[@id="menu-posts"]')
        self.check_edit.editingFinished.connect(self._save_config)
        row2.addWidget(self.check_edit, 1)
        root.addLayout(row2)
        hint = QLabel(
            "填一个「只有登录之后才会出现」的元素（比如后台左侧菜单）。"
            "执行器打开第一个网页后查它：在＝登录态还有效，不在＝已失效，自动重登。"
            "留空就不做检查（失效了也能用，只是发现不了）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888888;")
        root.addWidget(hint)

        # 已有登录态
        root.addWidget(QLabel(f"已保存的登录态（{auth_store.AUTH_DIR_NAME}/ 目录）："))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["名字", "最后保存", "cookie", "localStorage", "最早过期"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.table, 1)

        # 按钮
        btns = QHBoxLayout()
        self.btn_import = QPushButton("导入文件…")
        self.btn_import.setToolTip("从别处复制过来的 storage_state json（cookies + localStorage）")
        self.btn_import.clicked.connect(self._import)
        btns.addWidget(self.btn_import)
        self.btn_rename = QPushButton("改名")
        self.btn_rename.clicked.connect(self._rename)
        btns.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self._delete)
        btns.addWidget(self.btn_delete)
        self.btn_folder = QPushButton("打开所在文件夹")
        self.btn_folder.clicked.connect(self._open_folder)
        btns.addWidget(self.btn_folder)
        btns.addStretch()
        self.btn_close = QPushButton("关闭")
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

    # ------------------------------
    # 刷新
    # ------------------------------
    def refresh(self):
        """按磁盘上的实际内容重建列表与提示（不触发保存）。"""
        cfg = self.store.load_auth()
        states = auth_store.list_states(self.store.dir)

        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(NO_AUTH_TEXT)
        for st in states:
            self.combo.addItem(st.name)
        idx = self.combo.findText(cfg["name"]) if cfg["name"] else 0
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        else:
            self.combo.setEditText(cfg["name"])    # 名字还没落成文件，先显示着
        self.combo.blockSignals(False)

        self.check_edit.blockSignals(True)
        if not self.check_edit.text().strip():
            self.check_edit.setText(cfg["check_locator"])
        self.check_edit.blockSignals(False)

        self.table.setRowCount(0)
        for st in states:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cells = [st.name, _fmt_time(st.saved_at), str(st.cookies),
                     str(st.origins),
                     _fmt_time(st.expires_at) if st.expires_at
                     else "会话级（关浏览器就没）"]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, st.name)
                self.table.setItem(r, c, item)

        self._update_state_label(states)
        self._update_buttons()

    def _update_state_label(self, states: List[auth_store.AuthState]):
        """当前选中的登录态是什么情况 + 缺什么配置。"""
        name = self._selected_name()
        if not name:
            self.state_label.setText("不用登录态：每次都会走完整登录流程")
            self.state_label.setStyleSheet("color:#888888;")
            return
        state = next((s for s in states if s.name == name), None)
        if state is None:
            text = "还没保存过：下次运行会自动创建（第一次会走完整登录流程）"
            color = "#b45309"
        else:
            text = f"已有：{auth_store.describe(state)}"
            color = "#0f766e"
        if not self._has_login_group():
            text += "；提醒：流程里还没有标记为「登录用」的组合，登录那几步不会跳过"
            color = "#b45309"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color:{color};")

    def _has_login_group(self) -> bool:
        return any(s.action == blocks.GROUP_START and s.skip_if_logged_in
                   for s in self.steps)

    def _update_buttons(self):
        has_row = self.table.currentRow() >= 0
        for btn in (self.btn_rename, self.btn_delete):
            btn.setEnabled(has_row)

    def _selected_name(self) -> str:
        text = self.combo.currentText().strip()
        return "" if text in ("", NO_AUTH_TEXT) else text

    def _selected_row_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    # ------------------------------
    # 保存配置
    # ------------------------------
    def _on_combo_changed(self, _index: int):
        # 从「已有列表」里选了一个（手输的名字由 editingFinished 管）
        self._save_config()

    def _save_config(self):
        name = self._selected_name()
        locator = self.check_edit.text().strip()
        old = self.store.load_auth()
        if old["name"] == name and old["check_locator"] == locator:
            return
        self.store.save_auth(name, locator)
        self.changed = True
        self._update_state_label(auth_store.list_states(self.store.dir))

    # ------------------------------
    # 文件操作
    # ------------------------------
    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择登录态文件（storage_state json）", "", "JSON 文件 (*.json)")
        if not path:
            return
        default = Path(path).stem
        name, ok = QInputDialog.getText(
            self, "导入登录态", "存成什么名字：", text=default)
        if not ok:
            return
        try:
            auth_store.import_state(self.store.dir, path, name)
        except OSError as e:
            QMessageBox.critical(self, "导入失败", str(e))
            return
        if not self._selected_name():
            self.store.save_auth(auth_store.safe_name(name),
                                 self.check_edit.text().strip())
            self.changed = True
        self.refresh()

    def _rename(self):
        old = self._selected_row_name()
        if not old:
            return
        name, ok = QInputDialog.getText(
            self, "登录态改名", "新名字：", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        try:
            auth_store.rename_state(self.store.dir, old, name)
        except OSError as e:
            QMessageBox.critical(self, "改名失败", str(e))
            return
        if self.store.load_auth()["name"] == old:
            self.store.save_auth(auth_store.safe_name(name),
                                 self.check_edit.text().strip())
            self.changed = True
        self.refresh()

    def _delete(self):
        name = self._selected_row_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "删除登录态",
            f"确定删掉「{name}」吗？\n"
            "下次运行会重新走一遍登录（登录步骤）：\n"
            "如果你的流程是自动登录的，删掉没影响；否则会先让你登录一次。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        auth_store.delete_state(self.store.dir, name)
        self.refresh()

    def _open_folder(self):
        folder = auth_store.auth_dir(self.store.dir)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
