# -*- coding: utf-8 -*-
"""登录态管理：登录一次，以后直接复用 cookie / localStorage。

里面干三件事：
1. 选运行时用哪个登录态（名字可以现起——第一次运行会自动创建这个文件）
2. 填「登录后才有的元素」（XPath）——执行器靠它判断登录态还有没有效
   （可以直接输入、从「本项目已用过的定位」下拉里挑、或用【捕获元素…】点一下抓）
3. 管理已有登录态文件：看保存时间与有效期、导入、改名、删除、打开文件夹

改动即时保存（和这个工具里其它窗口一致），关掉即生效。
它平时以页签形式出现在【项目管理…】里（embedded=True），也能当独立窗口用。
"""
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from smart_tool.core import auth_store, blocks, step_executor
from smart_tool.core.project_store import ProjectStore, Step
from smart_tool.ui.element_capture import drop_capture_image
from smart_tool.ui.help_tip import help_row
from smart_tool.ui.picker_controller import capture_element

NO_AUTH_TEXT = "（不使用登录态）"
XPATH_PLACEHOLDER = "（下拉＝本项目已用过的定位）"

#: 【?】里的完整说明（界面上只留一句摘要，其余收进弹窗）
AUTH_HELP = (
    "干嘛用的：登录一次，以后运行就不用再登了。\n"
    "\n"
    "运行时会把这里保存的登录态（cookie，含 httpOnly 的，以及 localStorage\n"
    "里的 token）一起带上，直接就是登录状态。\n"
    "\n"
    "【第一次运行】还没有登录态文件 → 正常走完整登录流程，跑完自动存一份。\n"
    "【以后运行】带上登录态 → 查一下「登录后才有的元素」在不在：\n"
    "          在 ＝ 还有效，登录那几步会自动跳过；\n"
    "          不在 ＝ 失效了，自动清掉、把整条流程重跑一遍（这次走完整登录），\n"
    "                跑完再把新的登录态存回去（相当于自动续期）。\n"
    "\n"
    "【要让登录步骤真的被跳过，得配两处】\n"
    "1) 这里：选一个名字（决定文件存在 projects/<项目>/auth/<名字>.json），\n"
    "   并填「登录后才有的元素」——一个只有登录之后才会出现的 XPath，\n"
    "   比如后台左侧菜单 //*[@id=\"menu-posts\"]。可以直接敲、\n"
    "   从下拉挑本项目用过的定位、点【捕获元素…】去页面上点，或者写 {{元素定位}}。\n"
    "   留空＝不做体检：照样能用，但登录态失效了发现不了。\n"
    "2) 【流程编辑…】里：把「打开登录页 → 填账号 → 填密码 → 点登录」多选，\n"
    "   右键「合并选中节点」起个名，再右键「标记为登录用」。\n"
    "\n"
    "【体检的时机】流程里第一个「打开网页」之后。所以那个元素必须是\n"
    "第一步打开的那个页面上就能看到的（通常用后台菜单这种全站都有的元素）。\n"
    "如果第一步打开的是公开首页，登录后才有的元素在那页本来就找不到，\n"
    "就会每次都被判失效、每次都重新登（不会报错，但这么配就没意义了）。\n"
    "\n"
    "【其它】\n"
    "· 存的不是 cookie 字符串，是 Playwright 的 storage_state：httpOnly cookie\n"
    "  和 localStorage 一次存全。（手拼 cookie 很容易漏东西，被站点判定无效。）\n"
    "· 每次跑完都会把最新的 cookie 存回去；这次一条 cookie 都没拿到就不写文件，\n"
    "  免得把好的一份覆盖了。\n"
    "· 下面表格里能看到每个登录态的情况：几个 cookie、几个站点有 localStorage、\n"
    "  最早什么时候过期。可以导入 / 改名 / 删除，也能直接打开所在文件夹。"
)


