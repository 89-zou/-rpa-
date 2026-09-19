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

from smart_tool.core import blocks, free_code, project_store
from smart_tool.core.project_store import Locator, Step
from smart_tool.ui.code_editor import CodeEditor
from smart_tool.ui.collect_panel import CollectPanel
from smart_tool.ui.desktop_picker import DesktopPickerDialog
from smart_tool.ui.element_picker_dialog import (
    drop_capture_image, pick_element, save_captured_locator,
)
from smart_tool.ui.help_tip import HelpButton, help_row
from smart_tool.ui.read_data_panel import ReadDataPanel
from smart_tool.ui.screen_capture import ScreenCaptureDialog

# 网页场景能用的动作
WEB_ACTIONS = [
    "navigate", "read_data", "collect", "click", "fill", "select",
    "note", "pause_for_human", "loop_start", "loop_end", "condition_start",
    "condition_end", "script", "call",
]
# 桌面场景能用的动作（没有浏览器，也就没有 XPath / 下拉选择）
DESKTOP_ACTIONS = [
    "win_activate", "click", "fill", "hotkey", "delay", "read_data",
    "note", "pause_for_human", "loop_start", "loop_end", "condition_start",
    "condition_end", "script", "call",
]
# 新建步骤时不出现在菜单里的动作：这些标记由系统配对生成
NEW_STEP_HIDDEN = {"loop_end", "condition_end"}
ACTION_LABELS = {
    "navigate": "打开网页 navigate",
    "read_data": "读取数据 read_data（读文件夹/文件 → 产出一个列表变量）",
    "collect": "采集数据 collect（把页面上的文字/链接/图片/截图取下来 → 存 data/ 并进变量）",
    "note": "提示 / 日志 note（画布上写一句说明；运行时把内容打进日志，可含 {{变量}}）",
    "click": "点击 click",
    "fill": "填入 fill",
    "select": "下拉选择 select",
    "pause_for_human": "暂停等人工 pause_for_human",
    "loop_start": "循环 loop（自动带上「循环结束」，夹在中间的步骤会重复执行）",
    "loop_end": "循环结束（设置与「循环开始」共用，点哪个都是编辑这个循环）",
    "condition_start": "条件 if/else（自动带上「条件结束」；块里的动作节点自带判断方式）",
    "condition_end": "条件结束（设置与「条件」共用，点哪个都是编辑这个条件）",
    "script": "自由代码 script（Python / JavaScript）",
    "call": "调用函数 call（调用【项目管理…】→【函数库】里定义好的函数）",
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
    ("rule", "规则（条件只提供数据，每个动作节点自带判断方式＋值）"),
    ("expr", "表达式（写一段 Python，适合复杂判断）"),
]
SCRIPT_LANGS = [("python", "Python（本地执行）"),
                ("javascript", "JavaScript（在网页里执行）")]

SCRIPT_PLACEHOLDER_PY = (
    "# 写一个真正的函数，系统会自动调用它（第一个函数）\n"
    "def 清洗标题(#价格表=D:/data/价格表.xlsx):\n"
    "    标题 = @原始标题.strip()          # 读变量清单（直接写 标题 也行）\n"
    "    @结果 = 标题 + '（已处理）'        # 等号左边＝写回变量清单\n"
    "    return 标题"
)
SCRIPT_PLACEHOLDER_JS = (
    "// 写一个真正的函数（ES6 也行），系统会自动调用它\n"
    "function 清洗标题(#价格表=D:/data/价格表.xlsx) {\n"
    "    const 标题 = 原始标题.trim();      // 读变量清单\n"
    "    @结果 = 标题 + '（已处理）';        // 等号左边＝写回变量清单\n"
    "    return 标题;\n"
    "}"
)

