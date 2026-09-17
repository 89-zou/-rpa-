# -*- coding: utf-8 -*-
"""步骤编辑对话框：新建/编辑单个步骤。

按 action 动态切换表单字段：
- navigate           URL
- click              定位(XPath/截图) + 步骤后等待
- fill / select      定位 + 输入值(支持 {{变量}}) + 步骤后等待
- pause_for_human    提示语 + 恢复条件(URL/元素/双重) + 超时
所有动作都可填备注 note。
"""
import re
import shutil
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from smart_tool.core import blocks
from smart_tool.core.data_sources import DataSourceConfig, list_columns
from smart_tool.core.project_store import Locator, Step

ACTIONS = [
    "navigate", "click", "fill", "select", "pause_for_human",
    "loop_start", "loop_end", "condition_start", "condition_end", "branch",
    "script",
]
# 新建步骤时不出现在菜单里的动作：这些标记由系统配对生成
NEW_STEP_HIDDEN = {"loop_end", "condition_end", "branch"}
ACTION_LABELS = {
    "navigate": "打开网页 navigate",
    "click": "点击 click",
    "fill": "填入 fill",
    "select": "下拉选择 select",
    "pause_for_human": "暂停等人工 pause_for_human",
    "loop_start": "循环 loop（自动带上「循环结束」，夹在中间的步骤会重复执行）",
    "loop_end": "循环结束（设置与「循环开始」共用，点哪个都是编辑这个循环）",
    "condition_start": "条件 if/else（自动带上分支与「条件结束」，按结果走某个分支）",
    "condition_end": "条件结束（设置与「条件」共用，点哪个都是编辑这个条件）",
    "branch": "分支（匹配值在「条件」节点里改，点它会打开那个条件）",
    "script": "自由代码 script（Python / JavaScript）",
}
# 条件判断方式
COND_MODES = [
    ("equal", "变量相等（变量值跟分支的匹配值比，一样就走那个分支）"),
    ("expr", "表达式（写 Python 表达式，如 len({{row.正文}}) > 500）"),
]
SCRIPT_LANGS = [("python", "Python（本地执行）"),
                ("javascript", "JavaScript（在网页里执行）")]

# 脚本节点可用的对象说明
SCRIPT_HINT_PY = (
    "可用对象：\n"
    "  vars   —— 当前变量字典（可读写，改完自动写回流程变量）\n"
    "  log()  —— 输出一行日志到运行窗口\n"
    "  page   —— Playwright 页面对象，可直接操作浏览器\n"
    "  current_url / project_dir —— 当前网址、项目目录\n"
    "脚本里可写 result = {\"新变量\": 值} 直接输出变量。\n"
    "注意：Python 脚本无法强制中断，请自行避免死循环。"
)
SCRIPT_HINT_JS = (
    "在网页里执行，可直接操作 DOM。可用对象：\n"
    "  vars   —— 当前变量对象（如 vars[\"row.标题\"]，改完自动写回）\n"
    "  log()  —— 输出一行日志到运行窗口\n"
    "  url    —— 当前网址\n"
    "例：vars[\"页数\"] = document.querySelectorAll(\".item\").length;"
)
WAIT_OPTIONS = [
    ("", "不等待"),
    ("element_present", "等待元素出现（推荐，能扛住跳转）"),
    ("page_load", "等待页面跳转 / 加载完成"),
    ("url_changed", "等待 URL 变化"),
    ("network_idle", "等待网络空闲"),
    ("manual", "手动（不自动等）"),
]
# 需要填「等待目标」的等待方式
WAIT_NEEDS_TARGET = ("element_present", "url_changed")
# 循环方式
LOOP_SOURCES = [
    ("data", "数据源（在【数据源…】里配置，每一行一次）"),
    ("list", "变量 / 手动列表（每行一项，可写 {{变量}}）"),
    ("range", "索引范围（写 10 就跑 10 次，或写 0-10）"),
]
# 索引范围：一个整数或 {{变量}}，可写成「起-止」（首尾都算）
_LOOP_RANGE_TERM = r"(?:\d+|\{\{[^{}]+\}\})"
_LOOP_RANGE_RE = re.compile(
    rf"^\s*{_LOOP_RANGE_TERM}\s*(?:-\s*{_LOOP_RANGE_TERM})?\s*$"
)
_LOOP_RANGE_LITERAL_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
RESUME_OPTIONS = [
    ("manual", "仅人工继续"),
    ("url_changed", "URL 变化"),
    ("element_present", "元素出现"),
    ("url_and_element", "URL + 元素 双重确认（推荐）"),
]
IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _set_row_visible(form: QFormLayout, widget, visible: bool):
    """整行显隐（连左侧标签一起），用不到的选项直接收起来不占地方。"""
    widget.setVisible(visible)
    label = form.labelForField(widget)
    if label is not None:
        label.setVisible(visible)


