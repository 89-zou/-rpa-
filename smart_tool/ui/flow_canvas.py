# -*- coding: utf-8 -*-
"""Coze 风格编排画布（QGraphicsView）。

- 每个步骤是一张可自由拖动的圆角卡片，按 steps 列表顺序连线
- 默认【横向蛇形排版】：从左到右排列，超出宽度自动换行，
  下一行反向（右→左），使连线始终最短；可随时点【自动排版】重排
- 「循环开始/结束」「条件/分支/条件结束」这类配套节点用虚线框圈在一起：
  **结束端（循环结束 / 条件结束）在画布上不画卡片**（只在【流程编辑】里显示）
- 自动连线只在**最外层**按「单元」顺序连：一个单元＝一个普通步骤卡片，或一个
  循环 / 条件（整块当一端，箭头直接落在它的虚线框上）；**块内部（循环体 /
  条件体 / 分支体）一条自动箭头都不画**，先后顺序看编号就够了
- 想画框内的箭头：选中框内节点 → 点它右边出现的小箭头 → 再点另一个节点，
  连这一条（要再连一条就再操作一遍）；**双击**橙色箭头即可删掉它。
  纯画布展示，不动执行顺序（存在 steps.json 的 canvas_edges 里，执行器不读）
- 虚线框只有循环（紫）和条件（蓝）两种：**分支不套框**（靠分支卡片区分）
- **组合节点**：把连着的一串步骤合成一个、起个名字，画布上只显示一张卡片
  （内部的行不画，所以它内部的循环 / 条件也不画框）。合并 / 取消合并只能在
  【流程编辑】里做；画布上双击它只会提示去哪儿展开
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
TYPE_LINE_H = 9        # 标题下面的「类型行」（点击 / 填入 / 循环开始…）
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
    "read_data": ("读取数据", "#0f766e"),
    "click": ("点击", "#e8890c"),
    "fill": ("填入", "#1a9d6b"),
    "select": ("下拉选择", "#1192a8"),
    "pause_for_human": ("暂停等人工", "#d1495b"),
    "loop_start": ("循环开始", "#7a4fb5"),
    "loop_end": ("循环结束", "#7a4fb5"),
    "condition_start": ("条件", "#2f6fb3"),
    "condition_end": ("条件结束", "#2f6fb3"),
    "branch": ("分支", "#5b8fd0"),
    "collect": ("采集数据", "#9d174d"),
    "note": ("提示 / 日志", "#a16207"),
    "group_start": ("组合", "#0d7a6a"),
    "group_end": ("组合结束", "#0d7a6a"),
    "script": ("自由代码", "#475569"),
    # 桌面场景
    "win_activate": ("激活窗口", "#7c3aed"),
    "hotkey": ("按键", "#0d9488"),
    "delay": ("等待", "#64748b"),
}


def node_height_for(step: Step, lines: Optional[List[str]] = None) -> float:
    """卡片高度（排版与绘制共用同一算法，避免对不齐）。

    lines 给的是「这张卡片实际要画几行」——组合卡片显示的不是步骤摘要，
    所以要把行数显式传进来，否则算出来的高度和画出来的对不上。
    """
    if lines is None:
        lines = step_summary(step)
    return HEADER_H + TYPE_LINE_H + max(1, len(lines)) * BODY_LINE_H + BODY_PAD * 2


def group_card_lines(count: int, skip_if_logged_in: bool = False) -> List[str]:
    """组合卡片上的正文：里面收了几步 + 去哪儿展开。"""
    second = ("登录态有效时自动跳过（登录用）" if skip_if_logged_in
              else "在【流程编辑…】里展开 / 取消组合")
    return [f"组合（{count} 个步骤）", second]


def wrap_for_card(text: str, cols: int = 19, max_lines: int = 3) -> List[str]:
    """把一段话按卡片宽度折行（一个中文占 2 格、西文占 1 格），最多 max_lines 行。

    给「提示 / 日志」节点用：它整张卡片就是一段话，不折行的话会被截成一条。
    """
    paras = [p.strip() for p in str(text or "").splitlines()]
    paras = [p for p in paras if p]
    if not paras:
        return ["（写点什么当提示）"]
    total = sum(2 if ord(ch) > 127 else 1 for p in paras for ch in p)
    lines: List[str] = []
    shown = 0
    for para in paras:
        cur, used = "", 0
        for ch in para:
            w = 2 if ord(ch) > 127 else 1
            if used + w > cols * 2:
                lines.append(cur)
                shown += used
                cur, used = "", 0
                if len(lines) >= max_lines:
                    break
            cur += ch
            used += w
        if len(lines) >= max_lines:
            break
        if cur:
            lines.append(cur)
            shown += used
        if len(lines) >= max_lines:
            break
    if not lines:
        return ["（写点什么当提示）"]
    if shown < total:
        lines[-1] += "…"          # 还有没画完的，末尾给个省略号（后面会被 elide 兜住）
    return lines[:max_lines]


def step_summary(s: Step, branch_text: str = "") -> List[str]:
    """卡片正文最多 3 行摘要（branch_text 是分支标记从所属条件里取的匹配值）。"""
    if s.action == "note":
        return wrap_for_card(s.text)
    if s.action == "navigate":
        return [s.url or "（未填网址）"]
    if s.action == "read_data":
        cfg = s.data_cfg or {}
        path = cfg.get("path") or ""
        fields = [(m.get("var") or "").strip()
                  for m in (cfg.get("field_map") or []) if isinstance(m, dict)]
        fields = [f for f in fields if f]
        lines = [f"读：{Path(path).name}" if path else "（未选文件 / 文件夹）"]
        lines.append(f"产出 {{{{{s.output_var}}}}}"
                     if s.output_var else "（未填产出变量名）")
        if fields:
            lines.append("字段：" + "、".join(fields[:4])
                         + ("…" if len(fields) > 4 else ""))
        return lines[:3]
    if s.action == "click" and s.locator:
        times = "双击" if int(s.click_times or 1) >= 2 else "单击"
        head = (f"{times}：{s.locator.value[:56]}" if s.locator.type == "image"
                else f"点击：{s.locator.value[:60]}")
        return [head]
    if s.action in ("fill", "select"):
        lines = [f"值：{s.value or '（空）'}"]
        if s.locator:
            lines.append(f"定位：{s.locator.value[:50]}")
        return lines[:3]
    if s.action == "win_activate":
        return [f"窗口：{s.win_title[:50]}" if s.win_title
                else "（未填窗口标题）"]
    if s.action == "hotkey":
        return [f"按键：{s.keys}" if s.keys else "（未填按键）"]
    if s.action == "delay":
        return [f"等 {float(s.wait_seconds or 0):g} 秒"]
    if s.action == "pause_for_human":
        cond = {
            "manual": "人工继续", "url_changed": "URL 变化",
            "element_present": "元素出现", "url_and_element": "URL+元素",
        }.get(s.resume_condition, s.resume_condition)
        return [s.prompt[:50] or "（无提示）", f"恢复：{cond}（{s.resume_timeout}s）"]
    if s.action == "loop_start":
        expr = (s.loop_expr or "").strip()
        if not expr:
            return ["循环内容（未填写）"]
        if expr.isdigit():
            return [f"循环 {expr} 次（{{{{loop.item}}}} 是序号，0 起）"]
        return [f"循环：{expr[:60]}"]
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
    if s.action == "group_start":
        lines = [s.title or "（未命名组合）",
                 "登录态有效时自动跳过（登录用）" if s.skip_if_logged_in
                 else "把连着的一串步骤收成一张卡片"]
        return lines
    if s.action == "group_end":
        return ["组合到此结束"]
    if s.action == "collect":
        fields = [str(f.get("name") or "").strip()
                  for f in (s.collect_fields or []) if isinstance(f, dict)]
        fields = [f for f in fields if f]
        mode = "列表" if (s.collect_mode or "page") == "list" else "当前页"
        if fields:
            head = f"{mode}采集：{'、'.join(fields[:4])}" + ("…" if len(fields) > 4 else "")
        else:
            head = "（还没加要采集的字段）"
        var = (s.output_var or "").strip()
        if not var:
            tail = "（未填产出变量名）"
        elif mode == "列表":
            tail = f"产出 {{{{{var}}}}}（可配循环逐行遍历）"
        else:
            tail = f"产出 {{{{{var}.字段}}}}，并可存 data/"
        return [head, tail]
    if s.action == "script":
        lang = "JavaScript" if (s.script_lang or "").lower() == "javascript" else "Python"
        first = ""
        for line in (s.script_code or "").splitlines():
            if line.strip():
                first = line.strip()
                break
        return [f"{lang} 脚本：{first[:40]}" if first else f"{lang} 脚本（未填代码）"]
    return []


class _ConnectHandle(QGraphicsItem):
    """节点被选中时，出现在它右边的小箭头：点它就从这里连一条线。

    点箭头 → 再点另一个节点 / 框 = 连一条，连完自动结束（不做常驻模式，
    因为只是偶尔连一条，常驻反而碍事）。
    """

    SIZE = 11.0

    def __init__(self, node: "NodeItem"):
        super().__init__(node)
        self._canvas = node.canvas
        self._node = node
        self.setZValue(20)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("从这里连一条线：点我，再点另一个节点 / 循环框 / 条件框")
        self.setVisible(False)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.SIZE, self.SIZE)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#2f6fed"), 0.8))
        painter.setBrush(QBrush(QColor("#e8f0ff")))
        painter.drawEllipse(self.boundingRect().adjusted(0.4, 0.4, -0.4, -0.4))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#2f6fed")))
        tri = QPainterPath()
        tri.moveTo(3.0, 3.2)
        tri.lineTo(8.4, 5.5)
        tri.lineTo(3.0, 7.8)
        tri.closeSubpath()
        painter.drawPath(tri)

    def mousePressEvent(self, event):
        # 必须自己吃掉这次点击，否则会被父节点当成「拖动卡片」
        event.accept()
        self._canvas.begin_connect(self._node)


class NodeItem(QGraphicsItem):
    """步骤卡片。"""

    def __init__(self, step: Step, canvas: "FlowCanvas",
                 branch_text: str = "", lines: Optional[List[str]] = None,
                 number: str = ""):
        super().__init__()
        self.step = step
        self.canvas = canvas
        self._canvas = canvas
        self._name, color = ACTION_META.get(step.action, (step.action, "#888888"))
        self._color = QColor(color)
        self._lines = lines if lines is not None else step_summary(step, branch_text)
        self._h = node_height_for(step, self._lines)
        # 显示编号：组合显示成「2-4」这种范围，见 blocks.step_numbers
        self._number = number or ""

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        # 不用缓存：卡片小、数量少，滚动缩放时保持清晰（缓存会按原分辨率失真）
        self.setCacheMode(QGraphicsItem.CacheMode.NoCache)
        self.setZValue(10)
        if step.pos:
            self.setPos(QPointF(step.pos[0], step.pos[1]))
        self._press_pos: Optional[QPointF] = None
        # 选中时出现在右侧的连线小箭头（只有块内的节点才显示：
        # 最外层本来就自动连了，不需要手动连）
        self._connectable = False
        self.connect_handle = _ConnectHandle(self)
        self.connect_handle.setPos(
            NODE_W + 2, self._h / 2 - _ConnectHandle.SIZE / 2
        )

    def set_connectable(self, flag: bool):
        """标记这个节点要不要给连线小箭头（块内才给）。"""
        self._connectable = bool(flag)
        self._sync_handle()

    def _sync_handle(self):
        self.connect_handle.setVisible(
            self._connectable and self.isSelected() and not self._canvas._readonly
        )

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
            self._sync_handle()
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

        # 标题：编号 + 自定义名称（没写名称就用动作默认名）
        painter.setPen(QPen(QColor("#ffffff")))
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSizeF(TITLE_FONT_PT)
        painter.setFont(title_font)
        title = f"{self._number}. {self.step.title or self._name}" \
            if self._number else (self.step.title or self._name)
        painter.drawText(
            QRectF(5, 0, NODE_W - 9, HEADER_H),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            title,
        )

        # 类型行：动作名（点击 / 填入 / 循环开始…），小字灰色，一眼看出这是什么节点
        painter.setPen(QPen(QColor("#8a94a2")))
        type_font = QFont()
        type_font.setPointSizeF(BODY_FONT_PT)
        painter.setFont(type_font)
        fm_type = QFontMetrics(type_font)
        painter.drawText(
            QRectF(4, HEADER_H, NODE_W - 8, TYPE_LINE_H),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            fm_type.elidedText(self._name, Qt.TextElideMode.ElideRight,
                               int(NODE_W - 8)),
        )

        # 正文摘要
        painter.setPen(QPen(QColor("#444444")))
        body_font = QFont()
        body_font.setPointSizeF(BODY_FONT_PT)
        painter.setFont(body_font)
        fm = QFontMetrics(body_font)
        y = HEADER_H + TYPE_LINE_H + BODY_PAD
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
                 arrow: bool = True, width: float = 1.0):
        super().__init__()
        self._end = QPointF()
        self._arrow = arrow
        pen = QPen(QColor(color), width)
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
# 画布上用虚线框表示这一块的结束位置就够了。
# 组合体内部的行也不画（只留「组合开始」那一张卡片），见 blocks.card_hidden_indices
HIDDEN_ACTIONS = blocks.END_MARKERS


def _region_pad(depth: int) -> Tuple[float, float, float, float]:
    """块框的留白：层级越深留白越大，保证嵌套时里面的框不会被外面的框压住。"""
    d = 13.0 * max(0, depth)
    return (-14 - d, -22 - d, 14 + d, 14 + d)


# 会画虚线框的块：循环紫、条件蓝（分支不画框，靠卡片与扇出箭头区分）
REGION_COLORS = {
    "loop": "#7a4fb5",
    "condition": "#2f6fb3",
}

# 手动连线的颜色（橙色，和自动连线的灰色区分开）
MANUAL_EDGE_COLOR = "#f08a24"


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

    @property
    def span(self):
        """这个框对应的块范围（连线模式里认它是「哪一块」）。"""
        return self._span

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

    def mousePressEvent(self, event):
        """左键：连线途中 → 完成连线；其余走默认。

        手动连线的橙色箭头**不在这里删**：单击太容易误碰（拖动、选中节点时
        手一滑就把线删了），改成双击才删，见 mouseDoubleClickEvent。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self._canvas.has_pending_connect():
            self._canvas.finish_connect(self.itemAt(event.pos()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击一条橙色箭头＝删掉它（单击不再删）。"""
        item = self.itemAt(event.pos())
        if isinstance(item, EdgeItem) and self._canvas.delete_manual_edge(item):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

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
        # 和双击一样「延后一拍」再发信号：
        # 这一下右键还在 Qt 的事件分发栈里（场景正在处理它），而菜单里的
        # 「新增 / 编辑步骤」会重建整个画布（图元被销毁），事件分发返回时
        # 还要接着访问那张卡片——和双击弹编辑框是同一个坑，照同样的办法躲。
        item = self.itemAt(pos)
        sid = item.step.id if isinstance(item, NodeItem) else None
        canvas, gpos = self._canvas, self.mapToGlobal(pos)
        QTimer.singleShot(0, lambda: canvas.context_menu_requested.emit(sid, gpos))


class FlowCanvas(QWidget):
    """对外的画布控件。"""

    selection_changed = pyqtSignal()
    node_activated = pyqtSignal(int)                       # 双击
    context_menu_requested = pyqtSignal(object, object)    # (step_id|None, 全局pos)
    positions_changed = pyqtSignal()                       # 拖拽结束
    auto_layout_applied = pyqtSignal()                     # 自动排版完成（需落盘）
    edges_changed = pyqtSignal()                           # 手动连线增删（需落盘）
    connect_status = pyqtSignal(str)                       # 连线模式的状态提示

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
        self._nodes: Dict[int, NodeItem] = {}
        self._edges: List[EdgeItem] = []
        self._edge_pairs: List[Tuple[EdgeItem, object, object]] = []
        self._spans: List[blocks.Span] = []
        self._manual_edges: List[Tuple[object, object]] = []   # 手动连线（ref 对）
        self._manual_edge_items: List[EdgeItem] = []           # 与 _manual_edges 一一对应
        self._pending_source = None                            # 正在连线的起点（点小箭头后设上）
        self._readonly = False                                 # 执行期间禁止拖动/连线
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
        hidden = blocks.card_hidden_indices(steps)
        span_map = {sp.start: sp for sp in blocks.spans(steps)}
        cards = [(i, s) for i, s in enumerate(steps) if i not in hidden]
        if not cards:
            return
        per_row = max(1, per_row or self.compute_per_row())
        heights = [node_height_for(s, self._card_lines(i, s, span_map))
                   for i, s in cards]
        # 块框会往外扩（层级越深越大），行距跟着放大，免得框压到上一行
        max_depth = max((sp.depth for sp in blocks.spans(steps)), default=0)
        gap_y = GAP_Y + 13.0 * (max_depth + 1)

        y = float(MARGIN_Y)
        for row_start in range(0, len(cards), per_row):
            row_end = min(row_start + per_row, len(cards))
            row_h = max(heights[row_start:row_end])
            row_idx = row_start // per_row
            for i in range(row_start, row_end):
                col = i % per_row
                if row_idx % 2 == 1:        # 奇数行反向（蛇形回折）
                    col = per_row - 1 - col
                cards[i][1].pos = [
                    float(MARGIN_X + col * (NODE_W + GAP_X)),
                    y,
                ]
            y += row_h + gap_y
        # 不画卡片的行：给个占位坐标，免得每次都被判成「缺位置」而重排
        for i in range(len(steps)):
            if i in hidden and i > 0 and steps[i - 1].pos:
                steps[i].pos = list(steps[i - 1].pos)

    def _card_lines(self, index: int, step: Step,
                    span_map: dict) -> Optional[List[str]]:
        """这张卡片实际要画的正文行；普通节点返回 None（用步骤摘要）。"""
        if step.action != blocks.GROUP_START:
            return None
        sp = span_map.get(index)
        return group_card_lines(blocks.inner_count(sp) if sp else 0,
                                step.skip_if_logged_in)

    def apply_auto_layout(self):
        """对外入口：按当前宽度重排全部节点并刷新画布。"""
        if not self._steps:
            return
        self.assign_auto_layout(self._steps)
        # 重排后节点会回到起点附近，视野也跟着回到流程开头
        self._rebuild_scene(keep_view=False)
        self.auto_layout_applied.emit()

    def place_missing_positions(self, steps: List[Step]) -> bool:
        """只给「还没有坐标」的步骤安排位置，已有坐标的节点一个都不动。

        为什么不整体重排：画布上的布局是用户自己摆的（拖过、对过齐），
        加一个节点就把整片打乱太难受了。这里只给新节点找一个不压到任何
        现有卡片的空格，其余照旧。想整体重排有【自动排版】按钮。
        """
        hidden = blocks.card_hidden_indices(steps)
        span_map = {sp.start: sp for sp in blocks.spans(steps)}
        taken: List[QRectF] = []
        missing: List[int] = []
        for i, s in enumerate(steps):
            if i in hidden:
                continue
            if s.pos:
                taken.append(QRectF(s.pos[0], s.pos[1], NODE_W,
                                    node_height_for(s, self._card_lines(
                                        i, s, span_map))))
            else:
                missing.append(i)
        if not missing:
            return False
        per_row = max(1, self.compute_per_row())
        for i in missing:
            step = steps[i]
            h = node_height_for(step, self._card_lines(i, step, span_map))
            spot = self._free_spot(taken, h, per_row)
            step.pos = [spot.x(), spot.y()]
            taken.append(QRectF(spot.x(), spot.y(), NODE_W, h))
        return True

    def _free_spot(self, taken: List[QRectF], height: float,
                   per_row: int) -> QPointF:
        """按横向蛇形的格子顺序找第一个不压到现有卡片的空位。"""
        row_h = height + GAP_Y
        for row in range(200):              # 上限只是兜底，正常第一屏就能找到
            for col in range(per_row):
                x = MARGIN_X + col * (NODE_W + GAP_X)
                y = MARGIN_Y + row * row_h
                # 四面八方留出半个间距，挨着别的卡片也不算挤
                probe = QRectF(x - GAP_X / 2, y - GAP_Y / 2,
                               NODE_W + GAP_X, height + GAP_Y)
                if not any(probe.intersects(t) for t in taken):
                    return QPointF(x, y)
        return QPointF(MARGIN_X, MARGIN_Y)

    # ------------------------------
    # 数据装载
    # ------------------------------
    def load_steps(self, steps: List[Step],
                   select_id: Optional[int] = None,
                   auto_layout: Optional[bool] = None,
                   keep_view: Optional[bool] = None):
        """重建画布。

        :param auto_layout: True 才整体重排（【自动排版】按钮 / 排版版本变了）；
                            None / False＝只给新节点补个空位，其余节点原地不动
        :param keep_view: 是否保持当前视野。默认：重排/换项目→回到流程开头，
                          仅改内容→保持视野（避免编辑后视图乱跳）
        """
        self._steps = steps

        if auto_layout:
            self.assign_auto_layout(steps)
        else:
            # 新增的节点还没坐标：给它找个空位就好，别动用户摆好的布局
            self.place_missing_positions(steps)
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
        self._manual_edge_items = []
        self._spans = []
        self._regions = []
        self._region_spans = []
        self._placeholder = None
        self._pending_source = None      # 重建后起点引用会失效，直接清掉
        self._scene.clear()

        self._spans = blocks.spans(self._steps)
        hidden = blocks.card_hidden_indices(self._steps)
        span_map = {sp.start: sp for sp in self._spans}
        numbers = blocks.step_numbers(self._steps)
        for i, s in enumerate(self._steps):
            if i in hidden:
                continue        # 结束端标记、以及组合体内部：画布上不画卡片
            node = NodeItem(s, self,
                            branch_text=self._branch_text_of(s),
                            lines=self._card_lines(i, s, span_map),
                            number=numbers[i])
            # 块内的节点才给「连线小箭头」：最外层是自动连好的，不用手动连
            node.set_connectable(self._inside_block(s))
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

    def _inside_block(self, step: Step) -> bool:
        """这个步骤是不是在某个块（循环体 / 条件体 / 分支体）里面。"""
        idx = next((i for i, s in enumerate(self._steps) if s is step), -1)
        return idx >= 0 and blocks.enclosing_span(self._spans, idx) is not None

    def _endpoint(self, ref):
        """连线单元 → 端点对象（步骤卡片或块虚线框）。

        - loop box → 循环紫框；condition box → 条件蓝框：
          连接到「一个块」时直接接到它最外层的虚线框上，
          箭头不必伸进框里去够里面的卡片；
        - branch box → 分支卡本身（分支不套框，没有框可接）；
        - group box → 组合那张卡片（组合不套框，画布上就一张卡片）。
        """
        if isinstance(ref, tuple):
            idx = ref[1]
            sp = self._span_by_start(idx)
            if sp is None:
                return None
            if sp.kind in ("branch", "group"):
                return self._nodes.get(self._steps[idx].id)
            return _LoopBoxPort(self._span_rect(sp))
        return self._nodes.get(ref)

    def _units_in(self, lo: int, hi: int) -> List[object]:
        """把 [lo, hi) 里的步骤按「单元」切开：块整块算一个单元。

        块的端点见 _endpoint：循环 / 条件接到各自的虚线框上，分支接到分支卡上，
        组合接到它自己那张卡片上。
        """
        units: List[object] = []
        hidden = blocks.card_hidden_indices(self._steps)
        i = lo
        while i < hi:
            sp = self._span_by_start(i)
            complete = sp is not None and (
                sp.inner_hi <= hi if sp.kind == "branch" else sp.end < hi
            )
            if complete:
                units.append(("box", i))
                i = sp.inner_hi if sp.kind == "branch" else sp.end + 1
            elif i in hidden:
                i += 1          # 不画卡片的行（结束标记 / 组合体内部），不参与连线
            else:
                units.append(self._steps[i].id)
                i += 1
        return units

    def _build_edges(self):
        """自动连线：**只在最外层**按「单元」顺序连。

        - 一个单元＝一个普通步骤卡片，或者一个循环 / 条件（整块当一端，
          箭头直接落在它的虚线框上），所以「4. 点击 → 5. 循环框」会连上；
        - **块内部（循环体、条件体、分支体）一条自动箭头都不画**：
          先后顺序看编号就够了，也不会把并行的分支画成顺序；
        - 想画哪条，选中框内节点 → 点它右边的小箭头 → 再点另一个节点，
          只连这一条（要再连一条就再操作一遍）。
        """
        units = self._units_in(0, len(self._steps))
        for k in range(len(units) - 1):
            self._add_edge(units[k], units[k + 1])
        self._build_manual_edges()

    def _add_edge(self, ref_a, ref_b) -> None:
        """连一条灰色箭头（任一端拿不到就跳过）。"""
        a, b = self._endpoint(ref_a), self._endpoint(ref_b)
        if a is None or b is None:
            return
        edge = EdgeItem(color="#9aa4b2")
        self._scene.addItem(edge)
        edge.connect_nodes(a, b)
        self._edges.append(edge)
        self._edge_pairs.append((edge, ref_a, ref_b))

    # ------------------------------
    # 手动连线（只在画布上展示，不参与执行顺序）
    # ------------------------------
    def begin_connect(self, node: "NodeItem"):
        """点了节点右边的小箭头：从这里开始连一条线。

        不做成「常驻模式」——连一条就够了，连完自动结束；
        要再连一条，重新点一次小箭头。
        """
        ref = self._anchor_of_item(node)
        if ref is None:
            return
        if self._pending_source == ref:
            self._pending_source = None
            self.connect_status.emit("已取消连线。")
            return
        self._pending_source = ref
        node.setSelected(True)
        self.connect_status.emit(
            f"起点：{self._anchor_label(ref)} → 再点一个节点 / 循环框 / 条件框；"
            "点空白处取消。"
        )

    def has_pending_connect(self) -> bool:
        """是否正等着点终点。"""
        return self._pending_source is not None

    def finish_connect(self, item) -> None:
        """连线途中点了另一个图元：连上或取消。"""
        if self._pending_source is None:
            return
        src = self._pending_source
        self._pending_source = None
        if item is None:
            self.connect_status.emit("已取消连线（点到了空白处）。")
            return
        ref = self._anchor_of_item(item)
        if ref is None:
            self.connect_status.emit("只能连到节点卡片或循环 / 条件框，已取消。")
            return
        if ref == src:
            self.connect_status.emit("起点和终点是同一个，已取消。")
            return
        if (src, ref) in self._manual_edges:
            self.connect_status.emit("这两点之间已经有连线了。")
            return
        self._manual_edges.append((src, ref))
        self._rebuild_scene(keep_view=True)
        self.edges_changed.emit()
        self.connect_status.emit(
            "已连上（橙色箭头）。要再连一条，重新点节点右边的小箭头。"
        )

    def delete_manual_edge(self, item) -> bool:
        """双击一条橙色箭头＝删掉它；返回是否真的删了。"""
        if item not in self._manual_edge_items:
            return False
        idx = self._manual_edge_items.index(item)
        self._manual_edges.pop(idx)
        self._rebuild_scene(keep_view=True)
        self.edges_changed.emit()
        self.connect_status.emit("已删除这条手动连线。")
        return True

    def set_manual_edges(self, edges: List[list]):
        """载入手动连线。

        每一端写成：步骤 id（卡片）或 "frame:<起始步骤id>"（循环 / 条件框）。
        端点已经不存在的连线直接丢掉——步骤 id 会随增删重排，
        留着的话会连到别的步骤上。
        """
        self._manual_edges = []
        for pair in edges or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            a, b = self._parse_anchor(pair[0]), self._parse_anchor(pair[1])
            if a is None or b is None or a == b:
                continue
            self._manual_edges.append((a, b))
        if self._steps:
            self._rebuild_scene(keep_view=True)

    def manual_edges(self) -> List[list]:
        """导出成可写进 steps.json 的样子。"""
        return [[self._anchor_value(a), self._anchor_value(b)]
                for a, b in self._manual_edges]

    def _parse_anchor(self, value):
        """手动连线的一端 → 内部 ref（步骤 id 或 ("box", 下标)）；无效返回 None。"""
        if isinstance(value, str) and value.startswith("frame:"):
            try:
                sid = int(value.split(":", 1)[1])
            except ValueError:
                return None
            for i, s in enumerate(self._steps):
                if s.id == sid:
                    sp = self._span_by_start(i)
                    return ("box", i) if (sp and sp.kind in REGION_COLORS) else None
            return None
        try:
            sid = int(value)
        except (TypeError, ValueError):
            return None
        return sid if any(s.id == sid for s in self._steps) else None

    def _anchor_value(self, ref):
        """内部 ref → 写进 json 的值。"""
        if isinstance(ref, tuple):
            return f"frame:{self._steps[ref[1]].id}"
        return ref

    def _build_manual_edges(self):
        """画手动连的箭头（橙色，比自动箭头粗一点，一眼能认出来）。"""
        for ref_a, ref_b in self._manual_edges:
            a, b = self._endpoint(ref_a), self._endpoint(ref_b)
            if a is None or b is None:
                continue
            edge = EdgeItem(color=MANUAL_EDGE_COLOR, width=1.4)
            self._scene.addItem(edge)
            edge.connect_nodes(a, b)
            self._edges.append(edge)
            self._edge_pairs.append((edge, ref_a, ref_b))
            self._manual_edge_items.append(edge)   # 与 _manual_edges 一一对应

    # ------------------------------
    # 连线端点识别
    # ------------------------------
    def _anchor_of_item(self, item):
        """图元 → 连线端点 ref；不是可连的图元返回 None。"""
        if isinstance(item, NodeItem):
            return item.step.id
        if isinstance(item, LoopRegion) and item.span is not None:
            return ("box", item.span.start)
        return None

    def _anchor_label(self, ref) -> str:
        """连线两端的名字（编号用显示编号，跟画布上看到的一致）。"""
        if isinstance(ref, tuple):
            sp = self._span_by_start(ref[1])
            if sp is None:
                return "（框）"
            num = blocks.step_numbers(self._steps)[sp.start]
            label = self._region_label(sp)
            return f"{num}. {label}" if num else label
        idx = next((i for i, s in enumerate(self._steps) if s.id == ref), -1)
        if idx < 0:
            return f"#{ref}"
        step = self._steps[idx]
        name, _ = ACTION_META.get(step.action, (step.action, ""))
        shown = step.title or name
        num = blocks.step_numbers(self._steps)[idx]
        return f"{num}. {shown}" if num else shown

    def _build_regions(self):
        """循环 / 条件各画一个虚线框（嵌套时框也嵌套）；分支、组合不画框。

        组合里面收着的循环 / 条件也不画框——组合在画布上就是一张卡片，
        框画出来反而像是「组合里还有东西露在外面」。

        注意：先把所有 LoopRegion 对象都建好（此时 _span_rect 还不准，
        因为子 region 可能还没注册到 _region_spans），然后按 depth 从大到小
        重算 rect——内层先准确，外层 union 子层时才能拿到正确的框范围。
        """
        in_group = set()
        for gsp in blocks.group_spans(self._spans):
            in_group.update(range(gsp.start, gsp.end + 1))
        # 第一轮：创建所有 region 对象，先占位注册到列表里
        pending: List[Tuple[LoopRegion, blocks.Span]] = []
        for sp in self._spans:
            if sp.kind not in REGION_COLORS:
                continue        # 分支 / 组合不套框
            if sp.start in in_group:
                continue        # 被组合收起来了，不画
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
        expr = (step.loop_expr or "").strip()
        if not expr:
            return "↳ 循环体（还没填循环内容）"
        if expr.isdigit():
            return f"↳ 循环体（跑 {expr} 次）"
        return f"↳ 循环体（{expr}，每一项重复）"

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
        """执行期间禁止拖动、也收起连线小箭头。"""
        self._readonly = bool(readonly)
        for node in self._nodes.values():
            node.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
                         not readonly)
            node._sync_handle()

    def fit_all(self):
        if self._nodes:
            self._view.fitInView(
                self._scene.itemsBoundingRect().adjusted(-60, -60, 60, 60),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
