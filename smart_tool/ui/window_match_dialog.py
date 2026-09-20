# -*- coding: utf-8 -*-
"""定位匹配（桌面场景的捕获）：先选窗口 → **只截这个窗口** → 在窗口图上框控件。

跟老的「截屏取模板」比，区别就三点，都是为了让运行时点得准：

1. **不截整屏**：只把选中的那个窗口截下来（PrintWindow，被挡住也能截；
   不行就切到最前按窗口矩形那一块截）。窗口挪到哪儿、是不是最大化了，都不影响捕获。
2. **记窗口名**：结果是「窗口名 + 窗口图 + 控件框在窗口图上的位置」。
   运行时先按窗口名找到窗口（窗口挪位置、改大小都不怕），再按框的位置点。
3. **可选的深度定位**：在窗口图上再框一块**不会变的地方**（标题栏、固定图标），
   运行时先对一下这块特征图——对不上就说明窗口内容跟捕获时差太多，**不点、直接报错**。

产出（写进步骤的 locator）：window（窗口图）、window_size（捕获时窗口长宽）、
offset（控件框）、feature（特征图）；窗口名写进这一步的「窗口标题」。
"""
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from smart_tool.core import desktop

#: 框选最小尺寸（像素）——太小了匹配不准
MIN_SIZE = 6


def pil_to_pixmap(img) -> QPixmap:
    """PIL 图 → QPixmap（走 RGB 原始字节，别用临时文件）。"""
    rgb = img.convert("RGB")
    return QPixmap.fromImage(
        QImage(rgb.tobytes("raw", "RGB"), rgb.width, rgb.height, rgb.width * 3,
               QImage.Format.Format_RGB888).copy())


def guess_window_keyword(title: str) -> str:
    """从窗口标题里猜一个「不会老是变」的关键字（实现见 core.desktop）。"""
    return desktop.guess_window_keyword(title)


class _BoxView(QWidget):
    """显示窗口图，支持框两块：橙色＝控件框，绿色＝特征图（深度定位）。"""

    picked = pyqtSignal(str)          # "main" / "feature"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 340)
        self.setStyleSheet("background:#2b2b2b;")
        self._pix: Optional[QPixmap] = None
        self._boxes: Dict[str, QRect] = {"main": QRect(), "feature": QRect()}
        self.mode = "main"            # 现在拖框画到哪一块上
        self._origin = QPoint()
        self._dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, pix: QPixmap):
        self._pix = pix
        self._boxes = {"main": QRect(), "feature": QRect()}
        self.update()

    def clear_box(self, kind: str):
        self._boxes[kind] = QRect()
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

    def selection(self, kind: str) -> Optional[Tuple[int, int, int, int]]:
        """这一框对应的原图区域 (x, y, w, h)；没框或太小返回 None。"""
        rect = self._boxes.get(kind, QRect())
        if rect.isNull() or not self._pix:
            return None
        s = self._scale()
        off = self._offset()
        x = int((rect.left() - off.x()) / s)
        y = int((rect.top() - off.y()) / s)
        w = int(rect.width() / s)
        h = int(rect.height() / s)
        x = max(0, min(x, self._pix.width() - 1))
        y = max(0, min(y, self._pix.height() - 1))
        w = max(1, min(w, self._pix.width() - x))
        h = max(1, min(h, self._pix.height() - y))
        if w < MIN_SIZE or h < MIN_SIZE:
            return None
        return x, y, w, h

    # ---- 绘制 ----
    def paintEvent(self, _event):
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
                       "先在上面选一个窗口，点【截取这个窗口】")
            return
        s = self._scale()
        off = self._offset()
        p.drawPixmap(QRect(off, self._pix.size() * s), self._pix)
        for kind, color in (("main", "#f08a24"), ("feature", "#12b886")):
            r = self._boxes.get(kind, QRect())
            if r.isNull():
                continue
            p.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine
                          if kind == "feature" else Qt.PenStyle.SolidLine))
            p.drawRect(r)
            p.setPen(QColor(color))
            p.drawText(r.left() + 3, max(12, r.top() - 4),
                       "控件框" if kind == "main" else "特征图（深度定位）")

    # ---- 拖框 ----
    def mousePressEvent(self, event):
        if not self._pix or event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._boxes[self.mode] = QRect(self._origin, self._origin)
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        self._boxes[self.mode] = QRect(self._origin,
                                       event.position().toPoint()).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        off = self._offset()
        img_rect = QRect(off, self._pix.size() * self._scale())
        self._boxes[self.mode] = self._boxes[self.mode].intersected(img_rect)
        self.update()
        self.picked.emit(self.mode)