# 脚本节点说明（放在【?】里）
SCRIPT_HINT_PY = (
    "代码框里写一个**完整的函数**（第一个函数会被自动调用），三个引用符号：\n"
    "\n"
    "    def 清洗标题(#价格表=D:/data/价格表.xlsx, 后缀='（已处理）'):\n"
    "        标题 = @原始标题.strip()      # 读变量清单（直接写 标题 也行）\n"
    "        @结果 = 标题 + 后缀           # 等号左边是 @名字 → 写回变量清单\n"
    "        /封面 = 'D:/图片/封面.png'    # 等号左边是 /图片名 → 存回图片库\n"
    "        return @结果                  # 其它位置的 @名字 → 读变量\n"
    "\n"
    "【@名字】变量清单里的变量。等号左边＝写回，其它位置＝读取；\n"
    "  名字是唯一的那就直接写名字（不加 @ 也行），带点的只能写 @名字。\n"
    "  变量里存的都是文本，要算数先 int(...) / float(...)。\n"
    "【/图片名】图片库里的图片：读＝得到 img/图片名.xxx 的绝对路径；\n"
    "  等号左边＝把图片存回图片库（值可以是图片路径、bytes，或 dataURL 文本）。\n"
    "【#文件名=路径】电脑里的文件（xlsx/csv/txt…）：只写在参数表里，\n"
    "  函数里用这个名字就是那个路径（自己 open / openpyxl / pandas 打开）。\n"
    "  路径里可以写 {{变量}}，如 #表=D:/导出/{{年月}}.xlsx。\n"
    "  没写路径的形参（如 #表）运行时是空文本。\n"
    "\n"
    "【还能拿到什么】\n"
    "  log('...')  —— 输出一行日志到运行窗口\n"
    "  page        —— Playwright 页面对象，可直接操作浏览器（网页场景）\n"
    "  current_url / project_dir —— 当前网址、项目目录\n"
    "\n"
    "【这是个完整的本机 Python】没有沙箱：能 import 库、读写文件、发网络请求。\n"
    "要用什么就在函数里自己 import（外面的 import 带不进来）。\n"
    "\n"
    "【报错】抛异常会让这一步失败、流程停下（出错信息带行号）。\n"
    "「某条数据不规整就跳过」这类场景，自己在函数里 try/except 包一层。\n"
    "\n"
    "【执行超时】到点会真的把这步掐掉：每进一次循环都会看一眼「到点了没」，\n"
    "所以死循环、超长循环能在超时那一刻停下并告诉你在第几行。\n"
    "唯一的例外是「等外部返回」的写法（time.sleep(600)、page.click() 卡住），\n"
    "那种只能等它自己返回——所以页面上等元素请写 page.xxx(..., timeout=毫秒)。\n"
    "\n"
    "【顺带进函数库】保存这个节点时，函数会自动进【项目管理…】→【函数库】，\n"
    "流程别处就能用「调用函数」节点复用它（改一处、到处一起变）。"
)
SCRIPT_HINT_JS = (
    "代码框里写一个**完整的函数**（也支持 ES6：箭头函数、const/let、\n"
    "async/await、模板字符串；第一个函数会被自动调用）。写法与 Python 一致：\n"
    "\n"
    "    function 清洗标题(#价格表=D:/data/价格表.xlsx, 后缀='（已处理）') {\n"
    "        const 标题 = 原始标题.trim();      // 读变量清单\n"
    "        @结果 = 标题 + 后缀;                // 等号左边＝写回变量清单\n"
    "        /封面 = 'data:image/png;base64,...'; // 存回图片库\n"
    "        return 标题;\n"
    "    }\n"
    "\n"
    "【@名字】变量清单里的变量：等号左边＝写回，其它位置＝读取。\n"
    "【/图片名】图片库里的图片 → 得到 /img 下的绝对路径（也能喂给上传控件）；\n"
    "  等号左边＝存回图片库，值可以是 dataURL / Base64 / 图片路径。\n"
    "【#文件名=路径】写在参数表里：函数里用这个名字拿到的就是那个路径。\n"
    "\n"
    "【还能拿到什么】log('...') 输出日志、url 当前网址、project_dir 项目目录。\n"
    "\n"
    "【注意】\n"
    "· JS 跑在页面里，刷新页面就没了（值都放在流程变量里）。\n"
    "· 桌面场景没有浏览器页面，JS 节点用不了（用 Python）。\n"
    "· 取到的文本是页面原始文本，该 trim() 就 trim()。\n"
    "· 执行超时会中断「await 等着不回来」的写法；但如果函数里写的是\n"
    "  同步死循环（while(true){}），页面本身就被卡住了，那种拦不住。"
)

CALL_HELP = (
    "函数＝写一次、到处调用的一段代码。定义在【项目管理…】→【函数库】，\n"
    "流程里放几个「调用函数」节点就能反复用它——改函数那一处，\n"
    "所有调用它的地方一起变（「自由代码」节点也是同一个写法，只是就地写）。\n"
    "\n"
    "【参数怎么传】写 `形参名=值`，逗号分隔，值可以是变量也可以是字面量：\n"
    "    价格表=D:/data/价格表.xlsx, 单价={{价格}}, 倍数=2\n"
    "· 形参名＝函数签名括号里的名字；\n"
    "· 值里可以写 {{变量}}（运行时先换成实际值）；\n"
    "· 只写名字不写 `=` （如 单价）＝把同名的流程变量传进去；\n"
    "· 没写的形参用函数自己的默认值（file 形参＝签名里写的那个路径）。\n"
    "\n"
    "【返回值】函数里 `@名字 = 值` 写的变量，调用后流程里就能用；\n"
    "`return` 的值只打进运行日志（要看就点运行窗口的日志）。\n"
    "\n"
    "【函数里能用什么】和「自由代码」节点完全一样：\n"
    "@名字 读写变量清单、/图片名 读写图片库、log() / page / current_url /\n"
    "project_dir；执行超时、报错行号的处理也一样（超时会真的把这步掐掉）。\n"
    "\n"
    "在哪加函数：【流程编辑…】上方的【项目管理…】→【函数库】页签，\n"
    "点【新建函数】写代码即可；「自由代码」节点保存时也会自动进函数库。"
)

