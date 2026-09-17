# -*- coding: utf-8 -*-
"""Coze 风格编排画布（QGraphicsView）。

- 每个步骤是一张可自由拖动的圆角卡片，按 steps 列表顺序连线
- 默认【横向蛇形排版】：从左到右排列，超出宽度自动换行，
  下一行反向（右→左），使连线始终最短；可随时点【自动排版】重排
- 「循环开始/结束」之间用紫色虚线框圈出循环体，并画一条"下一轮"回流虚线
  （新增「循环」时这两个节点由系统一起创建，设置只存在循环开始节点上）
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

from smart_tool.core.project_store import Step, loop_ranges

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

# 循环框留白（顶部留够回流虚线的高度）
REGION_PAD = (-14, -22, 14, 14)
BACK_EDGE_LIFT = 16.0

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


def step_summary(s: Step, data_source: dict) -> List[str]:
    """卡片正文最多 3 行摘要。"""
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

    def __init__(self, step: Step, data_source: dict, canvas: "FlowCanvas"):
        super().__init__()
        self.step = step
        self._canvas = canvas
        self._name, color = ACTION_META.get(step.action, (step.action, "#888888"))
        self._color = QColor(color)
        self._lines = step_summary(step, data_source)
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
    def top_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W / 2, 0))

    def bottom_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W / 2, self._h))

    def left_port(self) -> QPointF:
        return self.mapToScene(QPointF(0, self._h / 2))

    def right_port(self) -> QPointF:
        return self.mapToScene(QPointF(NODE_W, self._h / 2))

    def left_port_top(self) -> QPointF:
        return self.mapToScene(QPointF(0, HEADER_H))

    def left_port_bottom(self) -> QPointF:
        return self.mapToScene(QPointF(0, self._h - 10))

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, NODE_W, self._h)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # 限制坐标范围，避免节点被甩到极远处；向左上拖动允许负坐标
            p = value
            return QPointF(min(max(p.x(), POS_MIN), POS_MAX),
                           min(max(p.y(), POS_MIN), POS_MAX))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
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

    def connect_nodes(self, a: NodeItem, b: NodeItem, back: bool = False):
        """相邻节点连线：同行走左右，跨行走上/下；回流边走上方弧线。"""
        ap, bp = a.pos(), b.pos()
        dx, dy = bp.x() - ap.x(), bp.y() - ap.y()
        same_row = abs(dy) < 20

        if back:
            if same_row:
                p1, p2 = a.top_port(), b.top_port()
                sp = QPointF(p1.x(), p1.y() - BACK_EDGE_LIFT)
                ep = QPointF(p2.x(), p2.y() - BACK_EDGE_LIFT)
                path = QPainterPath(sp)
                mid = (ep.x() - sp.x()) / 2
                path.cubicTo(sp.x() + mid, sp.y(), ep.x() - mid, ep.y(),
                             ep.x(), ep.y())
                path.lineTo(p2)
                self._end = p2
                self.setPath(path)
            else:
                self.update_path(a.left_port_bottom(), b.left_port_top())
            return

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


class LoopRegion(QGraphicsRectItem):
    """循环体虚线背景框。"""

    def __init__(self):
        super().__init__()
        self._label = ""
        self.setZValue(-10)
        self.setPen(QPen(QColor("#7a4fb5"), 1.0, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(122, 79, 181, 22)))

    def update_rect(self, rect: QRectF, label: str):
        self._label = label
        self.setRect(rect)
        self.update()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.pen())
        painter.setBrush(self.brush())
        painter.drawRoundedRect(self.rect(), 6, 6)
        painter.setPen(QPen(QColor("#7a4fb5")))
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
        self._edge_pairs: List[Tuple[EdgeItem, int, int, bool]] = []
        self._loop_ranges: List[Tuple[int, int]] = []
        self._regions: List[LoopRegion] = []
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
        if not steps:
            return
        per_row = max(1, per_row or self.compute_per_row())
        heights = [node_height_for(s, self._data_source) for s in steps]

        y = float(MARGIN_Y)
        for row_start in range(0, len(steps), per_row):
            row_end = min(row_start + per_row, len(steps))
            row_h = max(heights[row_start:row_end])
            row_idx = row_start // per_row
            for i in range(row_start, row_end):
                col = i % per_row
                if row_idx % 2 == 1:        # 奇数行反向（蛇形回折）
                    col = per_row - 1 - col
                steps[i].pos = [
                    float(MARGIN_X + col * (NODE_W + GAP_X)),
                    y,
                ]
            y += row_h + GAP_Y

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
            auto_layout = any(s.pos is None for s in steps)
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
        self._loop_ranges = []
        self._regions = []
        self._placeholder = None
        self._scene.clear()

        for s in self._steps:
            node = NodeItem(s, self._data_source, self)
            self._scene.addItem(node)
            self._nodes[s.id] = node

        self._build_edges()
        self._build_loop_regions()

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

    def _loop_ranges_of(self, steps: List[Step]) -> List[Tuple[int, int]]:
        """找出所有循环体的 [起, 止] 索引（不支持嵌套）。"""
        return loop_ranges(steps)

    def _build_edges(self):
        """按 steps 列表顺序连线；循环体内部连线用紫色。"""
        self._loop_ranges = self._loop_ranges_of(self._steps)
        loop_idx = set()
        for a, b in self._loop_ranges:
            loop_idx.update(range(a, b))

        for i in range(len(self._steps) - 1):
            a = self._nodes[self._steps[i].id]
            b = self._nodes[self._steps[i + 1].id]
            color = "#7a4fb5" if i in loop_idx else "#9aa4b2"
            edge = EdgeItem(color=color)
            self._scene.addItem(edge)
            edge.connect_nodes(a, b)
            self._edges.append(edge)
            self._edge_pairs.append((edge, self._steps[i].id,
                                     self._steps[i + 1].id, False))

        # 循环回流虚线：循环结束 → 循环开始
        for a_idx, b_idx in self._loop_ranges:
            n_start = self._nodes[self._steps[a_idx].id]
            n_end = self._nodes[self._steps[b_idx].id]
            back = EdgeItem(color="#7a4fb5", dashed=True)
            self._scene.addItem(back)
            back.connect_nodes(n_end, n_start, back=True)
            self._edges.append(back)
            self._edge_pairs.append(
                (back, n_end.step.id, n_start.step.id, True))

    def _build_loop_regions(self):
        for a_idx, b_idx in self._loop_ranges:
            region = LoopRegion()
            region.update_rect(self._loop_rect(a_idx, b_idx),
                               self._loop_label(self._steps[a_idx]))
            self._scene.addItem(region)
            self._regions.append(region)

    def _loop_rect(self, a_idx: int, b_idx: int) -> QRectF:
        rect = QRectF()
        for k in range(a_idx, b_idx + 1):
            node = self._nodes[self._steps[k].id]
            r = QRectF(node.pos().x(), node.pos().y(), NODE_W, node.node_height)
            rect = r if rect.isNull() else rect.united(r)
        x1, y1, x2, y2 = REGION_PAD
        return rect.adjusted(x1, y1, x2, y2)

    def _loop_label(self, start_step: Optional[Step] = None) -> str:
        src = (start_step.loop_source if start_step else "data") or "data"
        if src == "range":
            return f"↳ 循环体（{_range_summary(start_step.loop_range)}）"
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
        """节点拖动时实时更新连线、循环框与场景范围。"""
        if not self._nodes:
            return
        for edge, id_a, id_b, back in self._edge_pairs:
            node_a, node_b = self._nodes.get(id_a), self._nodes.get(id_b)
            if node_a is None or node_b is None:
                continue
            edge.connect_nodes(node_a, node_b, back=back)
        for region, (a_idx, b_idx) in zip(self._regions, self._loop_ranges):
            region.update_rect(self._loop_rect(a_idx, b_idx),
                               self._loop_label(self._steps[a_idx]))
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
