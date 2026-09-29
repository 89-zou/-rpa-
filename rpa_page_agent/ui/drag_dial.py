# -*- coding: utf-8 -*-
"""拖拽方向圆盘：点一下（或按住转）就定下方向。

角度口径跟运行时完全一致：0° ＝ 向右、90° ＝ 向下、顺时针增长
（见 core/desktop.py 的 drag_delta / angle_text）。屏幕坐标 y 是向下的，
所以 atan2(dy, dx) 算出来正好就是这个口径 —— 不用再翻符号。
"""
import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from rpa_page_agent.core.desktop import angle_text

#: 控件大小、圆半径、圆心（圆心偏上，底下留一行写字）
W = 148
H = 152
R = 52.0
CX, CY = W / 2, 58.0


class DragDial(QWidget):
    """一个能点的圆盘：箭头指着当前方向，底下写着「右下 45°」。

    点盘上任意一点＝朝那个方向；按住不放转圈＝连续改方向；
    键盘上下左右也能微调（焦点在它上面时）。
    """

    angleChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self.setFixedSize(W, H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "在圆盘上点一下定方向（也可以按住转）：\n"
            "0°＝向右、90°＝向下、180°＝向左、270°＝向上，顺时针。\n"
            "方向键可以一格格微调（按住 Shift＝一次 45°）。"
        )

    # ------------------------------
    # 角度
    # ------------------------------
    def angle(self) -> float:
        return self._angle

    def set_angle(self, value: float):
        """外部设定角度（加载步骤、旁边那个角度框改了都用它）。"""
        a = round(float(value or 0) % 360, 1)
        if abs(a - self._angle) < 1e-6:
            return
        self._angle = a
        self.update()
        self.angleChanged.emit(a)

    def _from_point(self, x: float, y: float):
        dx, dy = x - CX, y - CY
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        self.set_angle(math.degrees(math.atan2(dy, dx)))

    # ------------------------------
    # 鼠标 / 键盘
    # ------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            pos = event.position()
            self._from_point(pos.x(), pos.y())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            pos = event.position()
            self._from_point(pos.x(), pos.y())

    def wheelEvent(self, event):
        """滚轮＝一度度微调（不占别的控件的事）。"""
        step = 1.0 if event.angleDelta().y() > 0 else -1.0
        self.set_angle(self._angle + step)

    def keyPressEvent(self, event):
        """方向键＝朝那个方向一次挪 1°；按住 Shift＝直接跳到那个正方向。"""
        targets = {Qt.Key.Key_Right: 0.0, Qt.Key.Key_Down: 90.0,
                   Qt.Key.Key_Left: 180.0, Qt.Key.Key_Up: 270.0}
        if event.key() in targets:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.set_angle(targets[event.key()])
            else:
                self.set_angle(self._angle + self._step_toward(event.key(), 1.0))
            return
        super().keyPressEvent(event)

    def _step_toward(self, key, step: float) -> float:
        """方向键微调：按了就往那个方向转一点点（最短路径）。"""
        target = {Qt.Key.Key_Right: 0.0, Qt.Key.Key_Down: 90.0,
                  Qt.Key.Key_Left: 180.0, Qt.Key.Key_Up: 270.0}[key]
        delta = (target - self._angle + 180) % 360 - 180
        if abs(delta) <= step:
            return delta
        return step if delta > 0 else -step

    # ------------------------------
    # 画
    # ------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(CX, CY)
        # 底盘
        p.setPen(QPen(QColor("#cbd5e1"), 1.2))
        p.setBrush(QColor("#f8fafc"))
        p.drawEllipse(center, R, R)
        # 八个方向的刻度
        p.setPen(QPen(QColor("#e2e8f0"), 1.0))
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(CX + math.cos(a) * (R - 14), CY + math.sin(a) * (R - 14)),
                       QPointF(CX + math.cos(a) * (R - 4), CY + math.sin(a) * (R - 4)))
        # 四个正方向的字（右 / 下 / 左 / 上）
        p.setFont(QFont("Microsoft YaHei", 7))
        p.setPen(QColor("#94a3b8"))
        for text, (ax, ay) in (("右", (R + 8, 0)), ("下", (0, R + 3)),
                               ("左", (-R - 8, 0)), ("上", (0, -R - 3))):
            p.drawText(QRectF(CX + ax - 9, CY + ay - 8, 18, 16),
                       Qt.AlignmentFlag.AlignCenter, text)
        # 正中的小十字
        p.setPen(QPen(QColor("#e2e8f0"), 1.0))
        p.drawLine(QPointF(CX - R, CY), QPointF(CX + R, CY))
        p.drawLine(QPointF(CX, CY - R), QPointF(CX, CY + R))
        # 方向箭头
        rad = math.radians(self._angle)
        tip = QPointF(CX + math.cos(rad) * (R - 8), CY + math.sin(rad) * (R - 8))
        p.setPen(QPen(QColor("#0ea5e9"), 2.6))
        p.drawLine(center, tip)
        head = 9.0
        left = QPointF(tip.x() - math.cos(rad - 0.42) * head,
                       tip.y() - math.sin(rad - 0.42) * head)
        right = QPointF(tip.x() - math.cos(rad + 0.42) * head,
                        tip.y() - math.sin(rad + 0.42) * head)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0ea5e9"))
        p.drawPolygon(QPolygonF([tip, left, right]))
        p.setBrush(QColor("#0ea5e9"))
        p.drawEllipse(center, 3.0, 3.0)
        # 盘下的字：右下 45°
        p.setPen(QColor("#0f172a"))
        p.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, CY + R + 8, W, 20),
                   Qt.AlignmentFlag.AlignCenter, angle_text(self._angle))