def _fmt_time(stamp: Optional[float]) -> str:
    if not stamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def xpath_choices(steps: List[Step]) -> List[str]:
    """本项目里已经写过的 XPath（定位 / 等待目标 / 采集的行与字段）。

    体检用的元素往往就是流程里某个「登录后才出现」的定位，
    与其重新敲一遍，不如从下拉里挑；挑不到再自己写 / 用【捕获元素…】。
    """
    out: List[str] = []

    def add(text: str):
        text = (text or "").strip()
        if text and text not in out:
            out.append(text)

    for s in steps or []:
        loc = s.locator
        if loc is not None and loc.value and loc.type != "image":
            add(loc.value)
        if s.wait_target and s.action != "collect":
            add(s.wait_target)
        if s.resume_element:
            add(s.resume_element)
        add(s.collect_row)
        for f in s.collect_fields or []:
            if isinstance(f, dict):
                add(f.get("locator", ""))
    return out


class AuthDialog(QDialog):
    """登录态管理。"""

    def __init__(self, store: Optional[ProjectStore] = None,
                 steps: Optional[List[Step]] = None, parent=None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setWindowTitle("登录态（cookie / localStorage）")
        self.store = store
        self.steps = list(steps or [])
        self.changed = False
        self._loading = False
        self._embedded = embedded
        self._init_ui()
        if embedded:
            # 作为【项目管理】里的一个页签用：不当独立窗口，也不要自己的「关闭」
            self.setWindowFlags(Qt.WindowType.Widget)
            self.btn_close.setVisible(False)
        else:
            self.setMinimumSize(720, 520)
        self.refresh()

    def set_project(self, store: Optional[ProjectStore],
                    steps: Optional[List[Step]] = None):
        """换项目（项目管理里切换左边列表时调）。"""
        self.store = store
        self.steps = list(steps or [])
        self.refresh()

    def keyPressEvent(self, event):
        """嵌在【项目管理】里当页签时，Esc 别自己咽掉。

        QDialog 默认把 Esc 当「关闭窗口」＝hide()——在页签里就成了空白页，
        所以直接放行，让外面的对话框去处理（关掉整个项目管理）。
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

        root.addWidget(help_row("登录一次，以后直接复用 cookie / localStorage。",
                                "登录态", AUTH_HELP))

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

        # 体检用的元素：可以直接写、从「用过的定位」下拉挑、插入变量、或捕获
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("登录后才有的元素："))
        self.check_edit = QComboBox()          # 可编辑：既能下拉选，也能直接敲
        self.check_edit.setEditable(True)
        self.check_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.check_edit.lineEdit().setPlaceholderText(
            'XPath，例如 //*[@id="menu-posts"]')
        self.check_edit.activated.connect(self._on_xpath_picked)
        self.check_edit.lineEdit().editingFinished.connect(self._save_config)
        row2.addWidget(self.check_edit, 1)

        self.var_combo = QComboBox()
        self.var_combo.setToolTip("把变量插到光标处（XPath 里也能用，如 //*[@id=\"{{菜单id}}\"]）")
        self.var_combo.activated.connect(self._insert_variable)
        row2.addWidget(self.var_combo)

        self.btn_capture = QPushButton("捕获元素…")
        self.btn_capture.setToolTip(
            "打开浏览器，在页面上点一下「登录后才出现」的那个元素（比如后台左侧菜单），\n"
            "XPath 自动填进来，不用自己写。"
        )
        self.btn_capture.clicked.connect(self._capture)
        row2.addWidget(self.btn_capture)
        root.addLayout(row2)
        hint = QLabel(
            "只有登录之后才会出现的元素（如后台左侧菜单）；留空＝不做体检。"
            "细节见右上角 ?"
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
        states = auth_store.list_states(self.store.dir) if self.store else []
        self._loading = True

        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(NO_AUTH_TEXT)
        for st in states:
            self.combo.addItem(st.name)
        cfg = self.store.load_auth() if self.store else {"name": "",
                                                         "check_locator": ""}
        idx = self.combo.findText(cfg["name"]) if cfg["name"] else 0
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        else:
            self.combo.setEditText(cfg["name"])    # 名字还没落成文件，先显示着
        self.combo.blockSignals(False)

        # 登录后才有的元素：下拉里是本项目已用过的定位，也能直接敲 / 捕获
        self.check_edit.clear()
        self.check_edit.addItem(XPATH_PLACEHOLDER)
        for xp in xpath_choices(self.steps):
            self.check_edit.addItem(xp)
        self.check_edit.setEditText(cfg["check_locator"])
        self._refresh_var_combo()
        self._loading = False

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
        for w in (self.combo, self.check_edit, self.var_combo, self.btn_capture,
                  self.btn_import):
            w.setEnabled(self.store is not None)

    def _refresh_var_combo(self):
        """「插入变量 ▾」：本项目的变量（自定义 + 读取/采集节点产出的）。"""
        names = (step_executor.available_variables(
            self.steps, self.store.load_all_variables(),
            step_executor.library_written_vars(self.store.dir))
            if self.store else [])
        self.var_combo.blockSignals(True)
        self.var_combo.clear()
        self.var_combo.addItem("插入变量 ▾", "")
        for n in names:
            self.var_combo.addItem(f"{{{{{n}}}}}", n)
        self.var_combo.blockSignals(False)

    def _insert_variable(self, index: int):
        """把选中的变量插到 XPath 输入框的光标处。"""
        name = self.var_combo.itemData(index)
        self.var_combo.setCurrentIndex(0)
        if not name:
            return
        self.check_edit.lineEdit().insert(f"{{{{{name}}}}}")
        self.check_edit.setFocus()

    def _on_xpath_picked(self, index: int):
        """从下拉里挑了一条「本项目用过的定位」。"""
        if index <= 0:
            return
        self.check_edit.setEditText(self.check_edit.itemText(index))
        self._save_config()

    def _capture(self):
        """捕获元素：点一下页面上的元素，XPath 自动填进来。"""
        if self.store is None:
            return
        url = self._default_url()
        if not url:
            QMessageBox.information(
                self, "先加一个「打开网页」",
                "这个项目里还没有「打开网页」节点，捕获器不知道该打开哪个网址。\n"
                "可以在【流程编辑…】里加一步「打开网页」，或者直接手工填 XPath。",
            )
            return
        try:
            data = capture_element(url, self.store.dir)
        except Exception as e:
            QMessageBox.critical(self, "捕获失败", f"{type(e).__name__}: {e}")
            return
        if not data:
            return
        xpath = (data.get("xpath") or "").strip()
        if not xpath:
            QMessageBox.information(self, "没抓到 XPath", "换个元素再点一下试试。")
            return
        self.check_edit.setEditText(xpath)
        self._save_config()
        count = data.get("count", 1)
        # 体检只要 XPath，捕获时顺手存下的元素截图这里用不上，删掉别在 img/ 里堆废图
        drop_capture_image(self.store.dir, data)
        saved = save_captured_locator(self, self.store.dir, data)
        self.state_label.setText(
            f"已捕获：{data.get('desc') or '元素'} → {xpath}"
            + ("" if count == 1 else f"（命中 {count} 个，最好换个更准的）")
            + (f"；已存成元素定位 {{{{{saved}}}}}" if saved else "")
        )
        self.state_label.setStyleSheet("color:#0f766e;")

    def _default_url(self) -> str:
        """捕获器默认打开的网址：项目里第一个「打开网页」。"""
        for s in self.steps:
            if s.action == "navigate" and s.url and "{{" not in s.url:
                return s.url
        return ""

    def _update_state_label(self, states: List[auth_store.AuthState]):
        """当前选中的登录态是什么情况 + 缺什么配置。"""
        if self.store is None:
            self.state_label.setText("先在左边选中一个项目")
            self.state_label.setStyleSheet("color:#888888;")
            return
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
        if self._loading or self.store is None:
            return
        name = self._selected_name()
        locator = self.check_edit.currentText().strip()
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
                                 self.check_edit.currentText().strip())
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
                                 self.check_edit.currentText().strip())
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
