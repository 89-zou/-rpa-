# -*- coding: utf-8 -*-
"""桌面元素捕获的「眼睛」：用 Windows UI Automation 查鼠标下面是哪个控件。

桌面没有 XPath，但 Windows 的 UIA 能直接告诉我们：
    这里的控件叫什么（Name）、是什么类型（ButtonControl / EditControl…）、
    类名（QPushButton / Edit…）、**精确的屏幕矩形**、以及它属于哪个窗口。

有了矩形就能「点一下就自动裁出这个控件」——比手工拖框准得多，交互也跟网页那边一致。
拿不到 UIA 的程序（自绘界面、部分老程序、权限不足）返回 None，调用方退回手动拖框。

坐标口径：UIA 给的矩形是**物理像素**，与全屏截图、pyautogui 一致（实测过）。
"""
from dataclasses import dataclass
from typing import Optional, Tuple

# 常见控件类型的中文说法（UIA 的 ControlTypeName）
KIND_CN = {
    "ButtonControl": "按钮",
    "SplitButtonControl": "下拉按钮",
    "EditControl": "输入框",
    "TextControl": "文字",
    "ComboBoxControl": "下拉框",
    "ListControl": "列表",
    "ListItemControl": "列表项",
    "MenuControl": "菜单",
    "MenuItemControl": "菜单项",
    "MenuBarControl": "菜单栏",
    "CheckBoxControl": "勾选框",
    "RadioButtonControl": "单选框",
    "TabControl": "标签页",
    "TabItemControl": "标签页",
    "WindowControl": "窗口",
    "PaneControl": "面板",
    "GroupControl": "分组",
    "DocumentControl": "文档区",
    "HyperlinkControl": "链接",
    "TreeControl": "树",
    "TreeItemControl": "树节点",
    "DataItemControl": "数据项",
    "DataGridControl": "数据表格",
    "TableControl": "表格",
    "HeaderControl": "表头",
    "HeaderItemControl": "表头项",
    "ImageControl": "图片",
    "SliderControl": "滑块",
    "SpinnerControl": "数字框",
    "ProgressBarControl": "进度条",
    "ScrollBarControl": "滚动条",
    "StatusBarControl": "状态栏",
    "ToolBarControl": "工具栏",
    "TitleBarControl": "标题栏",
    "ThumbControl": "缩放柄",
    "CustomControl": "自定义控件",
    "CalendarControl": "日历",
    "IPAddressControl": "IP 输入框",
    "SeparatorControl": "分隔线",
}

# 矩形大到这种程度就当成「桌面/根节点」，不算有效控件（否则会裁出一整屏）
COVER_RATIO = 0.95


@dataclass
class UiControl:
    """鼠标底下的一个界面控件。"""
    name: str
    kind: str            # ButtonControl 这种 UIA 类型名
    cls: str             # 类名，如 QPushButton
    rect: Tuple[int, int, int, int]     # 屏幕物理像素：left, top, right, bottom
    window_title: str    # 所属顶层窗口的标题（可以填进「激活窗口」）

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def kind_cn(self) -> str:
        return KIND_CN.get(self.kind, self.kind)

    def describe(self) -> str:
        """人话描述：名字（类型）+ 尺寸。"""
        label = self.name.strip() or self.cls or self.kind_cn
        return f"{label}（{self.kind_cn}）{self.width}×{self.height}"


def available() -> bool:
    """UIA 能不能用（uiautomation 装了、且系统是 Windows）。"""
    import sys
    if sys.platform != "win32":
        return False
    try:
        import uiautomation  # noqa: F401
    except Exception:
        return False
    return True


def missing_hint() -> str:
    return ("桌面元素捕获需要 uiautomation（拿到控件名字和精确位置），"
            "没装也能用【截屏取模板…】手工框选。\n"
            "    装法：.venv\\Scripts\\python -m pip install uiautomation")


def _auto():
    try:
        import uiautomation as auto
    except Exception as e:
        raise RuntimeError(f"没能加载 uiautomation：{e}\n{missing_hint()}") from e
    return auto


def _screen_size() -> Tuple[int, int]:
    """虚拟桌面总宽高（多屏时包含所有屏）；拿不到就返回 0。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(78), u.GetSystemMetrics(79)  # SM_CX/CYVIRTUALSCREEN
    except Exception:
        return 0, 0


def control_at(x: float, y: float, up: int = 0) -> Optional[UiControl]:
    """查这个屏幕坐标（物理像素）下面的控件。

    :param up: 往父控件走的层数（0＝就是鼠标下那个；1＝它的父容器……）
    返回 None 表示这里查不到有意义的控件（桌面空白、自绘界面、UIA 不响应）。

    整个函数**不会抛异常**：调用它的地方是 Qt 定时器槽，抛出去会把程序搞崩。
    """
    try:
        auto = _auto()
        ctrl = auto.ControlFromPoint(int(round(x)), int(round(y)))
        for _ in range(max(0, int(up))):
            if ctrl is None:
                return None
            ctrl = ctrl.GetParentControl()
        return _describe(ctrl)
    except Exception:
        return None


def window_at(x: float, y: float) -> Optional[UiControl]:
    """这个屏幕坐标所在的**顶层窗口**（整窗矩形 + 窗口标题）。

    和 control_at 的区别：这个一路走到顶层窗口。用途是把「找控件的范围」
    从整屏缩到一个窗口，也用来在捕获时把整窗裁出来当窗口模板。

    注意：**最大化窗口的矩形就是整屏**，所以这里不能像 _describe 那样
    把「整屏大小」当成桌面根节点丢掉 —— 那会漏掉最常见的情况。
    整个函数不会抛异常（调用方可能是 Qt 定时器槽）。
    """
    try:
        auto = _auto()
        ctrl = auto.ControlFromPoint(int(round(x)), int(round(y)))
        if ctrl is None:
            return None
        root = ctrl.GetTopLevelControl() or ctrl
        r = root.BoundingRectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            return None
        try:
            name = root.Name or ""
        except Exception:
            name = ""
        try:
            cls = root.ClassName or ""
        except Exception:
            cls = ""
        return UiControl(name=name, kind="WindowControl", cls=cls,
                         rect=rect, window_title=name)
    except Exception:
        return None


def _describe(ctrl) -> Optional[UiControl]:
    """UIA 控件 → UiControl；不合适的（桌面根、空矩形）返回 None。"""
    if ctrl is None:
        return None
    try:
        r = ctrl.BoundingRectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    sw, sh = _screen_size()
    if sw > 0 and sh > 0:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w >= sw * COVER_RATIO and h >= sh * COVER_RATIO:
            return None                     # 整屏大小 → 这是桌面根节点
    try:
        name = ctrl.Name or ""
    except Exception:
        name = ""
    try:
        kind = ctrl.ControlTypeName or ""
    except Exception:
        kind = ""
    try:
        cls = ctrl.ClassName or ""
    except Exception:
        cls = ""
    top = ""
    try:
        root = ctrl.GetTopLevelControl()
        top = (root.Name or "") if root is not None else ""
    except Exception:
        top = ""
    return UiControl(name=name, kind=kind, cls=cls, rect=rect, window_title=top)