#: 【?】里的说明（界面上只留一句摘要）
LOOP_HELP = (
    "「循环开始 / 循环结束」是一对结构节点：新增循环时系统一起创建，\n"
    "夹在中间的那些步骤（列表里缩进显示）会重复执行。\n"
    "设置只有这一份——点「循环结束」打开的也是这里。\n"
    "\n"
    "【循环内容怎么写】\n"
    "· 跑固定次数 → 填数字，如 10（{{loop.item}} 是当前序号，0 起）；\n"
    "· 挨个处理「读取数据」/「采集数据」拿到的东西 → 填 {{变量名}}\n"
    "  （如 {{文章列表}}）；\n"
    "· 别的写法也行：值是列表 / 多行文本就逐项遍历，是数字就跑那么多次。\n"
    "\n"
    "【循环体里能用什么】\n"
    "· {{loop.item}}         —— 当前这一项（列表项是对象时，用它下面的字段，\n"
    "                           如 {{loop.item.标题}}）\n"
    "· {{loop.index}}        —— 现在是第几轮（从 1 开始）\n"
    "\n"
    "【想多个循环嵌套】直接在里面再放一个循环就行；\n"
    "【想循环里带条件】把「条件」节点放进去，块里的每个动作节点自带判断方式。\n"
    "\n"
    "【循环跑几行】由「开始」和「结束」之间的步骤决定；\n"
    "想让某一步不参与循环，把它挪到「循环结束」后面去。"
)

COND_HELP = (
    "条件节点只负责给出「判断的数据」，判断方式写在块里的每个动作节点上。\n"
    "执行时先算出这份数据，再从上往下看各动作节点，第一个成立的执行；\n"
    "一个都不成立就整段跳过（后面的步骤照常执行）。\n"
    "\n"
    "【怎么摆】\n"
    "条件节点下面直接放要做的事（在条件里点【＋ 点击创建新节点】加）。\n"
    "一个动作要好几步？先选中那几步【合并成组合】，再让条件指向那个组合。\n"
    "放在越前面的动作节点优先级越高。\n"
    "\n"
    "【规则】（推荐）\n"
    "「判断的数据」写一个变量（如 {{loop.item.标题}}），\n"
    "再双击块里的动作节点，填它的「条件判断」：\n"
    "· 判断方式：包含 / 不包含 / 等于 / 不等于 / 大于 / 小于 / 大于等于 / 小于等于；\n"
    "· 值：要拿来做比较的内容。包含 / 不包含 / 等于 / 不等于 可以写多个，\n"
    "  用逗号分隔（如 公示,公告），命中任意一个就成立；\n"
    "· 大小比较按数字比，两边都要能当数字（比如 5 和 10）。\n"
    "\n"
    "【兜底】（判断方式留空）\n"
    "这个动作不判断、无条件成立，相当于 else —— 「前面都不成立」就往这里走。\n"
    "所以要放在块里的最后一个，否则排在它后面的动作永远轮不到。\n"
    "\n"
    "【表达式】（复杂判断才用）\n"
    "写一段 Python 表达式，里面的 {{变量}} 会按数字 / 文本自动代入。\n"
    "算出来是真 / 假 → 按动作节点的先后走（真走第 1 个、假走第 2 个）；\n"
    "算出来是别的值 → 跟各动作节点里填的「值」比。\n"
    "\n"
    "【配对】\n"
    "「条件 / 条件结束」是配套的：设置只有一份，点哪一端都是编辑这个条件节点。"
)

NOTE_HELP = (
    "这个节点不点页面、不填表单，只是给你自己留记号：\n"
    "\n"
    "· 画布上它就是一张说明卡片（比如在登录那几步前面写「下面开始登录」），\n"
    "  文字会自动按卡片宽度折行，最多显示 3 行；\n"
    "· 运行时会把内容打进日志——想看看某个变量到底取到了什么，\n"
    "  在它后面插一个这种节点、写上 {{那个变量}} 就行。\n"
    "\n"
    "写法：内容里可以写多个 {{变量}}，运行时都会换成实际值；\n"
    "也可以写多行（用回车换行），日志里就一行一条。\n"
    "内容留空＝什么都不做（画布上也只显示一句占位文字）。"
)