class WindowMatchDialog(QDialog):
    """定位匹配。accept 后取 window_title / window_path / window_size / offset / feature_path。"""

    def __init__(self, project_dir: Path, parent=None,
                 initial_title: str = ""):
        super().__init__(parent)
        self.setWindowTitle("定位匹配（桌面场景）")
        self.setMinimumSize(980, 640)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._img = None                       # 窗口图（PIL）
        self._whole_title = ""                 # 截取时那个窗口的完整标题
        self.result_path = ""                  # 控件图 img/xxx.png
        self.window_path = ""                  # 窗口图 img/xxx_窗口.png
        self.feature_path = ""                 # 特征图 img/xxx_特征.png（没开深度定位就是空）
        self.window_size: list = []
        self.offset: list = []
        self.window_title = ""                 # 写进步骤的窗口标题关键字
        self._init_ui(initial_title)

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self, initial_title: str):
        root = QVBoxLayout(self)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #b45309; font-weight: bold;")
        root.addWidget(self.status)

        pick = QHBoxLayout()
        pick.addWidget(QLabel("目标窗口："))
        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(360)
        pick.addWidget(self.window_combo, 1)
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.clicked.connect(self._refresh_windows)
        pick.addWidget(self.btn_refresh)
        self.btn_capture = QPushButton("截取这个窗口")
        self.btn_capture.clicked.connect(self._capture)
        pick.addWidget(self.btn_capture)
        root.addLayout(pick)

        kw = QHBoxLayout()
        kw.addWidget(QLabel("窗口标题（用于运行时找窗口）："))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("留一小段不会变的，如「记事本」「Google Chrome」")
        kw.addWidget(self.keyword_edit, 1)
        root.addLayout(kw)

        body = QHBoxLayout()
        self.view = _BoxView()
        self.view.picked.connect(self._on_picked)
        body.addWidget(self.view, 3)

        side = QVBoxLayout()
        self.deep_box = QCheckBox("深度定位（再框一块「不会变的地方」）")
        self.deep_box.setToolTip(
            "勾上以后：在窗口图上再框一块几乎不会变的区域（标题栏、固定图标、导航条）。\n"
            "运行时先对一下这块特征图 —— 对不上就说明窗口内容跟捕获时差太多，\n"
            "这一步会直接报错停下，不会乱点。")
        self.deep_box.stateChanged.connect(self._on_deep_changed)
        side.addWidget(self.deep_box)

        side.addWidget(QLabel("控件框预览："))
        self.preview = QLabel("（还没框）")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(110)
        self.preview.setStyleSheet(
            "border:1px dashed #bbb; border-radius:4px; color:#999;")
        side.addWidget(self.preview)
        self.main_label = QLabel("")
        self.main_label.setStyleSheet("color:#666;")
        side.addWidget(self.main_label)

        side.addWidget(QLabel("特征图预览（深度定位）："))
        self.feature_preview = QLabel("（没勾深度定位）")
        self.feature_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.feature_preview.setMinimumHeight(90)
        self.feature_preview.setStyleSheet(
            "border:1px dashed #bbb; border-radius:4px; color:#999;")
        side.addWidget(self.feature_preview)
        self.feature_label = QLabel("")
        self.feature_label.setStyleSheet("color:#666;")
        side.addWidget(self.feature_label)

        side.addStretch()
        root.addLayout(body, 1)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("保存这一步的定位")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._refresh_windows(initial_title)

    def _refresh_windows(self, prefer: str = ""):
        """列出当前可见窗口（自己的窗口不算）。"""
        self.window_combo.clear()
        try:
            rows = desktop.list_windows()
        except Exception as e:
            self.status.setText(f"列窗口失败：{e}")
            return
        me = self.windowTitle()
        for title, active in rows:
            if not title or me in title or title == "定位匹配（桌面场景）":
                continue
            self.window_combo.addItem(("★ " if active else "") + title, title)
        if self.window_combo.count() == 0:
            self.status.setText("没列出别的窗口。先把目标程序打开，再点【刷新列表】。")
        else:
            want = prefer or self._whole_title
            if want:
                for i in range(self.window_combo.count()):
                    if want in self.window_combo.itemData(i):
                        self.window_combo.setCurrentIndex(i)
                        break
            self.status.setText("选一个窗口 → 点【截取这个窗口】→ 在图上拖框圈住要点的控件。")

    def _on_deep_changed(self):
        if self.deep_box.isChecked():
            self.view.mode = "feature"
            self.status.setText("深度定位：现在在图上再框一块**不会变的地方**"
                                "（标题栏文字、固定图标这类），框完再点保存。")
        else:
            self.view.mode = "main"
            self.feature_path = ""
            self.view.clear_box("feature")
            self.feature_preview.setPixmap(QPixmap())
            self.feature_preview.setText("（没勾深度定位）")
            self.feature_label.setText("")
            self.status.setText("已关掉深度定位：只在窗口里匹配控件本身，匹配不上就按红框中心点。")

    # ------------------------------
    # 截窗口
    # ------------------------------
    def _capture(self):
        """只截选中的那个窗口（绝不截整屏）。"""
        try:
            title = self.window_combo.currentData()
            if not title:
                QMessageBox.information(self, "先选窗口",
                                        "上面列表里选一个目标窗口再截。")
                return
            self.status.setText(f"正在截取「{title}」…")
            img, rect = desktop.grab_window(title, log=lambda *_: None)
            self._img = img
            self._whole_title = title
            self.view.set_image(pil_to_pixmap(img))
            self.view.mode = "feature" if self.deep_box.isChecked() else "main"
            if not self.keyword_edit.text().strip():
                self.keyword_edit.setText(guess_window_keyword(title))
            self.status.setText(
                f"已截取「{title}」{img.width}×{img.height}。"
                "现在在图上**拖框圈住要点的控件**"
                + ("，然后框一块不会变的特征区域" if self.deep_box.isChecked() else "")
                + "。")
            self._on_picked("main")     # 刷新一下提示（还没框）
        except Exception as e:
            self.status.setText("截取失败")
            QMessageBox.critical(self, "截取窗口失败", str(e))

    # ------------------------------
    # 框选
    # ------------------------------
    def _on_picked(self, kind: str):
        try:
            if self._img is None:
                self.main_label.setText("还没截窗口，先点【截取这个窗口】")
                return
            sel = self.view.selection(kind)
            if sel is None:
                if kind == "main":
                    self.main_label.setText(f"框太小了（至少 {MIN_SIZE}×{MIN_SIZE} 像素）")
                else:
                    self.feature_label.setText(f"框太小了（至少 {MIN_SIZE}×{MIN_SIZE} 像素）")
                return
            x, y, w, h = sel
            crop = self._img.crop((x, y, x + w, y + h))
            label = self.preview if kind == "main" else self.feature_preview
            label.setPixmap(pil_to_pixmap(crop).scaled(
                170, 100, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            text = f"已选 {w}×{h} 像素（图上位置 {x},{y}）"
            if kind == "main":
                self.main_label.setText(text)
                if self.deep_box.isChecked():
                    self.view.mode = "feature"
                    self.status.setText("控件框好了。接着框一块**不会变的地方**当特征图。")
                else:
                    self.status.setText("控件框好了，可以点【保存这一步的定位】。")
            else:
                self.feature_label.setText(text)
                self.status.setText("特征图也框好了，可以点【保存这一步的定位】。")
        except Exception as e:
            self.status.setText(f"出错了：{e}")

    # ------------------------------
    # 保存
    # ------------------------------
    def _on_accept(self):
        if self._img is None:
            QMessageBox.information(self, "还没截窗口",
                                    "先选窗口、点【截取这个窗口】。")
            return
        main = self.view.selection("main")
        if main is None:
            QMessageBox.information(
                self, "还没框控件",
                f"请在窗口图上拖一个框圈住要点的控件（至少 {MIN_SIZE}×{MIN_SIZE} 像素）。")
            return
        feature = None
        if self.deep_box.isChecked():
            feature = self.view.selection("feature")
            if feature is None:
                QMessageBox.information(
                    self, "还没框特征图",
                    "勾了「深度定位」就要再框一块**不会变的地方**"
                    "（标题栏文字、固定图标这类）。\n不想用就取消勾选。")
                return
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(
                self, "窗口标题没填",
                "填一小段**不会变**的窗口标题（如「记事本」），运行时靠它找窗口。")
            return
        stem = "match_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self.img_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 窗口图（整窗）：运行时先认窗口、再在窗口里找控件
            wpath = self.img_dir / f"{stem}_窗口.png"
            self._img.save(str(wpath))
            # 控件图：窗口里再匹配它（比纯按坐标点更抗布局微调）
            x, y, w, h = main
            cpath = self.img_dir / f"{stem}.png"
            self._img.crop((x, y, x + w, y + h)).save(str(cpath))
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"图片存不进去：\n{e}")
            return
        self.window_path = f"img/{wpath.name}"
        self.result_path = f"img/{cpath.name}"
        self.window_size = [float(self._img.width), float(self._img.height)]
        self.offset = [float(x), float(y), float(w), float(h)]
        self.window_title = keyword
        if feature is not None:
            fx, fy, fw, fh = feature
            fpath = self.img_dir / f"{stem}_特征.png"
            try:
                self._img.crop((fx, fy, fx + fw, fy + fh)).save(str(fpath))
                self.feature_path = f"img/{fpath.name}"
            except Exception:
                self.feature_path = ""
        self.accept()
