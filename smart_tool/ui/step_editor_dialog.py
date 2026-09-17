# -*- coding: utf-8 -*-
"""步骤编辑对话框：新建/编辑单个步骤。

按 action 动态切换表单字段：
- navigate           URL + 打开超时
- read_data          读取类型/路径/字段勾选 + 产出变量名
- click              定位(XPath/截图) + 步骤后等待
- fill / select      定位 + 输入值(支持 {{变量}}) + 步骤后等待
- pause_for_human    提示语 + 恢复条件(URL/元素/双重) + 超时
- loop_start         循环内容（一个输入框：数字＝跑几次 / {{变量}}＝按长度跑）
所有动作都可填备注 note。
"""
import re
import shutil
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from smart_tool.core.project_store import Locator, Step
from smart_tool.ui.desktop_picker import DesktopPickerDialog
from smart_tool.ui.element_picker_dialog import ElementPickerDialog
from smart_tool.ui.read_data_panel import ReadDataPanel
from smart_tool.ui.screen_capture import ScreenCaptureDialog

# 网页场景能用的动作
WEB_ACTIONS = [
    "navigate", "read_data", "click", "fill", "select", "pause_for_human",
    "loop_start", "loop_end", "condition_start", "condition_end", "branch",
    "script",
]
# 桌面场景能用的动作（没有浏览器，也就没有 XPath / 下拉选择）
DESKTOP_ACTIONS = [
    "win_activate", "click", "fill", "hotkey", "delay", "read_data",
    "pause_for_human", "loop_start", "loop_end", "condition_start",
    "condition_end", "branch", "script",
]
# 新建步骤时不出现在菜单里的动作：这些标记由系统配对生成
NEW_STEP_HIDDEN = {"loop_end", "condition_end", "branch"}
ACTION_LABELS = {
    "navigate": "打开网页 navigate",
    "read_data": "读取数据 read_data（读文件夹/文件 → 产出一个列表变量）",
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
    # 桌面场景
    "win_activate": "激活窗口 win_activate（把目标程序的窗口切到最前面）",
    "hotkey": "按键 hotkey（如 enter、ctrl+s、alt+f4）",
    "delay": "等待 delay（纯等几秒，秒数填在下面）",
}
DESKTOP_LABEL_SUFFIX = {
    "click": "点击 click（在屏幕上找这张图并点它，可双击）",
    "fill": "输入文字 fill（先点一下输入位置，再打进去；中文走剪贴板）",
}
# 条件判断方式
COND_MODES = [
    ("equal", "变量相等（变量值跟分支的匹配值比，一样就走那个分支）"),
    ("expr", "表达式（写 Python 表达式，如 {{loop.item.内容}} 含某个词）"),
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
    "  vars   —— 当前变量对象（如 vars[\"标题\"]，改完自动写回）\n"
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
# 桌面场景：没有 URL / DOM，只能等图片
DESKTOP_WAIT_OPTIONS = [
    ("", "不等待"),
    ("element_present", "等待图片出现（推荐）"),
    ("image_gone", "等待图片消失（转圈、加载提示这类）"),
    ("manual", "手动（不自动等）"),
]
# 需要填「等待目标」的等待方式
WAIT_NEEDS_TARGET = ("element_present", "url_changed", "image_gone")
RESUME_OPTIONS = [
    ("manual", "仅人工继续"),
    ("url_changed", "URL 变化"),
    ("element_present", "元素出现"),
    ("url_and_element", "URL + 元素 双重确认（推荐）"),
]
# 桌面场景只能人工点「继续」（没有 URL / DOM 可判断）
DESKTOP_RESUME_OPTIONS = [("manual", "仅人工继续")]
IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _set_row_visible(form: QFormLayout, widget, visible: bool):
    """整行显隐（连左侧标签一起），用不到的选项直接收起来不占地方。"""
    widget.setVisible(visible)
    label = form.labelForField(widget)
    if label is not None:
        label.setVisible(visible)


def _is_project_image(text: str) -> bool:
    """是不是项目 img/ 目录里的图片（桌面场景的模板都放这儿）。"""
    return text.replace("\\", "/").startswith("img/")


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
                 parent=None, variable_names: Optional[List[str]] = None,
                 default_url: str = "", scene: str = "web"):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._editing = step is not None
        self._step_id = step.id if step else 0
        # 可插入的变量名（自定义变量 + 读取节点产出的 + loop.*）
        self._var_names_list = list(variable_names or [])
        # 元素捕获时默认打开的地址（项目里第一个「打开网页」）
        self._default_url = (default_url or "").strip()
        # 场景决定能选哪些动作：网页（浏览器）/ 桌面（截图定位 + 鼠标键盘）
        self.desktop = scene == "desktop"
        self._actions = DESKTOP_ACTIONS if self.desktop else WEB_ACTIONS
        self.setWindowTitle(("编辑步骤" if self._editing else "新建步骤")
                            + ("（桌面应用）" if self.desktop else ""))
        self.setMinimumWidth(760)
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
        for a in self._actions:
            if not self._editing and a in NEW_STEP_HIDDEN:
                continue        # 新建时不给「循环结束」，它由「循环」自动配对
            label = ACTION_LABELS[a]
            if self.desktop and a in DESKTOP_LABEL_SUFFIX:
                label = DESKTOP_LABEL_SUFFIX[a]
            self.action_combo.addItem(label, a)
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
        form.addRow("动作：", self.action_combo)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("给自己看的名称（留空＝用上面的动作名）")
        self.title_edit.setToolTip(
            "画布和流程列表上显示的名称。\n"
            "例如把「7. 打开网页」改成「7. 打开写文章页」，一眼就知道这一步在干嘛。\n"
            "动作类型（打开网页/点击/循环…）会另外用一行小灰字标出来。"
        )
        form.addRow("名称：", self.title_edit)

        # --- navigate 组 ---
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/login")
        form.addRow("网址 URL：", self.url_edit)
        self.nav_timeout = QSpinBox()
        self.nav_timeout.setRange(5, 3600)
        self.nav_timeout.setSuffix(" 秒")
        self.nav_timeout.setToolTip(
            "打开这个网址最多等几秒（默认 120）。\n"
            "站点慢就调大；只等到网页结构解析完就算打开，\n"
            "页面是否稳定由下面的「步骤后等待」负责。"
        )
        form.addRow("打开超时：", self.nav_timeout)

        # --- read_data 组：读文件 / 文件夹，产出一个「列表变量」---
        self.output_var_edit = QLineEdit()
        self.output_var_edit.setPlaceholderText("给读到的数据起个名字，如 文章列表")
        self.output_var_edit.setToolTip(
            "循环节点里写 {{这个名字}} 就能逐项遍历；\n"
            "循环体里用 {{loop.item.字段}} 取当前这一项的字段。"
        )
        form.addRow("产出变量名：", self.output_var_edit)

        self.read_panel = ReadDataPanel()
        form.addRow("读什么：", self.read_panel)

        # --- 桌面动作专用 ---
        self.win_title_edit = QLineEdit()
        self.win_title_edit.setPlaceholderText("窗口标题里的一小段，如 记事本、Excel")
        self.win_title_edit.setToolTip(
            "填得越少越宽松；匹配到多个就取第一个。\n"
            "运行时找不到窗口，日志会把当前可见的窗口列出来给你参考。"
        )
        form.addRow("窗口标题：", self.win_title_edit)

        self.keys_edit = QLineEdit()
        self.keys_edit.setPlaceholderText(
            "enter、tab、ctrl+s、alt+f4（也认「回车」这类中文）"
        )
        form.addRow("按哪些键：", self.keys_edit)

        self.click_times_combo = QComboBox()
        self.click_times_combo.addItem("单击", 1)
        self.click_times_combo.addItem("双击", 2)
        form.addRow("点击方式：", self.click_times_combo)

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
        self.btn_shot = QPushButton("截屏取模板…")
        self.btn_shot.setToolTip(
            "桌面场景的定位方式：截一张全屏图，在图上拖框圈住控件，\n"
            "存进项目 img/ 当模板（运行时靠它在这块屏幕上找位置）。"
        )
        self.btn_shot.clicked.connect(lambda: self._capture_screen("main"))
        self.btn_capture = QPushButton("捕获元素…")
        self.btn_capture.setToolTip(
            "打开浏览器窗口，在页面上点一下目标元素：\n"
            "自动填好 XPath，并把元素的截图存进 img/ 当兜底。"
        )
        self.btn_capture.clicked.connect(lambda: self._capture_element("main"))
        self.btn_pick_image = QPushButton("选择截图…")
        self.btn_pick_image.clicked.connect(self._pick_image)
        loc_layout.addWidget(self.locator_value, 1)
        loc_layout.addWidget(self.btn_shot)
        loc_layout.addWidget(self.btn_capture)
        loc_layout.addWidget(self.btn_pick_image)
        self.loc_row = loc_row
        form.addRow("定位路径：", loc_row)

        self.capture_hint = QLabel("")
        self.capture_hint.setWordWrap(True)
        self.capture_hint.setStyleSheet("color: #0f766e;")
        form.addRow("", self.capture_hint)

        self.image_hint = QLabel(
            "直接用截图定位：可以用【捕获元素…】自动生成，"
            "也可以自己裁剪一张（选择后会复制到项目 img/ 目录）。"
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

        # --- 兜底截图（XPath 失效时用）---
        self.fallback_edit = QLineEdit()
        self.fallback_edit.setReadOnly(True)
        self.fallback_edit.setPlaceholderText(
            "选填：XPath 失效时用它兜底（点【捕获元素…】会自动生成）"
        )
        self.btn_capture_fb = QPushButton("捕获元素…")
        self.btn_capture_fb.clicked.connect(lambda: self._capture_element("fallback"))
        self.btn_fallback_pick = QPushButton("选择图片…")
        self.btn_fallback_pick.clicked.connect(self._pick_fallback_image)
        self.btn_fallback_clear = QPushButton("清除")
        self.btn_fallback_clear.clicked.connect(self.fallback_edit.clear)
        self.fallback_row = QWidget()
        fb_layout = QHBoxLayout(self.fallback_row)
        fb_layout.setContentsMargins(0, 0, 0, 0)
        fb_layout.addWidget(self.fallback_edit, 1)
        fb_layout.addWidget(self.btn_capture_fb)
        fb_layout.addWidget(self.btn_fallback_pick)
        fb_layout.addWidget(self.btn_fallback_clear)
        form.addRow("兜底截图：", self.fallback_row)

        self.fallback_hint = QLabel(
            "选填。配了它以后：XPath 等不到元素 / 点不动时，会自动改用这张图做模板匹配，"
            "命中后按坐标点击或填入（日志里会写明走了兜底）。"
        )
        self.fallback_hint.setWordWrap(True)
        self.fallback_hint.setStyleSheet("color: #888;")
        form.addRow("", self.fallback_hint)

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
        for key, label in (DESKTOP_WAIT_OPTIONS if self.desktop else WAIT_OPTIONS):
            self.wait_combo.addItem(label, key)
        self.wait_combo.currentIndexChanged.connect(self._on_wait_changed)
        form.addRow("步骤后等待：", self.wait_combo)
        self.wait_target = QLineEdit()
        self.wait_target.setPlaceholderText(
            "只填 XPath，如 //*[@id='wpadminbar']（说明文字请写到【备注】里）"
        )
        self.btn_wait_shot = QPushButton("截屏取模板…")
        self.btn_wait_shot.setToolTip("截屏框选一张图当等待目标（桌面场景用）")
        self.btn_wait_shot.clicked.connect(lambda: self._capture_screen("wait"))
        self.wait_target_row = QWidget()
        wt_layout = QHBoxLayout(self.wait_target_row)
        wt_layout.setContentsMargins(0, 0, 0, 0)
        wt_layout.addWidget(self.wait_target, 1)
        wt_layout.addWidget(self.btn_wait_shot)
        form.addRow("等待目标：", self.wait_target_row)
        self.wait_seconds = QDoubleSpinBox()
        self.wait_seconds.setRange(0, 300)
        self.wait_seconds.setDecimals(1)
        self.wait_seconds.setSingleStep(0.5)
        self.wait_seconds.setSuffix(" 秒")
        self.wait_seconds.setSpecialValueText("不等")
        self.wait_seconds.setToolTip(
            "这个步骤做完后再固定等几秒（0＝不等）。\n"
            "站点慢、点了没反应（比如点了发布但页面没动）时，\n"
            "给这一步加 2~3 秒往往就好了。"
        )
        form.addRow("额外等待：", self.wait_seconds)

        # --- 人工暂停组 ---
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setPlaceholderText("如：请在浏览器中完成验证码")
        form.addRow("暂停提示：", self.prompt_edit)

        self.resume_combo = QComboBox()
        for key, label in (DESKTOP_RESUME_OPTIONS if self.desktop
                           else RESUME_OPTIONS):
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

        # --- 循环组：只填一个「循环内容」 ---
        self.loop_expr_edit = QLineEdit()
        self.loop_expr_edit.setPlaceholderText(
            "10 = 跑 10 次；{{文章列表}} = 按这个变量的长度跑"
        )
        self.loop_expr_var_combo = QComboBox()
        self.loop_expr_var_combo.setMinimumWidth(190)
        self.loop_expr_var_combo.setToolTip("选择后把变量插入到循环内容里")
        self.loop_expr_var_combo.activated.connect(self._insert_loop_expr_var)
        self.loop_expr_row = QWidget()
        loop_layout = QHBoxLayout(self.loop_expr_row)
        loop_layout.setContentsMargins(0, 0, 0, 0)
        loop_layout.addWidget(self.loop_expr_edit, 1)
        loop_layout.addWidget(self.loop_expr_var_combo)
        form.addRow("循环内容：", self.loop_expr_row)

        self.loop_hint = QLabel(
            "「循环开始 / 循环结束」是一对节点：新增循环时系统一起创建，\n"
            "夹在中间的那些步骤（列表里缩进显示）会重复执行。\n"
            "设置只有这一份：点「循环结束」也是打开这里。\n"
            "· 只想跑固定次数 → 填数字，如 10（{{loop.item}} 是当前序号，0 起）；\n"
            "· 想按「读取数据」读到的内容挨个处理 → 填 {{变量名}}（如 {{文章列表}}），\n"
            "  循环体里用 {{loop.item.字段}} 取当前这一项，{{loop.index}} 是第几轮（1 起）；\n"
            "· 变量是别的东西也行：值是列表 / 多行文本就逐项遍历，是数字就跑那么多次。"
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
        self._navigate_widgets = [self.url_edit, self.nav_timeout]
        self._read_widgets = [self.output_var_edit, self.read_panel]
        self._locator_widgets = [self.locator_type, loc_row]
        self._image_widgets = [self.image_hint, self.preview]
        self._value_widgets = [value_row, self.value_hint]
        self._wait_widgets = [self.wait_combo, self.wait_seconds]
        self._pause_widgets = [self.prompt_edit, self.resume_combo,
                               self.resume_timeout]
        self._loop_widgets = [self.loop_expr_row, self.loop_hint]
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
        """按「场景 + 动作 + 定位方式 + 等待方式 + 恢复条件」统一刷新显隐。

        必须一次性处理全部字段：漏掉的那些会保留上一种动作的显示状态，
        切换动作后就冒出用不到的输入框。
        """
        action = self._current_action()
        is_loop = action in ("loop_start", "loop_end")
        is_cond = action == "condition_start"
        is_read = action == "read_data"
        is_locate = action in ("click", "fill", "select")
        is_fill = action in ("fill", "select")
        is_image = is_locate and self.locator_type.currentData() == "image"
        is_pause = action == "pause_for_human"
        is_script = action == "script"
        is_win = action == "win_activate"
        is_keys = action == "hotkey"
        is_delay = action == "delay"
        cond = self.resume_combo.currentData()
        # 桌面场景：定位一律是「图片模板」，没有 XPath / 兜底截图这些概念
        is_xpath = is_locate and not self.desktop \
            and self.locator_type.currentData() == "xpath"
        need_target = self.wait_combo.currentData() in WAIT_NEEDS_TARGET

        for w in self._navigate_widgets:
            self._show(w, action == "navigate")
        for w in self._read_widgets:
            self._show(w, is_read)
        for w in self._locator_widgets:
            self._show(w, is_locate)
        self._show(self.win_title_edit, is_win)
        self._show(self.keys_edit, is_keys)
        self._show(self.click_times_combo,
                   self.desktop and action == "click")
        self.btn_pick_image.setVisible(is_image or (is_locate and self.desktop))
        self.btn_capture.setVisible(is_xpath or (is_locate and self.desktop))
        self.btn_shot.setVisible(is_locate and self.desktop)
        self._show(self.capture_hint, is_locate)
        for w in (self.fallback_row, self.fallback_hint):
            self._show(w, is_xpath)
        for w in self._image_widgets:
            self._show(w, is_image)
        for w in self._value_widgets:
            self._show(w, is_fill)
        for w in self._wait_widgets:
            self._show(w, is_locate)
        self._show(self.wait_target_row, is_locate and need_target)
        self.btn_wait_shot.setVisible(self.desktop and is_locate)
        for w in self._pause_widgets:
            self._show(w, is_pause)
        self._show(self.resume_url,
                   is_pause and cond in ("url_changed", "url_and_element"))
        self._show(self.resume_element,
                   is_pause and cond in ("element_present", "url_and_element"))
        for w in self._script_widgets:
            self._show(w, is_script)
        for w in self._loop_widgets:
            self._show(w, is_loop)
        for w in self._cond_widgets:
            self._show(w, is_cond)

        # 定位那一行的说法随场景变：网页填 XPath，桌面填图片模板
        if self.desktop:
            self.locator_value.setPlaceholderText(
                "img/xxx.png（点【截屏取模板…】框一个控件）"
            )
            self.wait_target.setPlaceholderText(
                "img/xxx.png（点【截屏取模板…】框一个图）"
            )
            self.btn_pick_image.setText("选择图片…")
            self.btn_pick_image.setToolTip("从项目 img/ 里选一张已有图片")
            self.btn_capture.setText("捕获元素…")
            self.btn_capture.setToolTip(
                "桌面元素捕获：全屏遮罩上鼠标划到哪就高亮哪个控件，\n"
                "点一下自动把这个控件裁成模板（UI Automation 给精确位置）。\n"
                "右键＝选上一层（框住容器），按住左键拖＝手动框选，Esc＝取消。"
            )
        else:
            self.locator_value.setPlaceholderText(
                "选择截图后自动填入 img/xxx.png" if is_image
                else "//input[@id='username']"
            )
            self.wait_target.setPlaceholderText(
                "只填 XPath，如 //*[@id='wpadminbar']（说明文字请写到【备注】里）"
            )
            self.btn_pick_image.setText("选择截图…")
        label = self._form.labelForField(self.loc_row)
        if label is not None:
            label.setText("图片模板：" if self.desktop else "定位路径：")
        sec_label = self._form.labelForField(self.wait_seconds)
        if sec_label is not None:
            sec_label.setText("等待秒数：" if is_delay else "额外等待：")
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

    # ------------------------------
    # 变量下拉
    # ------------------------------
    def _refresh_var_combos(self):
        names = list(self._var_names_list)
        for combo, placeholder in (
            (self.var_combo, "插入变量 ▾"),
            (self.loop_expr_var_combo, "插入变量 ▾"),
            (self.cond_var_combo, "插入变量 ▾"),
            (self.script_var_combo, "添加变量 ▾"),
        ):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, "")
            for n in names:
                combo.addItem(f"{{{{{n}}}}}", n)
            combo.blockSignals(False)

        # 名字不写全也能看懂：自定义变量 + 节点产出的，不算「运行时」的那些
        user_names = [n for n in names if not n.startswith("loop.")]
        if user_names:
            self.value_hint.setText(
                f"可用变量 {len(names)} 个，从右侧下拉选择即可插入。\n"
                "循环体内可以用 {{loop.item.字段}} 取当前这一项、"
                "{{loop.index}} 取第几轮。"
            )
        else:
            self.value_hint.setText(
                "暂无变量：在【项目管理…】→【变量清单】里加自定义变量，"
                "或者新增一个「读取数据」节点让它产出变量。"
            )

    def _insert_variable(self, index: int):
        """把选中的变量插入输入值光标处。"""
        name = self.var_combo.itemData(index)
        if not name:
            return
        self.value_edit.insert(f"{{{{{name}}}}}")
        self.value_edit.setFocus()
        self.var_combo.setCurrentIndex(0)

    def _insert_loop_expr_var(self, index: int):
        """把选中的变量插入到「循环内容」光标处。"""
        name = self.loop_expr_var_combo.itemData(index)
        if not name:
            return
        self.loop_expr_edit.insert(f"{{{{{name}}}}}")
        self.loop_expr_edit.setFocus()
        self.loop_expr_var_combo.setCurrentIndex(0)

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
            "Python 表达式，如 len({{loop.item.内容}}) > 500"
            if is_expr else "要判断的变量，如 {{loop.item.地区}}"
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
    # 截图选择 / 元素捕获
    # ------------------------------
    def _copy_into_img(self, src: Path) -> Optional[str]:
        """把一张图拷进项目 img/，返回相对路径；失败返回 None。"""
        if src.suffix.lower() not in IMG_EXTS:
            QMessageBox.warning(self, "格式不支持", "请选择 png/jpg/bmp/webp 图片。")
            return None
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
            QMessageBox.critical(self, "复制失败", f"图片复制到 img/ 失败：\n{e}")
            return None
        return dst.relative_to(self.project_dir).as_posix()

    def _pick_image(self):
        """定位方式＝截图：选一张图当主定位。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择元素截图",
            str(self.project_dir),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return
        rel = self._copy_into_img(Path(path))
        if rel:
            self.locator_value.setText(rel)
            self._show_preview(self.project_dir / rel)

    def _pick_fallback_image(self):
        """兜底截图：选一张已有图片。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择兜底截图（XPath 失效时用）",
            str(self.project_dir),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return
        rel = self._copy_into_img(Path(path))
        if rel:
            self.fallback_edit.setText(rel)

    def _capture_element(self, target: str):
        """捕获元素（按钮槽：整段包住，异常绝不能逃进 Qt 的事件分发）。"""
        try:
            if self.desktop:
                self._capture_desktop_control(target)
                return
            self._capture_web_element(target)
        except Exception as e:
            QMessageBox.critical(self, "捕获失败",
                                 f"{type(e).__name__}: {e}")

    def _capture_web_element(self, target: str):
        """网页场景：打开浏览器点元素 → 拿到 XPath + 元素图（截图进兜底栏）。"""
        url = self.url_edit.text().strip() or self._default_url
        dlg = ElementPickerDialog(url, self.project_dir, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        data = dlg.result_data
        xpath = (data.get("xpath") or "").strip()
        image = data.get("image") or ""
        count = data.get("count", 1)
        desc = data.get("desc", "")
        if target == "main":
            if xpath:
                self.locator_value.setText(xpath)
                self.locator_type.setCurrentIndex(
                    max(0, self.locator_type.findData("xpath")))
                warn = "" if count == 1 else f"（命中 {count} 个，建议核对）"
                self.capture_hint.setText(f"已捕获：{desc} → {xpath}{warn}")
            if image and not self.fallback_edit.text().strip():
                self.fallback_edit.setText(image)
        else:
            if image:
                self.fallback_edit.setText(image)
            else:
                self.capture_hint.setText(
                    "只抓到了 XPath，没抓到截图（元素可能在 iframe 里）："
                    "可以点【选择图片…】手工裁剪一张。"
                )
        if image:
            self._show_preview(self.project_dir / image)
        self._sync_visibility()

    def _capture_desktop_control(self, target: str):
        """桌面场景的捕获：划到哪高亮哪，点一下自动裁图当模板。"""
        dlg = DesktopPickerDialog(self.project_dir, self)
        try:
            if not dlg.run() or not dlg.result_path:
                return
            text = dlg.result_text or dlg.result_path
            if target == "wait":
                self.wait_target.setText(dlg.result_path)
                self.capture_hint.setText(f"已捕获等待模板：{text}")
            else:
                self.locator_value.setText(dlg.result_path)
                self.capture_hint.setText(
                    f"已捕获：{text}　→　{dlg.result_path}"
                    + ("　（窗口标题可填到【激活窗口】那一步里）"
                       if dlg.window_title else "")
                )
            self._show_preview(self.project_dir / dlg.result_path)
            self._sync_visibility()
        finally:
            # 用完就销毁：捕获器里有个全屏遮罩窗口，攒着不放会越堆越多
            dlg.deleteLater()

    def _capture_screen(self, target: str):
        """截屏拖框取模板（桌面场景）：target=main 填定位，wait 填等待目标。"""
        try:
            dlg = ScreenCaptureDialog(self.project_dir, self)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_path:
                return
            if target == "wait":
                self.wait_target.setText(dlg.result_path)
                self.capture_hint.setText(f"已取等待模板：{dlg.result_path}")
            else:
                self.locator_value.setText(dlg.result_path)
                self.capture_hint.setText(
                    f"已取模板：{dlg.result_path}（只框控件本身，别带大片背景）"
                )
            self._sync_visibility()
        except Exception as e:
            QMessageBox.critical(self, "截屏取模板失败",
                                 f"{type(e).__name__}: {e}")

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
        self.title_edit.setText(s.title)
        self.nav_timeout.setValue(int(s.nav_timeout or 120))
        if s.locator:
            self.locator_type.setCurrentIndex(
                self.locator_type.findData(s.locator.type)
            )
            self.locator_value.setText(s.locator.value)
            self.fallback_edit.setText(s.locator.image or "")
            if s.locator.type == "image" and s.locator.value:
                img_path = self.project_dir / s.locator.value
                if img_path.exists():
                    self._show_preview(img_path)
        self.value_edit.setText(s.value)

        wait_idx = self.wait_combo.findData(s.wait_after)
        self.wait_combo.setCurrentIndex(wait_idx if wait_idx >= 0 else 0)
        self.wait_target.setText(s.wait_target)
        self.wait_seconds.setValue(float(s.wait_seconds or 0))

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

        self.output_var_edit.setText(s.output_var or "")
        self.read_panel.load(s.data_cfg or {})
        self.loop_expr_edit.setText(s.loop_expr or "")
        self.win_title_edit.setText(s.win_title or "")
        self.keys_edit.setText(s.keys or "")
        self.click_times_combo.setCurrentIndex(max(
            0, self.click_times_combo.findData(int(s.click_times or 1))))

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
            value = self.locator_value.text().strip()
            if self.desktop:
                if action == "click" and not value:
                    errors.append(
                        "桌面场景的「点击」要选一张图片模板"
                        "（点【截屏取模板…】框住那个控件）"
                    )
                elif value and not _is_project_image(value):
                    errors.append(
                        "图片模板要用【截屏取模板…】或【选择图片…】来选，"
                        "路径需位于项目 img/ 目录"
                    )
            else:
                if not value:
                    kind = ("XPath" if self.locator_type.currentData() == "xpath"
                            else "截图")
                    errors.append(f"{action} 必须填写定位路径（{kind}）")
                if (self.locator_type.currentData() == "image"
                        and not value.startswith("img/")):
                    errors.append("截图请通过【选择截图】按钮选取，路径需位于 img/ 目录")
            wait_mode = self.wait_combo.currentData()
            target = self.wait_target.text().strip()
            if wait_mode in WAIT_NEEDS_TARGET and not target:
                errors.append("设置了步骤后等待，就必须填写等待目标")
            elif self.desktop and target and not _is_project_image(target):
                errors.append(
                    "桌面场景的「等待目标」要选一张图片模板（点【截屏取模板…】）"
                )
            elif (not self.desktop and wait_mode == "element_present"
                    and not _looks_like_xpath(target)):
                errors.append(
                    "「等待元素出现」的目标只能填 XPath（例如 //*[@id='wpadminbar']），"
                    "说明文字请写到【备注】里"
                )
        elif action == "win_activate":
            if not self.win_title_edit.text().strip():
                errors.append(
                    "「激活窗口」要填窗口标题里的一小段（如 记事本、Excel）"
                )
        elif action == "hotkey":
            if not self.keys_edit.text().strip():
                errors.append("「按键」要填按什么键，如 enter、ctrl+s、alt+f4")
        elif action == "delay":
            if float(self.wait_seconds.value()) <= 0:
                errors.append("「等待」要填大于 0 的秒数（填在「等待秒数」里）")
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
        elif action == "read_data":
            if not self.output_var_edit.text().strip():
                errors.append(
                    "「读取数据」必须填一个产出变量名（如 文章列表）——"
                    "循环节点里就是靠这个名字引用它的"
                )
            data_cfg = self.read_panel.config()
            if not data_cfg.get("path"):
                errors.append("「读取数据」必须选好要读的文件 / 文件夹路径")
            elif not self.read_panel.field_names():
                errors.append(
                    "还没有勾选要保存的字段：点【读取预览】，"
                    "在「保存」列勾上要用的字段（如 标题 / 内容）"
                )
        elif action == "loop_start":
            if not self.loop_expr_edit.text().strip():
                errors.append(
                    "「循环」必须填循环内容："
                    "写数字＝跑几次（如 10），或写变量＝按它的长度跑（如 {{文章列表}}）"
                )
        elif action == "condition_start":
            if not self.cond_expr_edit.text().strip():
                errors.append(
                    "条件节点必须填写「判断内容」"
                    "（变量相等就填变量，如 {{loop.item.地区}}；表达式就写 Python 表达式）"
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
        step.title = self.title_edit.text().strip()

        if action == "navigate":
            step.url = self.url_edit.text().strip()
            step.nav_timeout = int(self.nav_timeout.value())
        elif action == "read_data":
            step.output_var = self.output_var_edit.text().strip()
            step.data_cfg = self.read_panel.config()
        elif action in ("click", "fill", "select"):
            step.locator = Locator(
                # 桌面场景一律是「图片模板」；网页场景看「定位方式」
                type="image" if self.desktop else self.locator_type.currentData(),
                value=self.locator_value.text().strip(),
                image="" if self.desktop else self.fallback_edit.text().strip(),
            )
            if self.desktop and action == "click":
                step.click_times = int(self.click_times_combo.currentData() or 1)
            if action in ("fill", "select"):
                step.value = self.value_edit.text().strip()
            step.wait_after = self.wait_combo.currentData()
            step.wait_target = self.wait_target.text().strip()
            step.wait_seconds = float(self.wait_seconds.value())
        elif action == "win_activate":
            step.win_title = self.win_title_edit.text().strip()
        elif action == "hotkey":
            step.keys = self.keys_edit.text().strip()
        elif action == "delay":
            step.wait_seconds = float(self.wait_seconds.value())
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
            step.loop_expr = self.loop_expr_edit.text().strip()
        elif action == "condition_start":
            step.cond_mode = self.cond_mode_combo.currentData()
            step.cond_expr = self.cond_expr_edit.text().strip()
            step.cond_branches = self._read_branches()
            # 界面记下的「被删掉的分支行号」，调用方据此删掉对应分支标记
            step.cond_dropped = sorted(set(self._dropped_branches))

        step.note = self.note_edit.text().strip()
        return step
