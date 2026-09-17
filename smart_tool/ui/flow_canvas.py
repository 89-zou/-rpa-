# -*- coding: utf-8 -*-
"""Coze 风格编排画布（QGraphicsView）。

- 每个步骤是一张可自由拖动的圆角卡片，按 steps 列表顺序连线
- 默认【横向蛇形排版】：从左到右排列，超出宽度自动换行，
  下一行反向（右→左），使连线始终最短；可随时点【自动排版】重排
- 「循环开始/结束」「条件/分支/条件结束」这类配套节点用虚线框圈在一起：
  **结束端（循环结束 / 条件结束）在画布上不画卡片**（只在【流程编辑】里显示），
  所有 block（loop / condition）在其父层都被折叠成黑盒——**框本身就是端点**，
  父层只看到"黑盒进、黑盒出"，不关心 block 内部结构；block 自己内部的箭头由递归画：
    - loop 内部：loop_start 卡 → 循环体第一单元（入口卡→卡）；
      循环体最后单元 → loop 框（回路指向 loop 框底，框本身闭合回路）；
      loop 体内再嵌套的 block 继续递归
    - condition 内部：条件卡 → 各分支卡（蓝色扇出），每个分支卡 → 分支体步骤（顺序）
    - branch 不画出口箭头（执行完自然走到条件结束）
- 虚线框只有循环（紫）和条件（蓝）两种：**分支不套框**（靠分支卡片与扇出箭头区分）
- **拖虚线框＝整块移动**（块里嵌套的块与所有卡片一起走）；拖单个卡片仍是单独移动
- 块可以嵌套（分支里放循环等），框按层级一层层套
- 位置持久化到每个步骤的 pos；执行顺序由步骤列表顺序决定
"""
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    QPointF, QRectF, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QSizePolicy, QVBoxLayout, QWidget,
)

from smart_tool.core import blocks
from smart_tool.core.project_store import Step

# 排版版本：与 steps.json 里的 layout 不一致时自动重排。
# h1→h2：卡片尺寸减半，旧坐标间距过大，需按新尺寸重排。
LAYOUT_VERSION = "h2"

# 卡片尺寸（保持紧凑，便于一屏看清整条流程；Ctrl+滚轮可放大细看）
NODE_W = 140
HEADER_H = 16
BODY_LINE_H = 9
BODY_PAD = 3

# 横向蛇形排版参数
GAP_X = 26
GAP_Y = 30
MARGIN_X = 18
MARGIN_Y = 18
DEFAULT_PER_ROW = 6
MAX_PER_ROW = 12

# 场景留白：起点侧留小一点（内容不会缩在一个巨大空白画布的角落），
# 拖动方向留足空间，方便继续往外拖
SCENE_PAD_START = 40
SCENE_PAD_END = 600
# 节点坐标安全范围（防止用惯性/误操作把节点甩到极远处）
POS_MIN = -20000.0
POS_MAX = 20000.0

# 空画布提示语
DEFAULT_PLACEHOLDER = ("流程为空：点上方【流程编辑…】开始编排\n"
                       "节点可自由拖动，Ctrl+滚轮缩放，双击节点编辑")
NO_PROJECT_PLACEHOLDER = ("尚未载入项目：点上方【载入项目…】选择项目\n"
                          "载入后可编排步骤、配置数据源并运行")

# 字号（配合小卡片；缩放时由视图 transform 统一放大）
TITLE_FONT_PT = 6.0
BODY_FONT_PT = 5.0
LABEL_FONT_PT = 6.0
CARD_RADIUS = 3.5

ACTION_META = {
    # action: (中文名, 主题色)
    "navigate": ("打开网页", "#2f6fed"),
    "click": ("点击", "#e8890c"),
    "fill": ("填入", "#1a9d6b"),
    "select": ("下拉选择", "#1192a8"),
    "pause_for_human": ("暂停等人工", "#d1495b"),
    "loop_start": ("循环开始", "#7a4fb5"),
    "loop_end": ("循环结束", "#7a4fb5"),
    "condition_start": ("条件", "#2f6fb3"),
    "condition_end": ("条件结束", "#2f6fb3"),
    "branch": ("分支", "#5b8fd0"),
    "script": ("自由代码", "#475569"),
}


def node_height_for(step: Step, data_source: dict) -> float:
    """卡片高度（排版与绘制共用同一算法，避免对不齐）。"""
    lines = max(1, len(step_summary(step, data_source)))
    return HEADER_H + lines * BODY_LINE_H + BODY_PAD * 2


def _range_summary(text: str) -> str:
    """索引范围的卡片摘要：字面量算出次数，含变量时原样显示。"""
    t = (text or "").strip()
    if not t:
        return "索引范围（未填写）"
    parts = [x.strip() for x in t.split("-")]
    if len(parts) == 2 and all(x.isdigit() for x in parts):
        a, b = int(parts[0]), int(parts[1])
        return f"索引 {a}–{b}（{max(0, b - a + 1)} 次）"
    if t.isdigit():
        n = int(t)
        return f"索引 0–{n - 1}（{n} 次）" if n > 0 else "索引范围（0 次）"
    return f"索引范围：{t}"


