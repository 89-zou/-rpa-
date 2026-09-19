# -*- coding: utf-8 -*-
"""项目对话框：新建项目、载入项目。

- NewProjectDialog：填名称 + 起始网址即可建项目（放在【载入项目…】旁边）
- ProjectPickerDialog：从项目文件夹里挑选，也可以浏览到别的文件夹
"""
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from smart_tool import paths
from smart_tool.core.project_store import (
    SCENE_DESKTOP, SCENE_WEB, ProjectStore, create_project, list_projects,
)
from smart_tool.ui.help_tip import help_row

#: 【?】里的完整说明（界面上只留一行路径）
PICKER_PATH_HELP = (
    "每个项目就是 projects/ 下的一个文件夹，里面必须有 steps.json（流程步骤）\n"
    "和（可选）img/（元素截图）、data/（采集到的数据）、auth/（登录态）。\n"
    "\n"
    "【载入】在列表里选中一个项目，点【载入】或直接双击它。\n"
    "\n"
    "【浏览其他文件夹…】项目不一定非得放在默认目录：\n"
    "选任意一个文件夹，只要里面含 steps.json 就能当项目载入。\n"
    "（比如把项目拷到 U 盘或共享盘上，用这个入口打开。）\n"
    "\n"
    "【新建】回主界面点【新建项目…】：填名称、选场景（网页 / 桌面），\n"
    "网页场景还可以顺手填一个起始网址。\n"
    "\n"
    "【删除】项目的删除在【项目管理…】里做（会连带删掉它的截图和数据，不可恢复）。"
)


