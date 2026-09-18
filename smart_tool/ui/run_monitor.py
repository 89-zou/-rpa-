# -*- coding: utf-8 -*-
"""运行小窗：跑流程时把主页收起来，只在屏幕右下角留这一块。

为什么要有它：跑自动化的时候主页挡着目标程序（桌面场景还会被截进图里），
干脆收起来；但收起来就看不见进度、也按不到停止。所以留一个角落小窗，
显示「跑到第几步 / 正在做什么 / 最近几条日志」，并带三个按钮：
暂停（继续）· 终止 · 显示主页。

收回主页只有两种情况：流程结束（或出错退出）、用户点【显示主页】。
"""
from typing import List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

# 小窗尺寸与离屏幕边缘的距离
WIDTH = 360
MARGIN = 18
# 日志区最多留几行、每行截多少字（多了小窗会被撑高）
LOG_LINES = 3
LOG_CHARS = 46

STYLE = """
#card { background: #1f2937; border: 1px solid #3f4b5b; border-radius: 10px; }
#title { color: #e5e7eb; font-size: 13px; font-weight: bold; }
#step { color: #fbbf24; font-size: 12px; font-weight: bold; }
#detail { color: #9ca3af; font-size: 11px; }
#log { color: #9ca3af; font-size: 11px; }
#rule { background: #374151; }
QPushButton {
    background: #374151; color: #e5e7eb; border: none;
    border-radius: 6px; padding: 6px 12px; font-size: 12px;
}
QPushButton:hover { background: #4b5563; }
QPushButton:disabled { background: #2b3442; color: #6b7280; }
QPushButton#stop { background: #7f1d1d; }
QPushButton#stop:hover { background: #991b1b; }
QPushButton#home { background: #1d4ed8; }
QPushButton#home:hover { background: #2563eb; }
"""

# 暂停按钮的三种样子
PAUSE_LABEL = {
    "running": "暂停",
    "user": "继续",
    "flow": "继续（人工处理完）",
}
TITLE_PREFIX = {
    "running": "▶ 正在运行",
    "user": "⏸ 已暂停",
    "flow": "⏸ 等人工处理",
    "stopping": "■ 正在停止",
}


class RunMonitor(QWidget):
    """右下角的运行小窗。"""

    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    home_clicked = pyqtSignal()

    def __init__(self):
        super().__init__(None)
        self.setWindowTitle("运行进度")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus   # 别抢走目标程序的焦点
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self._total = 0
        self._project = ""
        self._logs: List[str] = []
        self._init_ui()

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        self.title_label = QLabel("正在运行")
        self.title_label.setObjectName("title")
        root.addWidget(self.title_label)

        self.step_label = QLabel("准备开始…")
        self.step_label.setObjectName("step")
        root.addWidget(self.step_label)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("detail")
        root.addWidget(self.detail_label)

        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        root.addWidget(rule)

        self.log_label = QLabel("")
        self.log_label.setObjectName("log")
        self.log_label.setFixedHeight(LOG_LINES * 16)
        self.log_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root.addWidget(self.log_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_pause = QPushButton(PAUSE_LABEL["running"])
        self.btn_pause.clicked.connect(self.pause_clicked.emit)
        row.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("终止")
        self.btn_stop.setObjectName("stop")
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        row.addWidget(self.btn_stop)
        self.btn_home = QPushButton("显示主页")
        self.btn_home.setObjectName("home")
        self.btn_home.setToolTip("把主界面调回来（流程会在后台继续跑，小窗收起）")
        self.btn_home.clicked.connect(self.home_clicked.emit)
        row.addWidget(self.btn_home)
        root.addLayout(row)

        self.setFixedWidth(WIDTH)

    # ------------------------------
    # 对外：一次运行的生命周期
    # ------------------------------
    def start(self, project_name: str, total_steps: int):
        """开始跑：重置内容、摆到右下角、显示出来。"""
        self._total = int(total_steps)
        self._logs = []
        self._project = project_name or "（未命名项目）"
        self.log_label.setText("")
        self.step_label.setText(f"共 {self._total} 步，准备开始…")
        self.detail_label.setText("")
        self.set_state("running")
        self._move_to_corner()
        self.show()
        self.raise_()

    def set_step(self, index: int, action_cn: str, detail: str = ""):
        """跑到第几步了。"""
        self.step_label.setText(
            f"第 {index} / {self._total} 步　{action_cn}" if self._total
            else f"第 {index} 步　{action_cn}"
        )
        self.detail_label.setText(_clip(detail, 60))

    def add_log(self, line: str):
        """把最新日志喂进来（只留最后几行）。"""
        text = " ".join(str(line).split())
        if not text:
            return
        self._logs.append(_clip(text, LOG_CHARS))
        del self._logs[:-LOG_LINES]
        self.log_label.setText("\n".join(self._logs))

    def set_state(self, state: str, note: str = ""):
        """切换状态：running / user（用户暂停）/ flow（流程暂停节点）/ stopping。"""
        title = f"{TITLE_PREFIX.get(state, state)} · {self._project}"
        if note:
            title += f" · {note}"
        self.title_label.setText(title)
        self.btn_pause.setText(PAUSE_LABEL.get(state, "暂停"))
        self.btn_pause.setEnabled(state in ("running", "user", "flow"))
        self.btn_stop.setEnabled(state != "stopping")

    # ------------------------------
    # 位置
    # ------------------------------
    def _move_to_corner(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.right() - self.width() - MARGIN,
                  area.bottom() - self.height() - MARGIN)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"