LOCATOR_HELP = (
    "定位路径就是「要找哪个元素」，网页场景填 XPath。\n"
    "\n"
    "【定位里可以写变量】\n"
    "捕获到的元素可以存成「元素定位」（用【捕获元素…】抓的时候会问你要不要存），\n"
    "之后任何定位里写 {{登录框}} 就能复用它——\n"
    "改【项目管理…】→【变量清单】里的那一条，全项目跟着变。\n"
    "做多个同类页面（同一套模板的不同站点）时，这一招能省很多事。\n"
    "\n"
    "【XPath 怎么写】\n"
    "· 优先用 id：//*[@id=\"user_login\"]；\n"
    "· 类名要注意：@class='a' 是「class 整个等于 a」，\n"
    "  元素写的是 class=\"star-rating Three\" 就匹配不上，\n"
    "  要用 //p[contains(@class,'star-rating')]；\n"
    "· 最好别用第几个子元素这种（//div[3]），页面一变就错位。\n"
    "\n"
    "【兜底截图】下面那一栏是给 XPath 失效时兜底用的，选填。"
)

FALLBACK_HELP = (
    "选填。配了它以后：XPath 等不到元素 / 点不动时，会自动改用这张图做模板匹配，\n"
    "命中后按坐标点击或填入（日志里会写明这次走了兜底）。\n"
    "\n"
    "什么时候值得配：页面改版频繁、或者元素没有稳定的 id / class；\n"
    "用【捕获元素…】抓的时候会自动生成一张，不用自己截。\n"
    "\n"
    "注意：截图匹配对分辨率、系统缩放、浏览器窗口大小比较敏感，\n"
    "换台机器可能就找不到了——所以它是兜底，别当主定位。"
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
                 default_url: str = "", scene: str = "web",
                 rule_mode: Optional[str] = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._editing = step is not None
        self._step_id = step.id if step else 0
        # 这个节点摆在「条件」里时，块里那个条件的判断方式（rule / expr）；
        # 不是条件里的节点就是 None —— 那种情况下没有「条件判断」这一栏
        self.rule_mode = rule_mode
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

        # --- 这个节点摆在「条件」里时：它自带一条规则（判断方式 + 值）---
        self.rule_op_combo = QComboBox()
        for key, label in blocks.COND_OPS:
            self.rule_op_combo.addItem(label, key)
        self.rule_value_edit = QLineEdit()
        self.rule_value_edit.setPlaceholderText("要比较的值，如 公示,公告")
        self.rule_row = QWidget()
        rule_layout = QHBoxLayout(self.rule_row)
        rule_layout.setContentsMargins(0, 0, 0, 0)
        rule_layout.addWidget(self.rule_op_combo)
        rule_layout.addWidget(self.rule_value_edit, 1)
        form.addRow("条件判断：", self.rule_row)
        self.rule_hint = QLabel("")
        self.rule_hint.setWordWrap(True)
        self.rule_hint.setStyleSheet("color: #888;")
        form.addRow("", self.rule_hint)
        self._rule_widgets = [self.rule_row, self.rule_hint]
        if self.rule_mode == "expr":
            self.rule_value_edit.setPlaceholderText(
                "表达式算出来不是真/假时，按这个值匹配（可留空）")
            self.rule_hint.setText(
                "这个节点在「条件」里。上面的表达式算出来是真/假时，"
                "按动作节点的先后走（真走第 1 个、假走第 2 个）；"
                "算出来是别的值，才按这里的值匹配。"
            )
        else:
            self.rule_hint.setText(
                "这个节点在「条件」里：条件节点只负责给出数据，"
                "规则写在各自动作节点上。判断方式留空＝兜底"
                "（上面都不成立时走这里，所以要放在最后一个）。"
            )

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
        self.read_panel.project_dir = self.project_dir    # 路径失效时按文件名去项目里找
        form.addRow("读什么：", self.read_panel)

        # --- collect 组：把页面上的东西采下来（存 data/ + 进变量）---
        self.collect_panel = CollectPanel()
        self.collect_panel.capture_requested.connect(self._capture_collect_row)
        form.addRow("采集什么：", self.collect_panel)

        # --- note 组：画布上的提示 / 运行日志（不碰浏览器，纯说明 + 打日志）---
        self.note_box = QWidget()
        nb = QVBoxLayout(self.note_box)
        nb.setContentsMargins(0, 0, 0, 0)
        nb.setSpacing(4)
        self.note_text = QPlainTextEdit()
        self.note_text.setPlaceholderText(
            "写给自己看的话，画布上就显示这段；运行时它会（把 {{变量}} 换成实际值后）"
            "打到运行日志里")
        self.note_text.setFixedHeight(78)
        nb.addWidget(self.note_text)
        note_vars = QHBoxLayout()
        note_vars.addStretch()
        self.note_var_combo = QComboBox()
        self.note_var_combo.setMinimumWidth(190)
        self.note_var_combo.setToolTip("选一个变量插到光标处（运行时换成实际值）")
        self.note_var_combo.activated.connect(self._insert_note_var)
        note_vars.addWidget(self.note_var_combo)
        nb.addLayout(note_vars)
        nb.addWidget(help_row("给自己留的记号：画布上是说明卡片，运行时打进日志。",
                              "提示 / 日志", NOTE_HELP))
        form.addRow("提示内容：", self.note_box)

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

        self.locator_hint = help_row(
            "定位里可以写变量：元素定位（如 {{登录框}}）改一处、全项目跟着变。",
            "定位路径", LOCATOR_HELP)
        form.addRow("", self.locator_hint)

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

        self.fallback_hint = help_row(
            "选填：XPath 失效时用它兜底（自动改用截图找位置）。",
            "兜底截图", FALLBACK_HELP)
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

        self.loop_hint = help_row(
            "夹在「循环开始 / 循环结束」中间的步骤会重复执行。",
            "循环怎么填", LOOP_HELP)
        form.addRow("", self.loop_hint)

        # --- 条件节点（condition_start）---
        self.cond_mode_combo = QComboBox()
        for key, label in COND_MODES:
            self.cond_mode_combo.addItem(label, key)
        self.cond_mode_combo.currentIndexChanged.connect(self._on_cond_mode_changed)
        form.addRow("判断方式：", self.cond_mode_combo)

        self.cond_expr_edit = QLineEdit()
        self.cond_expr_edit.setPlaceholderText("条件要判断的数据，如 {{loop.item.标题}}")
        self.cond_var_combo = QComboBox()
        self.cond_var_combo.setMinimumWidth(190)
        self.cond_var_combo.setToolTip("选择后把变量插入到「判断的数据」里")
        self.cond_var_combo.activated.connect(self._insert_cond_var)
        self.cond_row = QWidget()
        cond_row_layout = QHBoxLayout(self.cond_row)
        cond_row_layout.setContentsMargins(0, 0, 0, 0)
        cond_row_layout.addWidget(self.cond_expr_edit, 1)
        cond_row_layout.addWidget(self.cond_var_combo)
        form.addRow("判断的数据：", self.cond_row)

        self.cond_hint = QLabel("")
        self.cond_hint.setWordWrap(True)
        self.cond_hint.setStyleSheet("color: #888;")
        form.addRow("", self.cond_hint)

        self.cond_branch_hint = help_row(
            "条件体里的每个动作节点自带一条「判断方式 + 值」，"
            "从上往下第一个成立的那个执行；都不成立就整段跳过。",
            "条件与动作节点", COND_HELP)
        form.addRow("", self.cond_branch_hint)

        self._cond_widgets = [
            self.cond_mode_combo, self.cond_row, self.cond_hint,
            self.cond_branch_hint,
        ]

        # --- 自由代码节点（script）：写一个真正的函数，系统自动调用它 ---
        self.script_lang_combo = QComboBox()
        for key, label in SCRIPT_LANGS:
            self.script_lang_combo.addItem(label, key)
        form.addRow("脚本语言：", self.script_lang_combo)

        self.script_code = CodeEditor()
        self.script_code.setPlaceholderText(SCRIPT_PLACEHOLDER_PY)
        self.script_code.setMinimumHeight(210)
        form.addRow("脚本代码：", self.script_code)

        self.script_hint = help_row(
            "代码里写一个完整的函数（系统会自动调用它）。",
            "Python 脚本", SCRIPT_HINT_PY)
        self.script_help_btn = self.script_hint.findChild(HelpButton)
        form.addRow("", self.script_hint)
        # 说明和秒数后缀都跟着脚本语言变，所以等这两样都建好了再接信号
        self.script_lang_combo.currentIndexChanged.connect(
            self._on_script_lang_changed
        )

        self.script_timeout = QSpinBox()
        self.script_timeout.setRange(1, 3600)
        self.script_timeout.setValue(30)
        self.script_timeout.setSuffix(" 秒")
        form.addRow("执行超时：", self.script_timeout)

        # --- 调用函数（call）：用【项目管理…】→【函数库】里定义好的函数 ---
        self.call_func_combo = QComboBox()
        self.call_func_combo.setMinimumWidth(280)
        self.call_func_combo.setToolTip(
            "从项目的函数库里选一个函数（一处定义、多处调用）。\n"
            "还没有函数？去【项目管理…】→【函数库】新建。"
        )
        form.addRow("调用函数：", self.call_func_combo)

        call_row = QWidget()
        call_layout = QHBoxLayout(call_row)
        call_layout.setContentsMargins(0, 0, 0, 0)
        self.call_args = QLineEdit()
        self.call_args.setPlaceholderText(
            "形参名=值，如：单价={{价格}}, 倍数=2（留空＝都按空文本传）")
        self.call_args.setToolTip(
            "传给函数的实参，逗号分隔，写法 `形参名=值`：\n"
            "· 值可以写 {{变量}}，也可以是字面量（2、促销 这种）；\n"
            "· 只写名字不写 = （如 单价）＝把同名的流程变量传进去；\n"
            "· 没写的形参＝空文本。"
        )
        call_layout.addWidget(self.call_args, 1)
        self.call_var_combo = QComboBox()
        self.call_var_combo.setMinimumWidth(160)
        self.call_var_combo.setToolTip("插入一个变量（插入到光标处）")
        self.call_var_combo.activated.connect(self._insert_call_var)
        call_layout.addWidget(self.call_var_combo)
        form.addRow("参数：", call_row)

        self.call_hint = help_row(
            "函数在【项目管理…】→【函数库】里定义，改一处、所有调用一起变。",
            "调用函数", CALL_HELP)
        form.addRow("", self.call_hint)

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
        self._read_widgets = [self.read_panel]
        self._collect_widgets = [self.collect_panel]
        self._note_widgets = [self.note_box]
        self._locator_widgets = [self.locator_type, loc_row, self.locator_hint]
        self._image_widgets = [self.image_hint, self.preview]
        self._value_widgets = [value_row, self.value_hint]
        self._wait_widgets = [self.wait_combo, self.wait_seconds]
        self._pause_widgets = [self.prompt_edit, self.resume_combo,
                               self.resume_timeout]
        self._loop_widgets = [self.loop_expr_row, self.loop_hint]
        self._script_widgets = [
            self.script_lang_combo, self.script_code, self.script_hint,
        ]
        # 「执行超时」是自由代码与调用函数共用的那一行
        self._script_out_widgets = [self.script_timeout]
        self._call_widgets = [
            self.call_func_combo, call_row, self.call_hint,
        ]
        self._load_function_list()
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
        is_collect = action == "collect"
        is_locate = action in ("click", "fill", "select")
        is_fill = action in ("fill", "select")
        is_image = is_locate and self.locator_type.currentData() == "image"
        is_pause = action == "pause_for_human"
        is_script = action == "script"
        is_call = action == "call"
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
        # 「条件判断」只在编辑「条件里的动作节点」时出现
        for w in self._rule_widgets:
            self._show(w, self.rule_mode is not None)
        self.rule_op_combo.setVisible(self.rule_mode == "rule")
        for w in self._read_widgets:
            self._show(w, is_read)
        for w in self._collect_widgets:
            self._show(w, is_collect)
        for w in self._note_widgets:
            self._show(w, action == "note")
        # 产出变量名：读取 / 采集都要填
        self._show(self.output_var_edit, is_read or is_collect)
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
        for w in self._script_out_widgets:
            self._show(w, is_script or is_call)
        for w in self._call_widgets:
            self._show(w, is_call)
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
            (self.note_var_combo, "插入变量 ▾"),
            (self.call_var_combo, "插入变量 ▾"),
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
                "或者新增一个「读取数据」/「采集数据」节点让它产出变量。"
            )

    def _insert_variable(self, index: int):
        """把选中的变量插入输入值光标处。"""
        name = self.var_combo.itemData(index)
        if not name:
            return
        self.value_edit.insert(f"{{{{{name}}}}}")
        self.value_edit.setFocus()
        self.var_combo.setCurrentIndex(0)

    def _insert_note_var(self, index: int):
        """把选中的变量插到「提示内容」的光标处。"""
        name = self.note_var_combo.itemData(index)
        self.note_var_combo.setCurrentIndex(0)
        if not name:
            return
        self.note_text.insertPlainText(f"{{{{{name}}}}}")
        self.note_text.setFocus()

    def _insert_loop_expr_var(self, index: int):
        """把选中的变量插入到「循环内容」光标处。"""
        name = self.loop_expr_var_combo.itemData(index)
        if not name:
            return
        self.loop_expr_edit.insert(f"{{{{{name}}}}}")
        self.loop_expr_edit.setFocus()
        self.loop_expr_var_combo.setCurrentIndex(0)

    def _insert_call_var(self, index: int):
        """把选中的变量插到「参数」的光标处（写成 {{变量}}）。"""
        name = self.call_var_combo.itemData(index)
        if not name:
            return
        self.call_args.insert(f"{{{{{name}}}}}")
        self.call_args.setFocus()
        self.call_var_combo.setCurrentIndex(0)

    def _image_names(self) -> List[str]:
        """项目 img/ 里有哪些图片名（解析代码里的 /图片名 用）。"""
        out: List[str] = []
        if self.img_dir.is_dir():
            for p in sorted(self.img_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in IMG_EXTS:
                    out.append(p.stem)
                    out.append(p.name)
        return out

    def _load_function_list(self):
        """把项目函数库里的函数填进「调用函数」下拉。"""
        self.call_func_combo.clear()
        self.call_func_combo.addItem("（选一个函数…）", "")
        try:
            funcs = project_store.ProjectStore(self.project_dir).load_functions()
        except Exception:
            funcs = []
        for f in funcs:
            lang = "JS" if str(f.get("lang")).lower() == "javascript" else "Python"
            params = (f.get("params") or "").strip()
            tip = f"{f['name']}（{lang}）"
            if params:
                tip += f"\n形参：{params}"
            if (f.get("desc") or "").strip():
                tip += f"\n{f['desc'].strip()}"
            self.call_func_combo.addItem(f"{f['name']}（{lang}）", f["name"])
            self.call_func_combo.setItemData(
                self.call_func_combo.count() - 1, tip,
                Qt.ItemDataRole.ToolTipRole)
        if not funcs:
            self.call_func_combo.setItemText(
                0, "（还没有函数：去【项目管理…】→【函数库】新建）")

    def _on_script_lang_changed(self):
        is_js = self.script_lang_combo.currentData() == "javascript"
        if self.script_help_btn is not None:
            self.script_help_btn.set_content(
                "JavaScript 脚本" if is_js else "Python 脚本",
                SCRIPT_HINT_JS if is_js else SCRIPT_HINT_PY)
        self.script_code.setPlaceholderText(
            SCRIPT_PLACEHOLDER_JS if is_js else SCRIPT_PLACEHOLDER_PY)

    # ------------------------------
    # 条件节点
    # ------------------------------
    def _on_cond_mode_changed(self):
        is_expr = self.cond_mode_combo.currentData() == "expr"
        self.cond_expr_edit.setPlaceholderText(
            "Python 表达式，如 len({{loop.item.内容}}) > 500"
            if is_expr else "条件要判断的数据，如 {{loop.item.标题}}"
        )
        self.cond_hint.setText(
            "表达式里可以直接写 {{变量}}（系统会按数字/文本自动代入）；\n"
            "算出来是真/假 → 按动作节点的先后走（真走第 1 个、假走第 2 个），"
            "算出来是别的值 → 按各动作节点的值匹配。"
            if is_expr else
            "条件只负责给出这份数据；判断方式写在块里的每个动作节点上，\n"
            "从上往下第一个成立的执行，都不成立就跳过。"
        )

    def _insert_cond_var(self, index: int):
        """把选中的变量插入到「判断的数据」光标处。"""
        name = self.cond_var_combo.itemData(index)
        if not name:
            return
        self.cond_expr_edit.insert(f"{{{{{name}}}}}")
        self.cond_expr_edit.setFocus()
        self.cond_var_combo.setCurrentIndex(0)

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
        data = pick_element(self, url, self.project_dir)
        if not data:
            return
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
                saved = save_captured_locator(self, self.project_dir, data)
                more = (f"　已存成元素定位 {{{{{saved}}}}}（定位里写它就能复用）"
                        if saved else "")
                self.capture_hint.setText(f"已捕获：{desc} → {xpath}{warn}{more}")
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

    def _capture_collect_row(self):
        """采集节点里点【捕获元素…】：抓页面上「一行」的 XPath 当行定位。"""
        try:
            url = self.url_edit.text().strip() or self._default_url
            if not url:
                QMessageBox.information(
                    self, "先填网址",
                    "这一步（或项目里第一个「打开网页」）还没有网址，\n"
                    "捕获器不知道该打开哪个页面。",
                )
                return
            data = pick_element(self, url, self.project_dir)
            if not data:
                return
            xpath = (data.get("xpath") or "").strip()
            if not xpath:
                QMessageBox.information(self, "没抓到 XPath",
                                        "换个元素再点一下试试。")
                return
            count = data.get("count", 1)
            note = f"已捕获：{data.get('desc') or '元素'} → {xpath}　命中 {count} 个"
            if count <= 1:
                note += ("；只命中 1 个——列表定位要能圈住每一行，"
                         "把 XPath 里 [1] 这样的序号删掉试试")
            self.collect_panel.set_row_locator(xpath, note)
            drop_capture_image(self.project_dir, data)     # 只要 XPath，不要那张图
        except Exception as e:
            QMessageBox.critical(self, "捕获失败", f"{type(e).__name__}: {e}")

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
        self.script_timeout.setValue(s.script_timeout or 30)
        self._on_script_lang_changed()

        # 调用函数：函数可能已经被改名 / 删掉，那就把它补进下拉，别让用户白改
        name = (s.func_name or "").strip()
        idx = self.call_func_combo.findData(name)
        if name and idx < 0:
            self.call_func_combo.addItem(f"{name}（已不在函数库里）", name)
            idx = self.call_func_combo.count() - 1
        self.call_func_combo.setCurrentIndex(max(0, idx))
        self.call_args.setText(s.func_args or "")

        self.output_var_edit.setText(s.output_var or "")
        self.read_panel.load(s.data_cfg or {})
        self.collect_panel.load(s)
        self.note_text.setPlainText(s.text or "")
        self.loop_expr_edit.setText(s.loop_expr or "")
        self.win_title_edit.setText(s.win_title or "")
        self.keys_edit.setText(s.keys or "")
        self.click_times_combo.setCurrentIndex(max(
            0, self.click_times_combo.findData(int(s.click_times or 1))))

        mode_idx = self.cond_mode_combo.findData(s.cond_mode or "rule")
        self.cond_mode_combo.setCurrentIndex(max(0, mode_idx))
        self.cond_expr_edit.setText(s.cond_expr or "")
        self._on_cond_mode_changed()
        if self.rule_mode is not None:
            self.rule_op_combo.setCurrentIndex(
                max(0, self.rule_op_combo.findData(s.cond_op or "")))
            self.rule_value_edit.setText(s.cond_value or "")

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
        elif action == "collect":
            if not self.output_var_edit.text().strip():
                errors.append(
                    "「采集数据」必须填一个产出变量名（如 采集结果）——"
                    "后面的步骤就是靠这个名字引用采到的数据的"
                )
            problem = self.collect_panel.validate()
            if problem:
                errors.append("「采集数据」：" + problem)
        elif action == "loop_start":
            if not self.loop_expr_edit.text().strip():
                errors.append(
                    "「循环」必须填循环内容："
                    "写数字＝跑几次（如 10），或写变量＝按它的长度跑（如 {{文章列表}}）"
                )
        elif action == "condition_start":
            if not self.cond_expr_edit.text().strip():
                errors.append(
                    "条件节点必须填写「判断的数据」"
                    "（规则模式填变量，如 {{loop.item.标题}}；表达式模式写 Python 表达式）"
                )
        elif action == "script":
            code = self.script_code.toPlainText()
            if not code.strip():
                errors.append("自由代码节点必须写一段代码（一个完整的函数定义）")
            else:
                # 用和执行时同一套解析：找函数、认 @变量 /图片 #文件、查语法
                fc = free_code.analyze(
                    code, self.script_lang_combo.currentData(),
                    self._var_names_list, self._image_names())
                errors.extend(fc.errors)
                if not errors:
                    self._register_functions(fc, code)
        elif action == "call":
            if not self.call_func_combo.currentData():
                errors.append(
                    "调用函数节点必须选一个函数"
                    "（还没定义过？去【项目管理…】→【函数库】新建一个）"
                )

        # 这个节点在「条件」里：选了判断方式却没填值，跑起来永远不会成立
        if self.rule_mode == "rule":
            op = self.rule_op_combo.currentData() or ""
            if op and not self.rule_value_edit.text().strip():
                errors.append(
                    f"「条件判断」选了「{blocks.COND_OP_CN.get(op, op)}」，"
                    "但没填要比较的值"
                )

        if errors:
            QMessageBox.warning(self, "内容不完整", "\n".join(f"· {e}" for e in errors))
            return
        self.accept()

    def _register_functions(self, fc, code: str):
        """保存自由代码节点时，把代码里的函数写进【函数库】（一处定义多处调用）。

        存的是**这个函数自己的那段原文**（@名字 写法原样保留），
        这样别处「调用函数」时不会误跑到同一个代码框里的另一个函数。
        """
        try:
            store = project_store.ProjectStore(self.project_dir)
            funcs = store.load_functions()
        except Exception:
            return
        lang = self.script_lang_combo.currentData() or "python"
        by_name = {f["name"]: f for f in funcs}
        changed = False
        for fn in fc.funcs:
            entry = {
                "name": fn.name,
                "lang": lang,
                "params": ", ".join(p.name for p in fn.params),
                "code": free_code.function_source(code, fn),
                "desc": str(by_name.get(fn.name, {}).get("desc") or ""),
            }
            if by_name.get(fn.name) != entry:
                by_name[fn.name] = entry
                changed = True
        if changed:
            store.save_functions(list(by_name.values()))

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
        elif action == "collect":
            step.output_var = self.output_var_edit.text().strip()
            mode, row, fields = self.collect_panel.config()
            step.collect_mode = mode
            step.collect_row = row
            step.collect_fields = fields
        elif action == "note":
            step.text = self.note_text.toPlainText().strip()
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
            step.script_timeout = self.script_timeout.value()
        elif action == "call":
            step.func_name = self.call_func_combo.currentData() or ""
            step.func_args = self.call_args.text().strip()
            step.script_timeout = self.script_timeout.value()
        elif action == "loop_start":
            step.loop_expr = self.loop_expr_edit.text().strip()
        elif action == "condition_start":
            step.cond_mode = self.cond_mode_combo.currentData()
            step.cond_expr = self.cond_expr_edit.text().strip()

        if self.rule_mode is not None:
            # 这个节点摆在「条件」里：它自带的规则
            step.cond_op = self.rule_op_combo.currentData() or ""
            step.cond_value = self.rule_value_edit.text().strip()

        step.note = self.note_edit.text().strip()
        return step