class NewProjectDialog(QDialog):
    """新建项目：选场景 + 名称 +（网页场景选填）起始网址。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建项目")
        self.setMinimumWidth(520)
        self.created_store: Optional[ProjectStore] = None

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如：WP官网发文")
        form.addRow("项目名称：", self.name_edit)

        self.scene_combo = QComboBox()
        self.scene_combo.addItem("网页自动化（浏览器，用 XPath / 元素捕获）", SCENE_WEB)
        self.scene_combo.addItem("桌面应用（截图定位 + 鼠标键盘）", SCENE_DESKTOP)
        self.scene_combo.setToolTip(
            "场景决定这个项目能用哪些动作，建好之后不能改（要换场景就新建一个）。\n"
            "· 网页：打开网页 / 点击 / 填入 / 下拉选择，靠 XPath 定位，\n"
            "  可以用【捕获元素…】点一下抓元素；\n"
            "· 桌面：激活窗口 / 点击(截图) / 输入文字 / 按键 / 等待，\n"
            "  靠【截屏取模板…】框选图片定位，适合操作本机上的软件。"
        )
        self.scene_combo.currentIndexChanged.connect(self._on_scene_changed)
        form.addRow("场景：", self.scene_combo)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("选填，填了会自动生成第 1 步「打开网页」")
        self.url_row = self.url_edit
        form.addRow("起始网址：", self.url_edit)
        self.form = form
        root.addLayout(form)

        self.scene_hint = QLabel("")
        self.scene_hint.setWordWrap(True)
        self.scene_hint.setStyleSheet("color: #0f766e;")
        root.addWidget(self.scene_hint)

        tip = QLabel(f"项目会创建在：{paths.PROJECTS_DIR}")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #777;")
        root.addWidget(tip)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建并载入")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._on_scene_changed()
        self.name_edit.setFocus()

    @property
    def scene(self) -> str:
        return self.scene_combo.currentData()

    def _on_scene_changed(self, _=None):
        """桌面场景不需要网址；提示也换一套。"""
        desktop = self.scene == SCENE_DESKTOP
        self.url_edit.setVisible(not desktop)
        label = self.form.labelForField(self.url_edit)
        if label is not None:
            label.setVisible(not desktop)
        self.scene_hint.setText(
            "桌面项目会先放一个【激活窗口】占位：填上目标程序的窗口标题\n"
            "（标题里的一小段就行），运行时先把它切到最前面，再往下操作。"
            if desktop else
            "网页项目用浏览器的 XPath 定位；新增「点击 / 填入」时\n"
            "可以直接点【捕获元素…】在页面上抓元素。"
        )

    def _on_accept(self):
        try:
            self.created_store = create_project(
                self.name_edit.text().strip(),
                initial_url=self.url_edit.text().strip(),
                scene=self.scene,
            )
        except ValueError as e:
            QMessageBox.warning(self, "无法创建", str(e))
            return
        self.accept()


class ProjectPickerDialog(QDialog):
    """选择要载入的项目。选好后 chosen_path 即项目目录。"""

    def __init__(self, current_path: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("载入项目")
        self.setMinimumSize(560, 440)
        self.chosen_path: Optional[Path] = None
        self._current_path = Path(current_path).resolve() if current_path else None
        self._stores: List[ProjectStore] = []
        self._init_ui()
        self._reload()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)

        root.addWidget(help_row(f"项目文件夹：{paths.PROJECTS_DIR}",
                                "项目放哪儿", PICKER_PATH_HELP))

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        # 名字太长就省略号，鼠标停上去看完整信息（别横向滚动条）
        self.list_widget.setWordWrap(False)
        self.list_widget.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._choose_from_list())
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.list_widget, 1)

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color: #999;")
        self.path_label.setWordWrap(True)
        root.addWidget(self.path_label)

        bottom = QHBoxLayout()
        self.btn_browse = QPushButton("浏览其他文件夹…")
        self.btn_browse.clicked.connect(self._browse)
        bottom.addWidget(self.btn_browse)
        bottom.addStretch()
        self.btn_load = QPushButton("载入")
        self.btn_load.setDefault(True)
        self.btn_load.clicked.connect(self._choose_from_list)
        bottom.addWidget(self.btn_load)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.btn_cancel)
        root.addLayout(bottom)

    # ------------------------------
    # 列表
    # ------------------------------
    def _reload(self):
        self._stores = list_projects()
        self.list_widget.clear()
        select_row = -1
        for i, s in enumerate(self._stores):
            steps_n = len(s.load_steps())
            item = QListWidgetItem(f"{s.name}（{steps_n} 步）")
            item.setData(Qt.ItemDataRole.UserRole, str(s.dir))
            item.setToolTip(f"{s.name}\n{steps_n} 步 ｜ {s.image_count()} 张截图"
                            f"\n{s.dir}")
            self.list_widget.addItem(item)
            if self._current_path and s.dir.resolve() == self._current_path:
                select_row = i
        if select_row >= 0:
            self.list_widget.setCurrentRow(select_row)
        elif self._stores:
            self.list_widget.setCurrentRow(0)

        # 当前项目不在列表里（从别处载入的）→ 提示一下
        if self._current_path and not any(
            s.dir.resolve() == self._current_path for s in self._stores
        ) and (self._current_path / "steps.json").exists():
            self.path_label.setText(f"当前项目在别处：{self._current_path}")
        self._update_buttons()

    def _update_buttons(self):
        self.btn_load.setEnabled(self.list_widget.currentItem() is not None)
        item = self.list_widget.currentItem()
        if item is not None:
            self.path_label.setText(item.data(Qt.ItemDataRole.UserRole))
        elif not self.path_label.text():
            self.path_label.setText("没有可选项目，请用【浏览其他文件夹…】")

    # ------------------------------
    # 选择
    # ------------------------------
    def _choose_from_list(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        self.chosen_path = Path(item.data(Qt.ItemDataRole.UserRole))
        self.accept()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择项目文件夹", str(paths.PROJECTS_DIR)
        )
        if not folder:
            return
        d = Path(folder)
        if not (d / "steps.json").exists():
            QMessageBox.warning(
                self, "不是项目文件夹",
                f"这个文件夹里没有 steps.json，不是自动化项目：\n{d}\n\n"
                f"如果是新项目，请到【项目管理…】里新建。",
            )
            return
        self.chosen_path = d
        self.accept()
