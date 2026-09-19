# -*- coding: utf-8 -*-
"""启动海报（广告页）：程序没加载完之前，不让点「进入程序」。

用法（main.py 里）：

    splash = AdSplash.try_create()          # 没有海报文件 → None
    if splash:
        splash.show(); app.processEvents()      # 先把海报画出来
    window = MainWindow()                       # 这一步最慢
    if splash:
        splash.set_ready()                      # 加载完 → 按钮可用 + 开始 8 秒倒计时
        splash.exec()                           # 等用户点【进入程序】或倒计时结束
    window.show()

海报文件放 `assets/求打赏.jpg`（换成 png 也行）。
加载完成后再等 8 秒自动进入——用户不想等就自己点【进入程序】。
"""
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QVBoxLayout,
)

from smart_tool import paths

#: 海报最大显示尺寸（等比缩放，不拉伸）
MAX_POSTER_W, MAX_POSTER_H = 560, 430
#: 加载完成后等几秒自动进入（用户点按钮就不用等）
AUTO_ENTER_SECONDS = 8


class AdSplash(QDialog):
    """启动海报：上面是图，下面是进度 + 【进入程序】。"""

    def __init__(self, poster, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{paths.APP_NAME} ｜ 作者：{paths.AUTHOR}")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 海报 ----
        pix = QPixmap(str(poster))
        if pix.width() > MAX_POSTER_W or pix.height() > MAX_POSTER_H:
            pix = pix.scaled(MAX_POSTER_W, MAX_POSTER_H,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self.poster_label = QLabel()
        self.poster_label.setPixmap(pix)
        self.poster_label.setFixedSize(pix.size())
        root.addWidget(self.poster_label)

        # ---- 底部状态区 ----
        bar = QFrame()
        bar.setObjectName("splashBar")
        bar.setStyleSheet(
            "#splashBar { background: #1f2430; border-bottom-left-radius: 8px;"
            " border-bottom-right-radius: 8px; }")
        bottom = QVBoxLayout(bar)
        bottom.setContentsMargins(16, 12, 16, 12)
        bottom.setSpacing(8)

        self.status_label = QLabel("正在加载…")
        self.status_label.setStyleSheet("color: #e8eaf0; font-size: 12px;")
        bottom.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setStyleSheet(
            "QProgressBar { background: #3a4050; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #4f8cff; border-radius: 3px; }")
        bottom.addWidget(self.progress)

        row = QHBoxLayout()
        self.hint = QLabel("程序正在加载，完成后按钮才可点")
        self.hint.setStyleSheet("color: #9aa4b2; font-size: 11px;")
        row.addWidget(self.hint, 1)
        self.enter_btn = QPushButton("进入程序")
        self.enter_btn.setEnabled(False)          # 加载完才启用
        self.enter_btn.setMinimumWidth(120)
        self.enter_btn.setDefault(True)
        self.enter_btn.setStyleSheet(
            "QPushButton { background:#4f8cff; color:white; border:none;"
            " border-radius:4px; padding:7px 16px; font-size:13px; }"
            "QPushButton:disabled { background:#3a4050; color:#8b93a3; }"
            "QPushButton:hover:enabled { background:#3d7bef; }")
        self.enter_btn.clicked.connect(self.accept)
        row.addWidget(self.enter_btn)
        bottom.addLayout(row)
        root.addWidget(bar)

        self.setFixedSize(pix.width(), pix.height() + bar.sizeHint().height())
        icon = paths.icon_file()
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self._drag_from = None
        # 加载完成后自动进入的倒计时（用户点按钮就提前进）
        self._left = AUTO_ENTER_SECONDS
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ------------------------------
    # 对外：状态 / 就绪
    # ------------------------------
    def set_status(self, text: str, percent: int = -1):
        """更新底部那句话（和进度条百分比，-1＝不动）。"""
        self.status_label.setText(text)
        if percent >= 0:
            self.progress.setValue(max(0, min(100, int(percent))))

    def set_ready(self):
        """加载完成：启用【进入程序】、聚焦它，并开始 8 秒倒计时。"""
        self.progress.setValue(100)
        self.status_label.setText("加载完成 ✓")
        self.enter_btn.setEnabled(True)
        self.enter_btn.setFocus()
        self._left = AUTO_ENTER_SECONDS
        self._update_hint()
        self._timer.start()

    def _update_hint(self):
        self.hint.setText(
            f"{self._left} 秒后自动进入（也可以直接点【进入程序】）")

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self.accept()               # 时间到＝自己进
            return
        self._update_hint()

    # ------------------------------
    # 无边框窗口：按住图能拖动
    # ------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - \
                self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None and \
                event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_from = None
        super().mouseReleaseEvent(event)

    # ------------------------------
    @classmethod
    def try_create(cls, parent=None):
        """海报文件在就建这个窗口；没有就返回 None（直接进程序）。

        注意：海报页是**不能关掉**的（启动广告），所以这里不看任何开关。
        """
        try:
            poster = paths.poster_file()
            if not poster.is_file():
                return None
            return cls(poster, parent)
        except Exception:
            return None             # 海报出问题不能挡住程序启动