def _looks_like_xpath(text: str) -> bool:
    """粗判是不是 XPath，用来拦住「把说明文字写进定位框」这种常见错误。

    规则：以 / 、( 或 . 开头，且引号之外的文字里没有中文。
    （引号里的中文是合法的，例如 //a[text()="登录"]。）
    说明文字混进去会让 Playwright 报「选择器语法错误」，很难看出问题在哪。
    """
    t = text.strip()
    if not t.startswith(("/", "(", ".")):
        return False
    outside_quotes = re.sub(r"'[^']*'|\"[^\"]*\"", "", t)
    return re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]",
                     outside_quotes) is None


class StepEditDialog(QDialog):
    """新建/编辑步骤。get_step() 在 accept 后取结果。"""

    def __init__(self, project_dir: Path, step: Optional[Step] = None,
                 parent=None, data_source: Optional[dict] = None,
                 project_variables: Optional[dict] = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._editing = step is not None
        self._step_id = step.id if step else 0
        self._data_source = data_source or {}
        self._variables = project_variables or {}
        self.setWindowTitle("编辑步骤" if self._editing else "新建步骤")
        self.setMinimumWidth(680)
        self._init_ui()
        if step:
            self._load_from_step(step)
        self._on_action_changed()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form = form

        # 动作
        self.action_combo = QComboBox()
        for a in ACTIONS:
            if not self._editing and a in NEW_STEP_HIDDEN:
                continue        # 新建时不给「循环结束」，它由「循环」自动配对
            self.action_combo.addItem(ACTION_LABELS[a], a)
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
        form.addRow("动作：", self.action_combo)

        # --- navigate 组 ---
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/login")
        form.addRow("网址 URL：", self.url_edit)

        # --- 定位组（click/fill/select）---
        self.locator_type = QComboBox()
        self.locator_type.addItem("XPath", "xpath")
        self.locator_type.addItem("截图（OpenCV 匹配）", "image")
        self.locator_type.currentIndexChanged.connect(self._on_locator_type_changed)
        form.addRow("定位方式：", self.locator_type)

        loc_row = QWidget()
        loc_layout = QHBoxLayout(loc_row)
        loc_layout.setContentsMargins(0, 0, 0, 0)
        self.locator_value = QLineEdit()
        self.locator_value.setPlaceholderText("//input[@id='username']")
        self.btn_pick_image = QPushButton("选择截图…")
        self.btn_pick_image.clicked.connect(self._pick_image)
        loc_layout.addWidget(self.locator_value, 1)
        loc_layout.addWidget(self.btn_pick_image)
        form.addRow("定位路径：", loc_row)

        self.image_hint = QLabel(
            "截图请自行用任意工具裁剪目标元素，选择后会复制到项目 img/ 目录。"
        )
        self.image_hint.setWordWrap(True)
        self.image_hint.setStyleSheet("color: #888;")
        form.addRow("", self.image_hint)

        self.preview = QLabel("（未选择截图）")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(130)
        self.preview.setStyleSheet(
            "border: 1px dashed #bbb; border-radius: 4px; color: #999;"
        )
        form.addRow("截图预览：", self.preview)

        # --- 输入值组（fill/select）：右侧下拉可直接关联数据源变量 ---
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("要填入的内容，可用 {{row.标题}} 引用变量")
        self.var_combo = QComboBox()
        self.var_combo.setMinimumWidth(190)
        self.var_combo.setToolTip("选择后把变量插入到输入值中")
        self.var_combo.activated.connect(self._insert_variable)
        value_row = QWidget()
        value_layout = QHBoxLayout(value_row)
        value_layout.setContentsMargins(0, 0, 0, 0)
        value_layout.addWidget(self.value_edit, 1)
        value_layout.addWidget(self.var_combo)
        form.addRow("输入值：", value_row)

        self.value_hint = QLabel("")
        self.value_hint.setWordWrap(True)
        self.value_hint.setStyleSheet("color: #888;")
        form.addRow("", self.value_hint)

        # --- 步骤后等待组 ---
        self.wait_combo = QComboBox()
        for key, label in WAIT_OPTIONS:
            self.wait_combo.addItem(label, key)
        self.wait_combo.currentIndexChanged.connect(self._on_wait_changed)
        form.addRow("步骤后等待：", self.wait_combo)
        self.wait_target = QLineEdit()
        self.wait_target.setPlaceholderText(
            "只填 XPath，如 //*[@id='wpadminbar']（说明文字请写到【备注】里）"
        )
        form.addRow("等待目标：", self.wait_target)

        # --- 人工暂停组 ---
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setPlaceholderText("如：请在浏览器中完成验证码")
        form.addRow("暂停提示：", self.prompt_edit)

        self.resume_combo = QComboBox()
        for key, label in RESUME_OPTIONS:
            self.resume_combo.addItem(label, key)
        self.resume_combo.currentIndexChanged.connect(self._on_resume_changed)
        form.addRow("恢复条件：", self.resume_combo)
        self.resume_url = QLineEdit()
        self.resume_url.setPlaceholderText("URL 片段，支持 * 通配，如 /wp-admin/")
        form.addRow("恢复 URL：", self.resume_url)
        self.resume_element = QLineEdit()
        self.resume_element.setPlaceholderText("目标元素 XPath，如 //*[@id='wpadminbar']")
        form.addRow("恢复元素：", self.resume_element)
        self.resume_timeout = QSpinBox()
        self.resume_timeout.setRange(5, 3600)
        self.resume_timeout.setValue(300)
        self.resume_timeout.setSuffix(" 秒")
        form.addRow("等待超时：", self.resume_timeout)

        # --- 循环标记说明 ---
        self.loop_source_combo = QComboBox()
        for key, label in LOOP_SOURCES:
            self.loop_source_combo.addItem(label, key)
        self.loop_source_combo.currentIndexChanged.connect(
            self._on_loop_source_changed
        )
        form.addRow("循环方式：", self.loop_source_combo)

        self.loop_items_edit = QPlainTextEdit()
        self.loop_items_edit.setMinimumHeight(96)
        self.loop_items_edit.setPlaceholderText(
            "每行一项，例如：\n"
            "通知甲/正文.txt\n"
            "通知乙/正文.txt\n\n"
            "也可以整框只写一个变量：{{文件列表}}\n"
            "（变量的值是列表、JSON 数组或换行/逗号分隔的文本都行）"
        )
        form.addRow("循环项：", self.loop_items_edit)

        # 索引范围：可像 fill 一样插入变量
        self.loop_range_edit = QLineEdit()
        self.loop_range_edit.setPlaceholderText(
            "10 = 跑 10 次（索引 0~9）；0-10 = 索引 0~10；也可用变量，如 {{次数}}"
        )
        self.loop_range_var_combo = QComboBox()
        self.loop_range_var_combo.setMinimumWidth(190)
        self.loop_range_var_combo.setToolTip("选择后把变量插入到索引范围里")
        self.loop_range_var_combo.activated.connect(self._insert_loop_range_var)
        self.loop_range_row = QWidget()
        range_layout = QHBoxLayout(self.loop_range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.addWidget(self.loop_range_edit, 1)
        range_layout.addWidget(self.loop_range_var_combo)
        form.addRow("索引范围：", self.loop_range_row)

        self.loop_range_hint = QLabel(
            "只写一个数 = 跑那么多次，索引从 0 开始（10 → 0,1,…,9）；\n"
            "写 起-止 = 从起跑到止，首尾都算（5-10 → 5,6,…,10）；\n"
            "只写一个变量 = 按变量的长度跑（列表 3 项 → 0~2；值为数字 5 → 0~4）；\n"
            "写 起-变量 = 从起跑到变量的末位（0-{{列表}} → 0 到列表最后一项）。"
        )
        self.loop_range_hint.setWordWrap(True)
        self.loop_range_hint.setStyleSheet("color: #888;")
        form.addRow("", self.loop_range_hint)

        self.loop_hint = QLabel(
            "「循环开始 / 循环结束」是一对节点：新增循环时系统一起创建，\n"
            "夹在中间的那些步骤（列表里缩进显示）会按「循环方式」重复执行。\n"
            "设置只有这一份：点「循环结束」也是打开这里。\n"
            "循环体里用 {{loop.item}} 取当前项 / 当前索引，{{loop.index}} 取第几轮（从 1 开始）。\n"
            "· 循环方式选「数据源」：每行一次，字段用 {{row.列名}} / {{file.content}} 引用；\n"
            "· 选「变量 / 手动列表」：每行一项；整框只写 {{变量名}} 时按该变量展开，\n"
            "  列表项是对象（如 JSON 数组）时，它的键同样可以用 {{row.键}} 引用；\n"
            "· 选「索引范围」：按次数或索引区间重复，{{loop.item}} 就是当前索引。"
        )
        self.loop_hint.setWordWrap(True)
        self.loop_hint.setStyleSheet("color: #7a4fb5;")
        form.addRow("", self.loop_hint)

        # --- 条件节点（condition_start）---
        self.cond_mode_combo = QComboBox()
        for key, label in COND_MODES:
            self.cond_mode_combo.addItem(label, key)
        self.cond_mode_combo.currentIndexChanged.connect(self._on_cond_mode_changed)
        form.addRow("判断方式：", self.cond_mode_combo)

        self.cond_expr_edit = QLineEdit()
        self.cond_expr_edit.setPlaceholderText("要判断的变量，如 {{row.地区}}")
        self.cond_var_combo = QComboBox()
        self.cond_var_combo.setMinimumWidth(190)
        self.cond_var_combo.setToolTip("选择后把变量插入到判断内容里")
        self.cond_var_combo.activated.connect(self._insert_cond_var)
        self.cond_row = QWidget()
        cond_row_layout = QHBoxLayout(self.cond_row)
        cond_row_layout.setContentsMargins(0, 0, 0, 0)
        cond_row_layout.addWidget(self.cond_expr_edit, 1)
        cond_row_layout.addWidget(self.cond_var_combo)
        form.addRow("判断内容：", self.cond_row)

        self.cond_hint = QLabel("")
        self.cond_hint.setWordWrap(True)
        self.cond_hint.setStyleSheet("color: #888;")
        form.addRow("", self.cond_hint)

        self.branch_table = QTableWidget(0, 2)
        self.branch_table.setHorizontalHeaderLabels(
            ["分支名（自己看得懂就行）", "匹配值（逗号分隔多个；表达式模式可留空）"]
        )
        self.branch_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.branch_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.branch_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.branch_table.setMinimumHeight(120)
        form.addRow("分支：", self.branch_table)

        branch_btns = QWidget()
        bb = QHBoxLayout(branch_btns)
        bb.setContentsMargins(0, 0, 0, 0)
        self.btn_add_branch = QPushButton("＋ 添加分支")
        self.btn_add_branch.clicked.connect(lambda: self._add_branch_row())
        bb.addWidget(self.btn_add_branch)
        self.btn_del_branch = QPushButton("－ 删除选中分支")
        self.btn_del_branch.clicked.connect(self._remove_branch_row)
        bb.addWidget(self.btn_del_branch)
        self.cond_count_label = QLabel("")
        self.cond_count_label.setStyleSheet("color:#777;")
        bb.addWidget(self.cond_count_label, 1)
        form.addRow("", branch_btns)

        self.cond_branch_hint = QLabel(
            "执行时会先算出「判断内容」的结果，然后从上往下找第一个匹配的分支，"
            "只执行那个分支里的步骤；都不匹配就整个跳过（后面步骤照常执行）。\n"
            "「变量相等」：变量值跟某分支的某个匹配值一样 → 走这个分支；\n"
            "「表达式」：结果是真/假时走第 1 / 第 2 个分支，结果是别的值时按匹配值走。\n"
            "删除分支会把它里面的步骤一起删掉。"
        )
        self.cond_branch_hint.setWordWrap(True)
        self.cond_branch_hint.setStyleSheet("color: #888;")
        form.addRow("", self.cond_branch_hint)

        self._cond_widgets = [
            self.cond_mode_combo, self.cond_row, self.cond_hint,
            self.branch_table, branch_btns, self.cond_branch_hint,
        ]
        # 被删除的分支行号（原下标），保存时由调用方据此删掉分支标记
        self._dropped_branches: list = []

        # --- 自由代码节点（script）---
        self.script_lang_combo = QComboBox()
        for key, label in SCRIPT_LANGS:
            self.script_lang_combo.addItem(label, key)
        self.script_lang_combo.currentIndexChanged.connect(
            self._on_script_lang_changed
        )
        form.addRow("脚本语言：", self.script_lang_combo)

        self.script_code = QPlainTextEdit()
        self.script_code.setPlaceholderText(
            "# 在此写脚本，例如：\n"
            "# vars[\"标题\"] = vars[\"file.parent_name\"].strip()\n"
            "# log(\"已生成标题：\" + vars[\"标题\"])"
        )
        self.script_code.setMinimumHeight(190)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.script_code.setFont(mono)
        form.addRow("脚本代码：", self.script_code)

        self.script_hint = QLabel(SCRIPT_HINT_PY)
        self.script_hint.setWordWrap(True)
        self.script_hint.setStyleSheet("color: #4b5563;")
        form.addRow("", self.script_hint)

        script_row = QWidget()
        script_layout = QHBoxLayout(script_row)
        script_layout.setContentsMargins(0, 0, 0, 0)
        self.script_vars = QLineEdit()
        self.script_vars.setPlaceholderText("留空=传入全部变量；也可只写 row.标题, row.正文")
        script_layout.addWidget(self.script_vars, 1)
        self.script_var_combo = QComboBox()
        self.script_var_combo.setMinimumWidth(160)
        self.script_var_combo.setToolTip("选择后追加到传入变量")
        self.script_var_combo.activated.connect(self._insert_script_var)
        script_layout.addWidget(self.script_var_combo)
        form.addRow("传入变量：", script_row)

        self.script_timeout = QSpinBox()
        self.script_timeout.setRange(1, 3600)
        self.script_timeout.setValue(30)
        self.script_timeout.setSuffix(" 秒")
        form.addRow("执行超时：", self.script_timeout)

        # --- 备注（所有动作）---
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("可选，仅用于你自己辨识这一步")
        form.addRow("备注：", self.note_edit)

        root.addLayout(form)

        # 确定/取消
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # 各字段的 label buddy 不便单独拿，统一用 widget 列表控制显隐
        self._navigate_widgets = [self.url_edit]
        self._locator_widgets = [self.locator_type, loc_row]
        self._image_widgets = [self.image_hint, self.preview]
        self._value_widgets = [value_row, self.value_hint]
        self._wait_widgets = [self.wait_combo]
        self._pause_widgets = [self.prompt_edit, self.resume_combo,
                               self.resume_timeout]
        self._loop_widgets = [self.loop_source_combo]
        self._script_widgets = [
            self.script_lang_combo, self.script_code, self.script_hint,
            script_row, self.script_timeout,
        ]
        self._refresh_var_combos()

    # ------------------------------
    # 显隐联动：只显示当前动作真正用得上的字段
    # ------------------------------
    def _current_action(self) -> str:
        return self.action_combo.currentData()

    def _show(self, widget, visible: bool):
        _set_row_visible(self._form, widget, visible)

    def _sync_visibility(self):
        """按「动作 + 定位方式 + 等待方式 + 恢复条件」统一刷新所有行的显隐。

        必须一次性处理全部字段：漏掉的那些会保留上一种动作的显示状态，
        切换动作后就冒出用不到的输入框。
        """
        action = self._current_action()
        is_loop = action in ("loop_start", "loop_end")
        is_cond = action == "condition_start"
        is_locate = action in ("click", "fill", "select")
        is_fill = action in ("fill", "select")
        is_image = is_locate and self.locator_type.currentData() == "image"
        is_pause = action == "pause_for_human"
        is_script = action == "script"
        cond = self.resume_combo.currentData()

        for w in self._navigate_widgets:
            self._show(w, action == "navigate")
        for w in self._locator_widgets:
            self._show(w, is_locate)
        self.btn_pick_image.setVisible(is_image)
        for w in self._image_widgets:
            self._show(w, is_image)
        for w in self._value_widgets:
            self._show(w, is_fill)
        for w in self._wait_widgets:
            self._show(w, is_locate)
        self._show(self.wait_target,
                   is_locate and self.wait_combo.currentData() in WAIT_NEEDS_TARGET)
        for w in self._pause_widgets:
            self._show(w, is_pause)
        self._show(self.resume_url,
                   is_pause and cond in ("url_changed", "url_and_element"))
        self._show(self.resume_element,
                   is_pause and cond in ("element_present", "url_and_element"))
        for w in self._script_widgets:
            self._show(w, is_script)
        self._show(self.loop_hint, is_loop)
        for w in self._loop_widgets:
            self._show(w, is_loop)
        loop_src = self.loop_source_combo.currentData()
        self._show(self.loop_items_edit, is_loop and loop_src == "list")
        for w in (self.loop_range_row, self.loop_range_hint):
            self._show(w, is_loop and loop_src == "range")
        for w in self._cond_widgets:
            self._show(w, is_cond)

        self.locator_value.setPlaceholderText(
            "选择截图后自动填入 img/xxx.png" if is_image
            else "//input[@id='username']"
        )
        self.adjustSize()

    def _on_action_changed(self):
        # 新建条件节点时先给两个空分支，省得用户还要手动加
        if (not self._editing and self._current_action() == "condition_start"
                and self.branch_table.rowCount() == 0):
            self._add_branch_row("分支 1")
            self._add_branch_row("分支 2")
        self._on_cond_mode_changed()
        self._sync_visibility()

    def _on_locator_type_changed(self):
        """截图相关字段只在「定位方式=截图」时出现。"""
        self._sync_visibility()

    def _on_wait_changed(self):
        """「等待目标」只在选了具体等待方式时才出现。"""
        self._sync_visibility()

    def _on_resume_changed(self):
        """恢复 URL / 恢复元素按恢复条件分别出现。"""
        self._sync_visibility()

    def _on_loop_source_changed(self):
        """「循环项」/「循环范围」按遍历来源分别出现。"""
        self._sync_visibility()

    # ------------------------------
    # 变量下拉（直接关联数据源/项目变量）
    # ------------------------------
    def _data_source_ready(self) -> bool:
        """当前项目的数据源是否已配好路径（循环来源选「数据源」时需要）。"""
        if not self._data_source:
            return False
        try:
            return DataSourceConfig.from_dict(self._data_source).configured
        except Exception:
            return False

    def _var_names(self) -> list:
        """可引用的变量：数据源字段 + 项目变量 + 循环项。"""
        names = []
        if self._data_source:
            try:
                names.extend(list_columns(DataSourceConfig.from_dict(
                    self._data_source)))
            except Exception:
                pass
        names.extend(self._variables.keys())
        names.extend(["loop.item", "loop.index", "loop.zero_index"])
        out = []
        for n in names:
            if n and n not in out:
                out.append(n)
        return out

    def _refresh_var_combos(self):
        names = self._var_names()
        for combo, placeholder in (
            (self.var_combo, "插入变量 ▾"),
            (self.loop_range_var_combo, "插入变量 ▾"),
            (self.cond_var_combo, "插入变量 ▾"),
            (self.script_var_combo, "添加变量 ▾"),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, "")
            for n in names:
                combo.addItem(f"{{{{{n}}}}}", n)
            combo.blockSignals(False)

        # 只有 loop.* （自动附加）不算"有变量"，仍提示去哪里配置
        user_names = [n for n in names if not n.startswith("loop.")]
        if user_names:
            src = ""
            if self._data_source:
                from pathlib import Path as _P
                src = _P(self._data_source.get("path", "")).name
            self.value_hint.setText(
                f"可用变量 {len(names)} 个"
                + (f"（数据源：{src}）" if src else "")
                + "，从右侧下拉选择即可插入。"
            )
        else:
            self.value_hint.setText(
                "暂无可用变量：可先在主界面点【数据源…】配置文件，"
                "或在【项目管理…】里手工添加变量。"
            )

    def _insert_variable(self, index: int):
        """把选中的变量插入输入值光标处。"""
        name = self.var_combo.itemData(index)
        if not name:
            return
        self.value_edit.insert(f"{{{{{name}}}}}")
        self.value_edit.setFocus()
        self.var_combo.setCurrentIndex(0)

    def _insert_loop_range_var(self, index: int):
        """把选中的变量插入到「索引范围」光标处。"""
        name = self.loop_range_var_combo.itemData(index)
        if not name:
            return
        self.loop_range_edit.insert(f"{{{{{name}}}}}")
        self.loop_range_edit.setFocus()
        self.loop_range_var_combo.setCurrentIndex(0)

    def _insert_script_var(self, index: int):
        """把选中的变量追加到「传入变量」列表。"""
        name = self.script_var_combo.itemData(index)
        if not name:
            return
        current = [x.strip() for x in
                   self.script_vars.text().replace("，", ",").split(",")
                   if x.strip()]
        if name not in current:
            current.append(name)
            self.script_vars.setText(", ".join(current))
        self.script_vars.setFocus()
        self.script_var_combo.setCurrentIndex(0)

    def _on_script_lang_changed(self):
        is_js = self.script_lang_combo.currentData() == "javascript"
        self.script_hint.setText(SCRIPT_HINT_JS if is_js else SCRIPT_HINT_PY)
        self.script_timeout.setSuffix(" 秒" + ("" if is_js else "（仅提示，不强制中断）"))

    # ------------------------------
    # 条件分支表
    # ------------------------------
    def _on_cond_mode_changed(self):
        is_expr = self.cond_mode_combo.currentData() == "expr"
        self.cond_expr_edit.setPlaceholderText(
            "Python 表达式，如 len({{row.正文}}) > 500"
            if is_expr else "要判断的变量，如 {{row.地区}}"
        )
        self.cond_hint.setText(
            "表达式里可以直接写 {{变量}}（系统会按数字/文本自动代入）；\n"
            "算出来是真/假 → 走第 1 / 第 2 个分支，算出来是别的值 → 按下面的匹配值走。"
            if is_expr else
            "把「判断内容」渲染出来的值，跟各分支的匹配值逐个比，一样就走那个分支。"
        )
        self.branch_table.setHorizontalHeaderLabels([
            "分支名（自己看得懂就行）",
            "匹配值（表达式结果是真/假时可留空）" if is_expr
            else "匹配值（逗号分隔多个）",
        ])
        self._update_branch_count()

    def _insert_cond_var(self, index: int):
        """把选中的变量插入到「判断内容」光标处。"""
        name = self.cond_var_combo.itemData(index)
        if not name:
            return
        self.cond_expr_edit.insert(f"{{{{{name}}}}}")
        self.cond_expr_edit.setFocus()
        self.cond_var_combo.setCurrentIndex(0)

    def _add_branch_row(self, name: str = "", values: str = "",
                        origin: int = -1) -> int:
        """加一行分支；origin 是它在原清单里的下标（新建的为 -1）。"""
        row = self.branch_table.rowCount()
        self.branch_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, origin)
        self.branch_table.setItem(row, 0, name_item)
        self.branch_table.setItem(row, 1, QTableWidgetItem(values))
        self._update_branch_count()
        return row

    def _remove_branch_row(self):
        row = self.branch_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在表里点一下要删除的分支。")
            return
        name_item = self.branch_table.item(row, 0)
        origin = name_item.data(Qt.ItemDataRole.UserRole) if name_item else -1
        if isinstance(origin, int) and origin >= 0:
            self._dropped_branches.append(origin)
        self.branch_table.removeRow(row)
        self._update_branch_count()

    def _read_branches(self) -> list:
        out = []
        for r in range(self.branch_table.rowCount()):
            name_item = self.branch_table.item(r, 0)
            value_item = self.branch_table.item(r, 1)
            out.append({
                "name": (name_item.text() if name_item else "").strip(),
                "values": (value_item.text() if value_item else "").strip(),
            })
        return out

    def _update_branch_count(self):
        n = self.branch_table.rowCount()
        self.cond_count_label.setText(
            f"共 {n} 个分支" + ("（至少要 1 个）" if n < 1 else "")
        )

    # ------------------------------
    # 截图选择
    # ------------------------------
    def _pick_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择元素截图",
            str(self.project_dir),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return
        src = Path(path)
        if src.suffix.lower() not in IMG_EXTS:
            QMessageBox.warning(self, "格式不支持", "请选择 png/jpg/bmp/webp 图片。")
            return
        self.img_dir.mkdir(parents=True, exist_ok=True)
        # 重名自动加后缀，避免覆盖已有截图
        dst = self.img_dir / src.name
        i = 1
        while dst.exists():
            dst = self.img_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            QMessageBox.critical(self, "复制失败", f"截图复制到 img/ 失败：\n{e}")
            return
        rel = dst.relative_to(self.project_dir).as_posix()
        self.locator_value.setText(rel)
        self._show_preview(dst)

    def _show_preview(self, path: Path):
        pix = QPixmap(str(path))
        if pix.isNull():
            self.preview.setText("（图片无法预览）")
            return
        self.preview.setPixmap(
            pix.scaled(
                self.preview.width() or 300, 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ------------------------------
    # 数据载入/输出
    # ------------------------------
    def _load_from_step(self, s: Step):
        idx = self.action_combo.findData(s.action)
        self.action_combo.setCurrentIndex(max(0, idx))

        self.url_edit.setText(s.url)
        if s.locator:
            self.locator_type.setCurrentIndex(
                self.locator_type.findData(s.locator.type)
            )
            self.locator_value.setText(s.locator.value)
            if s.locator.type == "image" and s.locator.value:
                img_path = self.project_dir / s.locator.value
                if img_path.exists():
                    self._show_preview(img_path)
        self.value_edit.setText(s.value)

        wait_idx = self.wait_combo.findData(s.wait_after)
        self.wait_combo.setCurrentIndex(wait_idx if wait_idx >= 0 else 0)
        self.wait_target.setText(s.wait_target)

        self.prompt_edit.setText(s.prompt)
        self.resume_combo.setCurrentIndex(
            max(0, self.resume_combo.findData(s.resume_condition))
        )
        self.resume_url.setText(s.resume_url)
        self.resume_element.setText(s.resume_element)
        self.resume_timeout.setValue(s.resume_timeout or 300)

        lang_idx = self.script_lang_combo.findData(s.script_lang or "python")
        self.script_lang_combo.setCurrentIndex(max(0, lang_idx))
        self.script_code.setPlainText(s.script_code or "")
        self.script_vars.setText(s.script_vars or "")
        self.script_timeout.setValue(s.script_timeout or 30)
        self._on_script_lang_changed()

        src_idx = self.loop_source_combo.findData(s.loop_source or "data")
        self.loop_source_combo.setCurrentIndex(max(0, src_idx))
        self.loop_items_edit.setPlainText(s.loop_items or "")
        self.loop_range_edit.setText(s.loop_range or "")

        mode_idx = self.cond_mode_combo.findData(s.cond_mode or "equal")
        self.cond_mode_combo.setCurrentIndex(max(0, mode_idx))
        self.cond_expr_edit.setText(s.cond_expr or "")
        self.branch_table.setRowCount(0)
        for i, m in enumerate(s.cond_branches or []):
            self._add_branch_row(m.get("name", ""), m.get("values", ""), origin=i)
        self._on_cond_mode_changed()

        self.note_edit.setText(s.note)

    def _on_accept(self):
        """校验通过则 accept，否则提示并留在对话框。"""
        action = self._current_action()
        errors = []

        if action == "navigate":
            if not self.url_edit.text().strip():
                errors.append("navigate 必须填写网址 URL")
        elif action in ("click", "fill", "select"):
            if not self.locator_value.text().strip():
                kind = "XPath" if self.locator_type.currentData() == "xpath" else "截图"
                errors.append(f"{action} 必须填写定位路径（{kind}）")
            if (self.locator_type.currentData() == "image"
                    and not self.locator_value.text().strip().startswith("img/")):
                errors.append("截图请通过【选择截图】按钮选取，路径需位于 img/ 目录")
            wait_mode = self.wait_combo.currentData()
            target = self.wait_target.text().strip()
            if wait_mode in WAIT_NEEDS_TARGET and not target:
                errors.append("设置了步骤后等待，就必须填写等待目标")
            elif (wait_mode == "element_present"
                    and not _looks_like_xpath(target)):
                errors.append(
                    "「等待元素出现」的目标只能填 XPath（例如 //*[@id='wpadminbar']），"
                    "说明文字请写到【备注】里"
                )
        elif action == "pause_for_human":
            cond = self.resume_combo.currentData()
            if cond in ("url_changed",) and not self.resume_url.text().strip():
                errors.append("恢复条件为 URL 变化时，必须填写恢复 URL")
            if cond == "element_present" and not self.resume_element.text().strip():
                errors.append("恢复条件为元素出现时，必须填写恢复元素 XPath")
            if cond == "url_and_element" and not (
                self.resume_url.text().strip() and self.resume_element.text().strip()
            ):
                errors.append("双重确认必须同时填写恢复 URL 与恢复元素 XPath")
            elem = self.resume_element.text().strip()
            if cond in ("element_present", "url_and_element") and elem \
                    and not _looks_like_xpath(elem):
                errors.append(
                    "恢复元素只能填 XPath（例如 //*[@id='wpadminbar']），"
                    "说明文字请写到【备注】里"
                )
        elif action == "loop_start":
            src = self.loop_source_combo.currentData()
            if src == "list":
                if not self.loop_items_edit.toPlainText().strip():
                    errors.append(
                        "循环方式选「变量 / 手动列表」时，必须填写循环项"
                        "（每行一项，或整框写一个变量，如 {{文件列表}}）"
                    )
            elif src == "range":
                raw = self.loop_range_edit.text().strip()
                if not raw:
                    errors.append(
                        "循环方式选「索引范围」时，必须填写索引范围"
                        "（写 10 就跑 10 次，或写 0-10，也可用变量如 {{次数}}）"
                    )
                elif not _LOOP_RANGE_RE.match(raw):
                    errors.append(
                        "索引范围格式不对：只能填数字、数字-数字，或 {{变量}}，"
                        "例如 10、0-10、5-10、{{次数}}、0-{{列表}}"
                    )
                else:
                    m = _LOOP_RANGE_LITERAL_RE.match(raw)
                    if m and int(m.group(1)) > int(m.group(2)):
                        errors.append("索引范围的起始值不能大于结束值")
                    elif raw.isdigit() and int(raw) <= 0:
                        errors.append("只写一个数时它表示跑多少次，要填大于 0 的整数")
            elif not self._data_source_ready():
                errors.append(
                    "还没配置数据源：请点主界面【数据源…】配置文件路径，"
                    "或把「循环方式」改成「索引范围」/「变量 / 手动列表」"
                )
        elif action == "condition_start":
            if not self.cond_expr_edit.text().strip():
                errors.append(
                    "条件节点必须填写「判断内容」"
                    "（变量相等就填变量，如 {{row.地区}}；表达式就写 Python 表达式）"
                )
            branches = self._read_branches()
            if not branches:
                errors.append("条件节点至少要有一个分支（点【＋ 添加分支】）")
            elif self.cond_mode_combo.currentData() == "equal":
                empty = [str(i + 1) for i, b in enumerate(branches)
                         if not b["values"]]
                if empty:
                    errors.append(
                        "「变量相等」模式下每个分支都要填匹配值，"
                        f"第 {'、'.join(empty)} 个分支还是空的"
                    )
        elif action == "script":
            if not self.script_code.toPlainText().strip():
                errors.append("自由代码节点必须填写脚本代码")
            elif self.script_lang_combo.currentData() == "python":
                # Python 脚本先做一次语法检查，避免运行到一半才报错
                try:
                    compile(self.script_code.toPlainText(),
                            "<脚本检查>", "exec")
                except SyntaxError as e:
                    errors.append(f"Python 脚本语法错误（第 {e.lineno} 行）：{e.msg}")

        if errors:
            QMessageBox.warning(self, "内容不完整", "\n".join(f"· {e}" for e in errors))
            return
        self.accept()

    def get_step(self) -> Step:
        """收集表单为 Step。id 由调用方统一重排。"""
        action = self._current_action()
        step = Step(id=self._step_id, action=action)

        if action == "navigate":
            step.url = self.url_edit.text().strip()
        elif action in ("click", "fill", "select"):
            step.locator = Locator(
                type=self.locator_type.currentData(),
                value=self.locator_value.text().strip(),
            )
            if action in ("fill", "select"):
                step.value = self.value_edit.text().strip()
            step.wait_after = self.wait_combo.currentData()
            step.wait_target = self.wait_target.text().strip()
        elif action == "pause_for_human":
            step.prompt = self.prompt_edit.text().strip()
            step.resume_condition = self.resume_combo.currentData()
            step.resume_url = self.resume_url.text().strip()
            step.resume_element = self.resume_element.text().strip()
            step.resume_timeout = self.resume_timeout.value()
        elif action == "script":
            step.script_lang = self.script_lang_combo.currentData()
            step.script_code = self.script_code.toPlainText()
            step.script_vars = self.script_vars.text().strip()
            step.script_timeout = self.script_timeout.value()
        elif action == "loop_start":
            step.loop_source = self.loop_source_combo.currentData()
            step.loop_items = self.loop_items_edit.toPlainText().strip()
            step.loop_range = self.loop_range_edit.text().strip()
        elif action == "condition_start":
            step.cond_mode = self.cond_mode_combo.currentData()
            step.cond_expr = self.cond_expr_edit.text().strip()
            step.cond_branches = self._read_branches()
            # 界面记下的「被删掉的分支行号」，调用方据此删掉对应分支标记
            step.cond_dropped = sorted(set(self._dropped_branches))

        step.note = self.note_edit.text().strip()
        return step
