# -*- coding: utf-8 -*-
"""截屏取模板：给桌面场景用（网页那边有「元素捕获」，这里只能靠框选）。

流程：打开后倒计时几秒（留时间把目标窗口切到最前面）→ 自动截全屏 →
在图上拖一个框 → 【保存为模板】存进项目 img/。

为什么要倒计时：截屏那一刻本窗口不能挡着目标程序，所以先给你几秒切窗口。
框选原则：**只框控件本身**，别带上大片背景——桌面没有 XPath，全靠这张图匹配，
带进背景就会「哪儿都像」而匹配错地方。
"""
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from smart_tool.core import desktop

# 打开后几秒自动截屏
COUNTDOWN_S = 3
# 框选最小尺寸（像素）——太小了匹配不准
MIN_SIZE = 6


def pil_to_pixmap(img) -> QPixmap:
    """PIL 图 → QPixmap（走 RGB 原始字节，别用临时文件）。"""
    rgb = img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3,
                  QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


class _ShotView(QWidget):
    """显示截图，支持拖框选择；对外给的是**原图像素**坐标。"""

    picked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 280)
        self.setStyleSheet("background:#2b2b2b;")
        self._pix: Optional[QPixmap] = None
        self._rect = QRect()          # 控件坐标下的选框
        self._origin = QPoint()
        self._dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_pixmap(self, pix: QPixmap):
        self._pix = pix
        self._rect = QRect()
        self.update()

    # ---- 坐标换算：控件 ↔ 原图 ----
    def _scale(self) -> float:
        if not self._pix or self._pix.isNull():
            return 1.0
        return min(self.width() / self._pix.width(),
                   self.height() / self._pix.height(), 1.0)

    def _offset(self) -> QPoint:
        if not self._pix or self._pix.isNull():
            return QPoint(0, 0)
        s = self._scale()
        return QPoint(int((self.width() - self._pix.width() * s) / 2),
                      int((self.height() - self._pix.height() * s) / 2))

    def selection(self) -> Optional[Tuple[int, int, int, int]]:
        """选框对应的原图区域 (x, y, w, h)；没框或太小返回 None。"""
        if self._rect.isNull() or not self._pix:
            return None
        s = self._scale()
        off = self._offset()
        x = int((self._rect.left() - off.x()) / s)
        y = int((self._rect.top() - off.y()) / s)
        w = int(self._rect.width() / s)
        h = int(self._rect.height() / s)
        x = max(0, min(x, self._pix.width() - 1))
        y = max(0, min(y, self._pix.height() - 1))
        w = max(1, min(w, self._pix.width() - x))
        h = max(1, min(h, self._pix.height() - y))
        if w < MIN_SIZE or h < MIN_SIZE:
            return None
        return x, y, w, h

    # ---- 绘制 ----
    def paintEvent(self, _event):
        # 画东西出错只丢一帧；异常逃进 Qt 的事件分发会直接终止进程
        try:
            self._paint()
        except Exception:
            pass

    def _paint(self):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#2b2b2b"))
        if not self._pix or self._pix.isNull():
            p.setPen(QColor("#dddddd"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "还没有截图")
            return
        s = self._scale()
        off = self._offset()
        p.drawPixmap(QRect(off, self._pix.size() * s), self._pix)
        if self._rect.isNull():
            return
        # 选区外压暗（画四条边带），方便一眼看清框住了什么
        dim = QColor(0, 0, 0, 110)
        r = self._rect
        full = self.rect()
        p.fillRect(QRect(full.left(), full.top(), full.width(),
                         r.top() - full.top()), dim)
        p.fillRect(QRect(full.left(), r.bottom() + 1, full.width(),
                         full.bottom() - r.bottom()), dim)
        p.fillRect(QRect(full.left(), r.top(), r.left() - full.left(),
                         r.height()), dim)
        p.fillRect(QRect(r.right() + 1, r.top(),
                         full.right() - r.right(), r.height()), dim)
        p.setPen(QPen(QColor("#f08a24"), 2))
        p.drawRect(r)

    # ---- 拖框 ----
    def mousePressEvent(self, event):
        if not self._pix or event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._rect = QRect(self._origin, self._origin)
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        cur = event.position().toPoint()
        self._rect = QRect(self._origin, cur).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        # 框选限制在图片范围内
        off = self._offset()
        img_rect = QRect(off, self._pix.size() * self._scale())
        self._rect = self._rect.intersected(img_rect)
        self.update()
        self.picked.emit()


class ScreenCaptureDialog(QDialog):
    """截屏取模板。accept 后用 result_path 取「img/xxx.png」。"""

    def __init__(self, project_dir: Path, parent=None,
                 countdown_s: int = COUNTDOWN_S):
        super().__init__(parent)
        self.setWindowTitle("截屏取模板")
        self.setMinimumSize(760, 600)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._img = None                    # PIL 原图（裁剪用）
        self._left = countdown_s
        self.result_path: str = ""
        self._init_ui()
        self._start_countdown()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #b45309; font-weight: bold;")
        root.addWidget(self.status)

        tip = QLabel(
            "用法：在图上拖一个框圈住控件（只框控件本身，别带大片背景），"
            "右边确认裁剪结果后点【保存为模板】。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666;")
        root.addWidget(tip)

        body = QHBoxLayout()
        self.view = _ShotView()
        self.view.picked.connect(self._on_picked)
        body.addWidget(self.view, 3)

        side = QVBoxLayout()
        side.addWidget(QLabel("裁剪结果："))
        self.preview = QLabel("（还没框选）")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(150)
        self.preview.setStyleSheet(
            "border:1px dashed #bbb; border-radius:4px; color:#999;"
        )
        side.addWidget(self.preview, 1)
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("color:#666;")
        side.addWidget(self.size_label)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("文件名："))
        self.name_edit = QLineEdit(self._default_name())
        name_row.addWidget(self.name_edit, 1)
        side.addLayout(name_row)
        self.name_hint = QLabel("存到项目 img/ 目录，扩展名固定 .png")
        self.name_hint.setStyleSheet("color:#888;")
        side.addWidget(self.name_hint)
        body.addLayout(side, 1)
        root.addLayout(body, 1)

        btns = QHBoxLayout()
        self.btn_shot = QPushButton("重新截屏")
        self.btn_shot.setToolTip("再倒计时截一次（先把目标窗口切到最前面）")
        self.btn_shot.clicked.connect(self._start_countdown)
        btns.addWidget(self.btn_shot)
        self.btn_full = QPushButton("看整屏")
        self.btn_full.setToolTip("把选框清掉，重新看整张截图")
        self.btn_full.clicked.connect(self._reset_view)
        btns.addWidget(self.btn_full)
        btns.addStretch()
        root.addLayout(btns)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("保存为模板")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    @staticmethod
    def _default_name() -> str:
        return "cap_" + datetime.now().strftime("%Y%m%d_%H%M%S")

    # ------------------------------
    # 截屏
    # ------------------------------
    def _start_countdown(self):
        try:
            if not desktop.available():
                QMessageBox.critical(self, "缺少依赖", desktop.missing_hint())
                self.reject()
                return
            self._left = COUNTDOWN_S
            self._tick()
        except Exception as e:
            QMessageBox.critical(self, "截屏失败", str(e))

    def _tick(self):
        """倒计时槽：整段包住，别让异常逃进 Qt 的事件分发。"""
        try:
            if self._left > 0:
                self.status.setText(
                    f"{self._left} 秒后自动截屏——请趁现在把目标窗口切到最前面"
                    "（本窗口别挡着它）"
                )
                self._left -= 1
                QTimer.singleShot(1000, self._tick)
                return
            self.status.setText("正在截屏…")
            QTimer.singleShot(50, self._grab)
        except Exception as e:
            self.status.setText(f"出错：{e}")

    def _grab(self):
        try:
            self._img = desktop.grab_screen()
            self.view.set_pixmap(pil_to_pixmap(self._img))
        except Exception as e:
            self.status.setText("截屏失败")
            QMessageBox.critical(self, "截屏失败", str(e))
            return
        self._reset_view()
        self.status.setText(
            f"截图完成（{self._img.width} × {self._img.height}）："
            "在图上拖框圈住目标控件。想重截就点【重新截屏】。"
        )

    def _reset_view(self):
        self.preview.setPixmap(QPixmap())
        self.preview.setText("（还没框选）")
        self.size_label.setText("")

    def _on_picked(self):
        """框选完成（鼠标松开）——槽函数，整段包住别让异常逃出去。"""
        try:
            if self._img is None:
                self.size_label.setText("还没截屏，先点【重新截屏】")
                return
            sel = self.view.selection()
            if sel is None:
                self.size_label.setText(
                    f"框太小了（至少 {MIN_SIZE}×{MIN_SIZE} 像素）")
                self.preview.setPixmap(QPixmap())
                return
            x, y, w, h = sel
            crop = self._img.crop((x, y, x + w, y + h))
            self.preview.setPixmap(pil_to_pixmap(crop).scaled(
                150, 90, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self.size_label.setText(f"已选 {w} × {h} 像素")
            if not self.name_edit.text().strip():
                self.name_edit.setText(self._default_name())
        except Exception as e:
            self.size_label.setText(f"出错了：{e}")

    # ------------------------------
    # 保存
    # ------------------------------
    def _on_accept(self):
        sel = self.view.selection()
        if self._img is None:
            QMessageBox.information(self, "还没截屏", "先点【重新截屏】截一张。")
            return
        if sel is None:
            QMessageBox.information(
                self, "还没框选",
                f"请在图上拖一个框圈住目标控件（至少 {MIN_SIZE}×{MIN_SIZE} 像素）。",
            )
            return
        stem = self.name_edit.text().strip() or self._default_name()
        for ch in '\\/:*?"<>|':
            stem = stem.replace(ch, "_")
        if stem.lower().endswith(".png"):
            stem = stem[:-4]
        self.img_dir.mkdir(parents=True, exist_ok=True)
        path = self.img_dir / f"{stem}.png"
        i = 1
        while path.exists():
            path = self.img_dir / f"{stem}_{i}.png"
            i += 1
        x, y, w, h = sel
        try:
            self._img.crop((x, y, x + w, y + h)).save(str(path))
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"模板图存不进去：\n{e}")
            return
        self.result_path = f"img/{path.name}"
        self.accept()