def step_summary(s: Step, data_source: dict,
                 branch_text: str = "") -> List[str]:
    """卡片正文最多 3 行摘要（branch_text 是分支标记从所属条件里取的匹配值）。"""
    if s.action == "navigate":
        return [s.url or "（未填网址）"]
    if s.action == "click" and s.locator:
        return [f"点击：{s.locator.value[:60]}"]
    if s.action in ("fill", "select"):
        lines = [f"值：{s.value or '（空）'}"]
        if s.locator:
            lines.append(f"定位：{s.locator.value[:50]}")
        return lines[:3]
    if s.action == "pause_for_human":
        cond = {
            "manual": "人工继续", "url_changed": "URL 变化",
            "element_present": "元素出现", "url_and_element": "URL+元素",
        }.get(s.resume_condition, s.resume_condition)
        return [s.prompt[:50] or "（无提示）", f"恢复：{cond}（{s.resume_timeout}s）"]
    if s.action == "loop_start":
        if (s.loop_source or "data") == "range":
            return [_range_summary(s.loop_range)]
        if (s.loop_source or "data") == "list":
            items = [x.strip() for x in (s.loop_items or "").splitlines()
                     if x.strip()]
            if not items:
                return ["遍历列表（未填写循环项）"]
            more = f"，共 {len(items)} 项" if len(items) > 1 else ""
            return [f"遍历列表：{items[0]}{more}"]
        p = (data_source or {}).get("path", "")
        return [f"数据源：{Path(p).name}" if p else "未配置数据源（请点【数据源…】）"]
    if s.action == "loop_end":
        return ["循环体到此结束"]
    if s.action == "condition_start":
        mode = "表达式" if (s.cond_mode or "equal") == "expr" else "变量相等"
        lines = [f"{mode}：{s.cond_expr or '（未填判断内容）'}"]
        lines.append(f"{len(s.cond_branches or [])} 个分支，命中哪个走哪个")
        return lines
    if s.action == "branch":
        return [branch_text or "（匹配值在条件节点里改）"]
    if s.action == "condition_end":
        return ["条件体到此结束"]
    if s.action == "script":
        lang = "JavaScript" if (s.script_lang or "").lower() == "javascript" else "Python"
        first = ""
        for line in (s.script_code or "").splitlines():
            if line.strip():
                first = line.strip()
                break
        return [f"{lang} 脚本：{first[:40]}" if first else f"{lang} 脚本（未填代码）"]
    return []


class NodeItem(QGraphicsItem):
    """步骤卡片。"""

    def __init__(self, step: Step, data_source: dict, canvas: "FlowCanvas",
                 branch_text: str = ""):
        super().__init__()
        self.step = step
        self._canvas = canvas
        self._name, color = ACTION_META.get(step.action, (step.action, "#888888"))
        self._color = QColor(color)
        self._lines = step_summary(step, data_source, branch_text)
        self._h = node_height_for(step, data_source)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        # 不用缓存：卡片小、数量少，滚动缩放时保持清晰（缓存会按原分辨率失真）
        self.setCacheMode(QGraphicsItem.CacheMode.NoCache)
        self.setZValue(10)
        if step.pos:
            self.setPos(QPointF(step.pos[0], step.pos[1]))
        self._press_pos: Optional[QPointF] = None

    @property
    def node_height(self) -> float:
        return self._h

    # ---- 连接锚点 ----
    def center(self) -> QPointF:
        """卡片中心（判断「是否同一行」用，比左上角稳）。"""
        return self.mapToScene(QPointF(NODE_W / 2, self._h / 2))

    def top_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W / 2, 0))

    def bottom_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W / 2, self._h))

    def left_port(self) -> QPointF:
        return self.mapToScene(QPointF(0, self._h / 2))

    def right_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W, self._h / 2))

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, NODE_W, self._h)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # 限制坐标范围，避免节点被甩到极远处；向左上拖动允许负坐标
            p = value
            return QPointF(min(max(p.x(), POS_MIN), POS_MAX),
                           min(max(p.y(), POS_MIN), POS_MAX))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            if not self._canvas._block_moving:
                self._canvas.scene_refresh_overlays()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._press_pos is not None and self.pos() != self._press_pos:
            self.step.pos = [self.pos().x(), self.pos().y()]
            self._canvas.on_node_moved()
        self._press_pos = None

    def mouseDoubleClickEvent(self, event):
        # 先把事件交回基类，再「延后一拍」发信号打开编辑弹窗。
        # 若在事件处理过程中就把画布整体重建（编辑保存会重建），
        # 当前这个图元会被销毁，而 Qt 事件分发在处理器返回后还会访问它，
        # 于是抛异常、整个程序毫无提示地退出（闪退）。
        super().mouseDoubleClickEvent(event)
        canvas, step_id = self._canvas, self.step.id
        QTimer.singleShot(0, lambda: canvas.node_activated.emit(step_id))

    def paint(self, painter: QPainter, option, widget=None):
        rect = self.boundingRect()
        radius = CARD_RADIUS

        # 卡片主体
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#cfd6e0"), 0.9))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawPath(path)

        # 顶部色条（圆角只画上半部分）
        header = QRectF(0, 0, NODE_W, HEADER_H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawRoundedRect(header, radius, radius)
        painter.drawRect(QRectF(0, HEADER_H - radius, NODE_W, radius))

        # 标题
        painter.setPen(QPen(QColor("#ffffff")))
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSizeF(TITLE_FONT_PT)
        painter.setFont(title_font)
        title = f"{self.step.id}. {self._name}"
        painter.drawText(
            QRectF(5, 0, NODE_W - 9, HEADER_H),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            title,
        )

        # 正文摘要
        painter.setPen(QPen(QColor("#444444")))
        body_font = QFont()
        body_font.setPointSizeF(BODY_FONT_PT)
        painter.setFont(body_font)
        fm = QFontMetrics(body_font)
        y = HEADER_H + BODY_PAD
        for line in self._lines:
            text = fm.elidedText(line, Qt.TextElideMode.ElideRight, NODE_W - 8)
            painter.drawText(
                QRectF(4, y, NODE_W - 8, BODY_LINE_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            y += BODY_LINE_H

        # 选中描边
        if self.isSelected():
            painter.setPen(QPen(QColor("#2f6fed"), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                                    radius, radius)


class EdgeItem(QGraphicsPathItem):
    """节点之间的连线（可虚线、可带箭头；自动选择横/竖贝塞尔走向）。"""

    def __init__(self, color: str = "#9aa4b2", dashed: bool = False,
                 arrow: bool = True):
        super().__init__()
        self._end = QPointF()
        self._arrow = arrow
        pen = QPen(QColor(color), 1.0)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self._color = QColor(color)
        self.setZValue(-1)

    def update_path(self, p1: QPointF, p2: QPointF, horizontal: bool = False):
        self._end = p2
        path = QPainterPath(p1)
        if horizontal:
            dx = (p2.x() - p1.x()) / 2
            path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
        else:
            dy = max(14.0, (p2.y() - p1.y()) / 2)
            path.cubicTo(p1.x(), p1.y() + dy, p2.x(), p2.y() - dy, p2.x(), p2.y())
        self.setPath(path)

    def connect_nodes(self, a, b):
        """相邻两端连线：同行走左右，跨行走上/下。

        a、b 可以是步骤卡片（NodeItem），也可以是循环虚线框（_LoopBoxPort）——
        循环整块当成一个端点，外部箭头直接接到框上。
        """
        ac, bc = a.center(), b.center()
        dx, dy = bc.x() - ac.x(), bc.y() - ac.y()
        same_row = abs(dy) < 20

        if same_row:
            if dx >= 0:
                self.update_path(a.right_port(), b.left_port(), horizontal=True)
            else:
                self.update_path(a.left_port(), b.right_port(), horizontal=True)
        elif dy > 0:
            self.update_path(a.bottom_port(), b.top_port())
        else:
            self.update_path(a.top_port(), b.bottom_port())

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not self._arrow:
            return
        path = self.path()
        if path.length() <= 0:
            return
        t = path.percentAtLength(max(0.0, path.length() - 1))
        angle = path.angleAtPercent(t)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self._end)
        painter.rotate(-angle)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        tri = QPainterPath()
        tri.moveTo(0, 0)
        tri.lineTo(-4.5, -2.5)
        tri.lineTo(-4.5, 2.5)
        tri.closeSubpath()
        painter.drawPath(tri)
        painter.restore()


# 画布上不画卡片的标记：成对标记的「结束」那一端只在【流程编辑】里显示，
# 画布上用虚线框表示这一块的结束位置就够了
HIDDEN_ACTIONS = ("loop_end", "condition_end")


def _region_pad(depth: int) -> Tuple[float, float, float, float]:
    """块框的留白：层级越深留白越大，保证嵌套时里面的框不会被外面的框压住。"""
    d = 13.0 * max(0, depth)
    return (-14 - d, -22 - d, 14 + d, 14 + d)


# 会画虚线框的块：循环紫、条件蓝（分支不画框，靠卡片与扇出箭头区分）
REGION_COLORS = {
    "loop": "#7a4fb5",
    "condition": "#2f6fb3",
}


class _LoopBoxPort:
    """把块虚线框当成一个连线端点：外部箭头接到框上，框内部不画箭头。

    只需要提供 connect_nodes 用到的那几个锚点，接口与 NodeItem 一致。
    """

    def __init__(self, rect: QRectF):
        self._rect = rect

    def center(self) -> QPointF:
        return self._rect.center()

    def left_port(self) -> QPointF:
        return QPointF(self._rect.left(), self._rect.center().y())

    def right_port(self) -> QPointF:
        return QPointF(self._rect.right(), self._rect.center().y())

    def top_port(self) -> QPointF:
        return QPointF(self._rect.center().x(), self._rect.top())

    def bottom_port(self) -> QPointF:
        return QPointF(self._rect.center().x(), self._rect.bottom())


class LoopRegion(QGraphicsRectItem):
    """块虚线背景框（循环 / 条件共用，靠颜色区分）。

    拖这个框＝把块里所有卡片一起移动；块里的卡片照样能单独拖。
    """

    def __init__(self, color: str = "#7a4fb5", canvas=None, span=None):
        super().__init__()
        self._label = ""
        self._color = QColor(color)
        self._canvas = canvas
        self._span = span
        self._press: Optional[QPointF] = None
        self.setZValue(-10)
        self.setPen(QPen(self._color, 1.0, Qt.PenStyle.DashLine))
        fill = QColor(color)
        fill.setAlpha(22)
        self.setBrush(QBrush(fill))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("拖动这里：整个块（含里面的步骤）一起移动")

    def update_rect(self, rect: QRectF, label: str):
        self._label = label
        self.setRect(rect)
        self.update()

    # ---- 拖框 = 整块移动 ----
    def mousePressEvent(self, event):
        if self._canvas is None or self._span is None:
            super().mousePressEvent(event)
            return
        self._press = event.scenePos()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None:
            super().mouseMoveEvent(event)
            return
        now = event.scenePos()
        delta = now - self._press
        if delta.isNull():
            return
        self._press = now
        self._canvas.move_block(self._span, delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._press is None:
            super().mouseReleaseEvent(event)
            return
        self._press = None
        self._canvas.commit_block_move()
        event.accept()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.pen())
        painter.setBrush(self.brush())
        painter.drawRoundedRect(self.rect(), 6, 6)
        painter.setPen(QPen(self._color))
        font = QFont()
        font.setBold(True)
        font.setPointSizeF(LABEL_FONT_PT)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(6, 2, -6, 0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            self._label,
        )


class _View(QGraphicsView):
    """画布视图：Ctrl+滚轮缩放，右键菜单转发。"""

    MIN_SCALE = 0.2
    MAX_SCALE = 5.0
    ZOOM_STEP = 1.15

    def __init__(self, canvas: "FlowCanvas"):
        super().__init__()
        self._canvas = canvas
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setAcceptDrops(False)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def wheelEvent(self, event):
        """Ctrl+滚轮 自由缩放（以鼠标位置为中心）；普通滚轮仍为上下滚动。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            cur = self.transform().m11()
            factor = (self.ZOOM_STEP if event.angleDelta().y() > 0
                      else 1 / self.ZOOM_STEP)
            target = cur * factor
            if self.MIN_SCALE <= target <= self.MAX_SCALE:
                self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def current_scale(self) -> float:
        return self.transform().m11()

    def _show_menu(self, pos):
        item = self.itemAt(pos)
        sid = item.step.id if isinstance(item, NodeItem) else None
        self._canvas.context_menu_requested.emit(sid, self.mapToGlobal(pos))


class FlowCanvas(QWidget):
    """对外的画布控件。"""

    selection_changed = pyqtSignal()
    node_activated = pyqtSignal(int)                       # 双击
    context_menu_requested = pyqtSignal(object, object)    # (step_id|None, 全局pos)
    positions_changed = pyqtSignal()                       # 拖拽结束
    auto_layout_applied = pyqtSignal()                     # 自动排版完成（需落盘）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._view = _View(self)
        self._view.setScene(self._scene)
        self._view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._steps: List[Step] = []
        self._data_source: dict = {}
        self._nodes: Dict[int, NodeItem] = {}
        self._edges: List[EdgeItem] = []
        self._edge_pairs: List[Tuple[EdgeItem, object, object]] = []
        self._spans: List[blocks.Span] = []
        self._regions: List[LoopRegion] = []
        self._region_spans: List[blocks.Span] = []   # 与 _regions 一一对应
        self._block_moving = False                   # 整块拖动中：跳过逐节点刷新
        self._placeholder = None
        # 空画布提示语（未载入项目 / 项目没有步骤 时不一样）
        self._placeholder_text = DEFAULT_PLACEHOLDER
        # 待排版：控件还没拿到真实宽度，先挂起，等显示/调整尺寸后再排
        self._layout_pending = False
        self._scene.selectionChanged.connect(self._on_scene_selection_changed)

    def set_placeholder_text(self, text: str):
        """设置空画布时的提示语。"""
        self._placeholder_text = text
        if self._placeholder is not None:
            self._placeholder.setPlainText(text)

    def _on_scene_selection_changed(self):
        """转发场景的选中变化。

        这里必须用方法转发并容错，不能直接连 self.selection_changed.emit：
        程序退出时画布先被销毁，随后场景销毁还会再发一次 selectionChanged，
        此时 emit 会抛 AttributeError；Qt 槽里的异常会让整个程序无声退出（闪退）。
        """
        try:
            self.selection_changed.emit()
        except (AttributeError, RuntimeError):
            pass

    # ------------------------------
    # 可见性/尺寸 → 延迟排版
    # ------------------------------
    def _viewport_width(self) -> int:
        """取一个可靠的可用宽度（内层视图可能还没来得及布局）。"""
        return max(self._view.viewport().width(), self.width())

    def is_wide_enough(self) -> bool:
        """宽度至少能放下一张卡片，才值得排版。"""
        return self._viewport_width() >= NODE_W + 2 * MARGIN_X

    def request_layout_when_ready(self):
        """请求横向排版。

        控件已可见且宽度可靠时立即排；否则挂起，等首次显示后
        （布局结束、拿到真实宽度）再排——避免按默认尺寸排出过窄的列数。
        """
        if self.isVisible() and self.is_wide_enough():
            self.apply_auto_layout()
        else:
            self._layout_pending = True

    def _apply_pending_layout(self):
        if not self._layout_pending:
            return
        self._layout_pending = False
        self.apply_auto_layout()

    def _maybe_apply_pending(self):
        """resize 后若挂起且已显示、宽度够用，说明布局给了真实尺寸，可以排了。

        注意必须判 isVisible()：控件尚未显示时的 resize 用的是默认尺寸
        （约 640×480），据此排版会排出过窄的列数。
        """
        if self._layout_pending and self.isVisible() and self.is_wide_enough():
            self._layout_pending = False
            self.apply_auto_layout()

    def showEvent(self, event):
        super().showEvent(event)
        if self._layout_pending:
            # showEvent 里几何尺寸还是旧的，等本轮布局结束再排
            QTimer.singleShot(0, self._apply_pending_layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._maybe_apply_pending()

    # ------------------------------
    # 横向蛇形自动排版
    # ------------------------------
    def compute_per_row(self) -> int:
        """按当前可视宽度算每行放几张卡片。"""
        vw = self._viewport_width()
        if vw < NODE_W + 2 * MARGIN_X:      # 控件还没完成布局，用默认列数
            return DEFAULT_PER_ROW
        n = int((vw - 2 * MARGIN_X + GAP_X) // (NODE_W + GAP_X))
        return max(1, min(MAX_PER_ROW, n))

    def assign_auto_layout(self, steps: List[Step],
                           per_row: Optional[int] = None):
        """横向蛇形排版：左→右填满一行后换行，下一行反向，原地写回 pos。"""
        visible = [s for s in steps if s.action not in HIDDEN_ACTIONS]
        if not visible:
            return
        per_row = max(1, per_row or self.compute_per_row())
        heights = [node_height_for(s, self._data_source) for s in visible]
        # 块框会往外扩（层级越深越大），行距跟着放大，免得框压到上一行
        max_depth = max((sp.depth for sp in blocks.spans(steps)), default=0)
        gap_y = GAP_Y + 13.0 * (max_depth + 1)

        y = float(MARGIN_Y)
        for row_start in range(0, len(visible), per_row):
            row_end = min(row_start + per_row, len(visible))
            row_h = max(heights[row_start:row_end])
            row_idx = row_start // per_row
            for i in range(row_start, row_end):
                col = i % per_row
                if row_idx % 2 == 1:        # 奇数行反向（蛇形回折）
                    col = per_row - 1 - col
                visible[i].pos = [
                    float(MARGIN_X + col * (NODE_W + GAP_X)),
                    y,
                ]
            y += row_h + gap_y
        # 不画卡片的结束标记：给个占位坐标，免得每次都被判成「缺位置」而重排
        for i, s in enumerate(steps):
            if s.action in HIDDEN_ACTIONS and i > 0 and steps[i - 1].pos:
                s.pos = list(steps[i - 1].pos)

    def apply_auto_layout(self):
        """对外入口：按当前宽度重排全部节点并刷新画布。"""
        if not self._steps:
            return
        self.assign_auto_layout(self._steps)
        # 重排后节点会回到起点附近，视野也跟着回到流程开头
        self._rebuild_scene(keep_view=False)
        self.auto_layout_applied.emit()

    # ------------------------------
    # 数据装载
    # ------------------------------
    def load_steps(self, steps: List[Step], data_source: Optional[dict] = None,
                   select_id: Optional[int] = None,
                   auto_layout: Optional[bool] = None,
                   keep_view: Optional[bool] = None):
        """重建画布。

        :param auto_layout: None 时：有节点缺位置就整体重排
        :param keep_view: 是否保持当前视野。默认：重排/换项目→回到流程开头，
                          仅改内容→保持视野（避免编辑后视图乱跳）
        """
        if data_source is not None:
            self._data_source = data_source
        self._steps = steps

        if auto_layout is None:
            auto_layout = any(s.pos is None for s in steps
                              if s.action not in HIDDEN_ACTIONS)
        if auto_layout:
            self.assign_auto_layout(steps)
        if keep_view is None:
            keep_view = not auto_layout

        self._rebuild_scene(keep_view=keep_view)

        if select_id is not None and select_id in self._nodes:
            self._nodes[select_id].setSelected(True)

    def _rebuild_scene(self, keep_view: bool = True):
        """按当前 self._steps / pos 重建全部图元。

        重建会清空场景，所以要先把视野处理干净：要么记住当前中心并复原，
        要么回到流程起点——否则每次都弹回场景左上角，编辑起来很难受。
        """
        had_content = bool(self._nodes)
        center = self._visible_center() if (keep_view and had_content) else None

        # 先丢掉 Python 侧的引用，再清空场景。
        # 顺序反了会留下「Python 里还握着已被 C++ 删除的图元」，
        # 之后任何一次垃圾回收（或退出时的清理）都可能让进程直接崩掉，
        # 表现为毫无提示的闪退（连 traceback 都没有）。
        self._nodes = {}
        self._edges = []
        self._edge_pairs = []
        self._spans = []
        self._regions = []
        self._region_spans = []
        self._placeholder = None
        self._scene.clear()

        self._spans = blocks.spans(self._steps)
        for s in self._steps:
            if s.action in HIDDEN_ACTIONS:
                continue        # 成对标记的结束端不画卡片（流程编辑里能看到）
            node = NodeItem(s, self._data_source, self,
                            branch_text=self._branch_text_of(s))
            self._scene.addItem(node)
            self._nodes[s.id] = node

        # 先建框（让 _region_spans 有值），再建边——这样画边时 _span_rect 能拿到
        # 完整的嵌套区域信息（入口/出口箭头要算块框坐标）
        self._build_regions()
        self._build_edges()

        if not self._steps:
            self._placeholder = self._scene.addText(self._placeholder_text)
            self._placeholder.setDefaultTextColor(QColor("#9aa4b2"))
            self._placeholder.setPos(120, 100)
            self._placeholder.setZValue(-20)

        self._sync_scene_rect(keep_view=False)
        if center is not None:
            self._view.centerOn(center)      # 保持原视野，编辑后不跳
        elif self._nodes:
            self.scroll_to_start()           # 首次加载：从流程起点开始看

    def _visible_center(self) -> QPointF:
        """当前视口中心对应的场景坐标。"""
        return self._view.mapToScene(self._view.viewport().rect().center())

    def _sync_scene_rect(self, keep_view: bool = True,
                         expand_only: bool = False):
        """让场景范围始终包住所有节点。

        这是"节点能拖到可见视图之外、回不来"的根因：场景范围若不同步，
        拖出去的节点会超出可滚动区域，视图滚不过去，看起来就像卡在左上角。

        :param expand_only: 拖动中只扩不缩，避免滚动条范围反复变化导致抖动
        """
        bounds = self._scene.itemsBoundingRect()
        if bounds.isNull() or bounds.isEmpty():
            rect = QRectF(0, 0, 1200, 600)
        else:
            rect = bounds.adjusted(-SCENE_PAD_START, -SCENE_PAD_START,
                                   SCENE_PAD_END, SCENE_PAD_END)
        if expand_only:
            rect = rect.united(self._scene.sceneRect())
        if rect == self._scene.sceneRect():
            return

        center = self._visible_center() if keep_view else None
        self._scene.setSceneRect(rect)
        if center is not None:
            self._view.centerOn(center)

    def scroll_to_start(self):
        """把视野移到流程起点（内容左上角），避免内容缩在角落、四周一片空白。"""
        if not self._nodes:
            return
        vp = self._view.viewport().rect()
        r = self._scene.sceneRect()
        self._view.centerOn(r.left() + vp.width() / 2,
                            r.top() + vp.height() / 2)

    # ------------------------------
    # 连线与块框
    # ------------------------------
    def _branch_text_of(self, step: Step) -> str:
        """分支标记的摘要文字：从它所属的条件的分支清单里取匹配值。"""
        if step.action != "branch":
            return ""
        idx = next((i for i, s in enumerate(self._steps) if s is step
                    or s.id == step.id), -1)
        cond = next((sp for sp in self._spans
                     if sp.kind == "condition" and sp.contains(idx)), None)
        if cond is None:
            return ""
        order = [k for k in range(cond.inner_lo, cond.inner_hi)
                 if self._steps[k].action == "branch"]
        bi = order.index(idx) if idx in order else -1
        if bi < 0:
            return ""
        cond_step = self._steps[cond.start]
        name = blocks.condition_branch_name(cond_step, bi)
        values = "、".join(blocks.condition_branch_values(cond_step, bi))
        return f"{name}：{values}" if values else name

    def _span_by_start(self, index: int):
        return next((sp for sp in self._spans if sp.start == index), None)

    def _endpoint(self, ref):
        """连线单元 → 端点对象（步骤卡片或块框）。

        所有 block（loop / condition / branch）在其父层都被折叠成 box，
        端点一律返回框（_LoopBoxPort）——这样父层只看到"黑盒进、黑盒出"，
        不关心 block 内部结构，避免跨 block 的顺序箭头穿越大片空白。
        block 自己内部的箭头（入口、回路、分支扇出等）由 _build_block_io_edges
        和递归 _collect_edges 在内部画。
        """
        if isinstance(ref, tuple):
            sp = self._span_by_start(ref[1])
            return _LoopBoxPort(self._span_rect(sp)) if sp else None
        return self._nodes.get(ref)

    def _units_in(self, lo: int, hi: int) -> List[object]:
        """把 [lo, hi) 里的步骤按「单元」切开：块整块算一个单元。

        但块的端点**不全是框**（见 _endpoint）：
        - loop 的端点是框（顶层外部连接用）
        - condition / branch 的端点是 start 卡本身（让箭头指向有业务含义的卡片，不连到框上）
        """
        units: List[object] = []
        i = lo
        while i < hi:
            sp = self._span_by_start(i)
            complete = sp is not None and (
                sp.inner_hi <= hi if sp.kind == "branch" else sp.end < hi
            )
            if complete:
                units.append(("box", i))
                i = sp.inner_hi if sp.kind == "branch" else sp.end + 1
            elif self._steps[i].action in HIDDEN_ACTIONS:
                i += 1          # 不画卡片的结束标记，不参与连线
            else:
                units.append(self._steps[i].id)
                i += 1
        return units

    def _build_edges(self):
        """分层连线。

        - 同一层里的相邻单元依次连线；
        - 循环块/条件块整块算一个单元，外部箭头直接接到框上；
        - 循环体内部：loop_start 卡 → 各步骤 → 循环框（顺序箭头）；
        - 条件框内部：「条件」节点扇出蓝色箭头指向各个「分支」；
          各分支内部画顺序箭头，分支末尾汇聚到条件框；
        - 分支内部的步骤是顺序执行的，照常连线；
        - 嵌套的循环/条件在块内部递归画箭头。
        """
        self._collect_edges(0, len(self._steps), draw=True)
        self._build_branch_fanout()

    def _build_branch_fanout(self):
        """条件 → 各分支 的箭头。"""
        for sp in self._spans:
            if sp.kind != "condition":
                continue
            cond_node = self._nodes.get(self._steps[sp.start].id)
            if cond_node is None:
                continue
            for k in range(sp.inner_lo, sp.inner_hi):
                s = self._steps[k]
                if s.action != blocks.BRANCH:
                    continue
                br_node = self._nodes.get(s.id)
                if br_node is None:
                    continue
                edge = EdgeItem(color=REGION_COLORS["condition"])
                self._scene.addItem(edge)
                edge.connect_nodes(cond_node, br_node)
                self._edges.append(edge)
                self._edge_pairs.append((edge, cond_node.step.id, br_node.step.id))

    def _collect_edges(self, lo: int, hi: int, draw: bool):
        units = self._units_in(lo, hi)
        if draw:
            for k in range(len(units) - 1):
                a, b = self._endpoint(units[k]), self._endpoint(units[k + 1])
                if a is None or b is None:
                    continue
                edge = EdgeItem(color="#9aa4b2")
                self._scene.addItem(edge)
                edge.connect_nodes(a, b)
                self._edges.append(edge)
                self._edge_pairs.append((edge, units[k], units[k + 1]))
        for u in units:
            if isinstance(u, tuple):
                sp = self._span_by_start(u[1])
                if sp is None:
                    continue
                self._build_block_io_edges(sp)
                # 递归进入块内部：
                #   loop/branch 内部步骤是顺序执行的 → draw=True
                #   condition 内部是并行分支 → draw=False（分支间没有顺序，
                #   只有蓝扇出 + 各 branch 内部自己递归 draw=True）
                self._collect_edges(
                    sp.inner_lo, sp.inner_hi,
                    draw=(sp.kind in ("loop", "branch")),
                )

    def _build_block_io_edges(self, sp):
        """画块的入口/出口箭头。

        - loop：loop_start 卡 → 循环体第一单元（入口）；循环体最后单元 → loop 框（回路，
          让 loop 框本身承担回路的终点，形成视觉闭环，不再连 loop_start 卡）
        - branch：分支卡 → 分支体第一单元；**没有出口箭头**
          （分支执行完自然走到条件结束，不需要额外箭头标示）
        - condition：入口由 _build_branch_fanout 处理（条件卡→各分支卡），
          不需要画出口
        """
        if sp.kind == "condition":
            return
        # 内部可见单元（不含开始标记本身）
        # branch 的 inner_hi 是 end+1（右闭），其他块 inner_hi 是 end（不含结束标记）
        inner_hi = sp.inner_hi if sp.kind == "branch" else sp.end
        inner_units = self._units_in(sp.inner_lo, inner_hi)
        if not inner_units:
            return

        start_node = self._nodes.get(self._steps[sp.start].id)
        first_ep = self._endpoint(inner_units[0])

        # ---- 入口：开始标记卡 → 内部第一单元（loop 与 branch 都画） ----
        if start_node and first_ep:
            edge = EdgeItem(color="#9aa4b2")
            self._scene.addItem(edge)
            edge.connect_nodes(start_node, first_ep)
            self._edges.append(edge)
            self._edge_pairs.append((edge, start_node.step.id, inner_units[0]))

        # ---- 出口：只给 loop 画 → **loop 框**形成回路（不连 loop_start 卡） ----
        # 让 loop 框本身成为回路的视觉终点，合并"从条件回来"的长路径
        # branch 不画出口，condition 也不画（扇出已经表达了并行结构）
        if sp.kind != "loop":
            return
        last_ep = self._endpoint(inner_units[-1])
        box_port = _LoopBoxPort(self._span_rect(sp))
        if last_ep and box_port:
            edge = EdgeItem(color="#9aa4b2")
            self._scene.addItem(edge)
            edge.connect_nodes(last_ep, box_port)
            self._edges.append(edge)
            # ref 存 ("box", sp.start)，这样 scene_refresh_overlays 刷新时
            # _endpoint 会返回 loop 框，回路始终指向框底
            self._edge_pairs.append((edge, inner_units[-1], ("box", sp.start)))

    def _build_regions(self):
        """循环 / 条件各画一个虚线框（嵌套时框也嵌套）；分支不画框。

        注意：先把所有 LoopRegion 对象都建好（此时 _span_rect 还不准，
        因为子 region 可能还没注册到 _region_spans），然后按 depth 从大到小
        重算 rect——内层先准确，外层 union 子层时才能拿到正确的框范围。
        """
        # 第一轮：创建所有 region 对象，先占位注册到列表里
        pending: List[Tuple[LoopRegion, blocks.Span]] = []
        for sp in self._spans:
            if sp.kind not in REGION_COLORS:
                continue        # 分支不套框：靠卡片与「条件→分支」箭头区分
            region = LoopRegion(REGION_COLORS[sp.kind], self, sp)
            self._scene.addItem(region)
            self._regions.append(region)
            self._region_spans.append(sp)
            pending.append((region, sp))

        # 第二轮：按 depth 从大到小算 rect（内层先算）
        # _direct_child_regions 会查 _region_spans，此时所有 span 都已注册
        pending.sort(key=lambda r: r[1].depth, reverse=True)
        for region, sp in pending:
            region.update_rect(self._span_rect(sp), self._region_label(sp))

    # ------------------------------
    # 拖框 = 整块移动
    # ------------------------------
    def move_block(self, span, delta: QPointF):
        """把块里的所有卡片按 delta 一起移动（拖框时用）。"""
        self._block_moving = True
        try:
            for k in range(span.start, span.end + 1):
                if k >= len(self._steps):
                    break
                node = self._nodes.get(self._steps[k].id)
                if node is not None:
                    node.setPos(node.pos() + delta)
        finally:
            self._block_moving = False
        self.scene_refresh_overlays()

    def commit_block_move(self):
        """整块移动结束：写回每个步骤的坐标并通知保存。"""
        for node in self._nodes.values():
            node.step.pos = [node.pos().x(), node.pos().y()]
        self.positions_changed.emit()

    def _span_rect(self, sp) -> QRectF:
        """块框的矩形：把块里所有卡片 + 所有直接子块的框（含它们自己的嵌套）
        圈起来，再按层级留白。——这样外层框就不会被内层框超越。"""
        rect = QRectF()
        # 1. 块自身覆盖范围内的所有卡片（含开始/结束标记卡，虽然结束标记已隐藏）
        card_end = max(sp.end, sp.inner_hi - 1)
        for k in range(sp.start, card_end + 1):
            if k >= len(self._steps):
                break
            node = self._nodes.get(self._steps[k].id)
            if node is None:
                continue
            r = QRectF(node.pos().x(), node.pos().y(), NODE_W, node.node_height)
            rect = r if rect.isNull() else rect.united(r)
        # 2. 所有被包含的子框（loop/condition）的矩形递归 union
        #    不管中间隔了多少层（branch 不画框所以可能直接跳几层），
        #    _build_regions 已按 depth 从大到小算过 rect，子框矩形此时准确
        for child_sp in self._all_contained_regions(sp):
            cr = self._span_rect(child_sp)
            rect = cr if rect.isNull() else rect.united(cr)
        x1, y1, x2, y2 = _region_pad(sp.depth)
        return rect.adjusted(x1, y1, x2, y2)

    def _all_contained_regions(self, parent_sp):
        """parent_sp 内部所有被包含的子框（loop/condition），不管隔了多少层。

        中间可能隔着 branch（不画框），所以不能只找 depth+1 的直接子。
        _build_regions 已按 depth 从大到小算过 rect，所以内层的 rect 此时已经准确。
        """
        children = []
        for sp in self._region_spans:
            if sp is parent_sp:
                continue
            if sp.start >= parent_sp.start and sp.end <= parent_sp.end:
                children.append(sp)
        return children

    def _region_label(self, sp) -> str:
        """块框左上角的说明文字。"""
        if sp.kind == "branch":
            return f"↳ 分支（{self._branch_text_of(self._steps[sp.start])}）"
        if sp.kind == "condition":
            step = self._steps[sp.start]
            mode = "表达式" if (step.cond_mode or "equal") == "expr" else "变量"
            count = sum(1 for k in range(sp.inner_lo, sp.inner_hi)
                        if self._steps[k].action == "branch")
            return f"↳ 条件（{mode}：{step.cond_expr or '未填'}，{count} 个分支）"
        step = self._steps[sp.start]
        src = (step.loop_source or "data") if step else "data"
        if src == "range":
            return f"↳ 循环体（{_range_summary(step.loop_range)}）"
        if src == "list":
            return "↳ 循环体（循环项列表，每一项重复）"
        p = (self._data_source or {}).get("path", "")
        if p:
            return f"↳ 循环体（数据源：{Path(p).name}，对每一行重复）"
        return "↳ 循环体（未配置数据源）"

    # ------------------------------
    # 运行期刷新
    # ------------------------------
    def scene_refresh_overlays(self):
        """节点拖动时实时更新连线、块框与场景范围。"""
        if not self._nodes:
            return
        for edge, ref_a, ref_b in self._edge_pairs:
            pa, pb = self._endpoint(ref_a), self._endpoint(ref_b)
            if pa is None or pb is None:
                continue
            edge.connect_nodes(pa, pb)
        for region, sp in zip(self._regions, self._region_spans):
            region.update_rect(self._span_rect(sp), self._region_label(sp))
        # 拖动中同步扩大场景范围，保证被拖远的节点仍能滚回来
        self._sync_scene_rect(keep_view=False, expand_only=True)

    def on_node_moved(self):
        self.positions_changed.emit()

    # ------------------------------
    # 对外查询
    # ------------------------------
    def selected_step_id(self) -> Optional[int]:
        for it in self._scene.selectedItems():
            if isinstance(it, NodeItem):
                return it.step.id
        return None

    def select_node(self, step_id: int):
        for sid, node in self._nodes.items():
            node.setSelected(sid == step_id)

    def set_readonly(self, readonly: bool):
        """执行期间禁止拖动。"""
        for node in self._nodes.values():
            node.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
                         not readonly)

    def fit_all(self):
        if self._nodes:
            self._view.fitInView(
                self._scene.itemsBoundingRect().adjusted(-60, -60, 60, 60),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
