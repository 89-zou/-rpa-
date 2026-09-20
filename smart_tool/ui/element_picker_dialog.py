# -*- coding: utf-8 -*-
"""元素捕获窗口：点一下拿到「XPath + 元素截图」。

界面上只有一个地址栏和状态提示，真正的操作在浏览器里：
鼠标划过页面元素会画橙框、并显示这个选择器命中几个；点一下就捕获。

浏览器跑在独立线程（Playwright 同步 API 不能和 Qt 事件循环挤在一个线程），
捕获结果通过信号回主线程。捕获完自动重新装填选择器，可以连着抓多个。
"""
import queue
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from smart_tool.core.element_picker import PICKER_JS, next_shot_path
from smart_tool.core.project_store import ProjectStore
from smart_tool.ui.help_tip import help_row

NAV_TIMEOUT_MS = 120_000
POLL_MS = 200

#: 【?】里的完整说明（界面上只留一句摘要）
PICKER_HELP = (
    "点【开始捕获】会打开一个浏览器窗口（用的是上面的网址）：\n"
    "\n"
    "· 鼠标划过元素 → 画橙框，并在旁边显示「这个选择器命中几个」；\n"
    "  命中好几个说明这个写法不够准，最好换一个元素或换种写法。\n"
    "· 点一下 → 抓下它的 XPath，同时把这个元素的截图存进项目 img/；\n"
    "  这张图后面可以当「兜底截图」用（XPath 失效时靠它找位置）。\n"
    "· 抓到没有，看页面就知道：点中的元素会被「绿框」圈住，\n"
    "  屏幕顶端还会弹一条绿色提示「✓ 已捕获第 N 个」。\n"
    "\n"
    "【可以连着抓】抓完会自动重新装填，接着点下一个就行，抓到满意为止点【完成】。\n"
    "· 按 Esc 只是收起橙框（方便你看清页面），不会退出；\n"
    "· 抓完最后留下的只有你选中那一次的元素截图，中间试的那些会自动删掉，\n"
    "  不会在 img/ 里堆废图。\n"
    "\n"
    "【抓不到的情况】如果元素在 iframe 里，XPath 能拿到但截不到图——\n"
    "这时可以自己裁剪一张图放进项目 img/ 当兜底。"
)


class PickerWorker(QThread):
    """后台线程：开浏览器 → 注入选择器 → 等人点 → 回传结果。"""

    ready = pyqtSignal(str)        # 页面已打开（当前 URL）
    picked = pyqtSignal(dict)      # 捕获结果
    failed = pyqtSignal(str)       # 出错 / 浏览器被关掉
    log = pyqtSignal(str)

    def __init__(self, url: str, img_dir: Path, parent=None):
        super().__init__(parent)
        self._url = url
        self._img_dir = Path(img_dir)
        self._stop = False
        self._picks: "queue.Queue[dict]" = queue.Queue()
        self._page = None
        self._seq = 0

    # ------------------------------
    # 主线程调用
    # ------------------------------
    def stop(self):
        """请求收工（会关掉浏览器）。"""
        self._stop = True

    # ------------------------------
    # 线程内
    # ------------------------------
    def _on_pick(self, payload):
        """页面里点中元素时由 Playwright 回调（运行在本线程）。"""
        self._picks.put(dict(payload or {}))

    def _save_shot(self, payload: dict) -> str:
        """把刚捕获的元素截下来，返回相对项目的路径（img/xxx.png）。

        用 locator.screenshot()：它裁的就是元素精确边框，不受滚动/缩放影响。
        iframe 里的元素（page 找不到这个 XPath）跳过截图，交给上层提示。
        """
        xpath = (payload.get("xpath") or "").strip()
        if not xpath or not payload.get("top"):
            return ""
        try:
            loc = self._page.locator(f"xpath={xpath}")
            if loc.count() < 1:
                return ""
            self._img_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = next_shot_path(self._img_dir, f"{stamp}_{self._seq}")
            loc.first.screenshot(path=str(path))
            return f"img/{path.name}"
        except Exception as e:
            self.log.emit(f"元素截图失败（XPath 仍然可用）：{str(e).splitlines()[0][:100]}")
            return ""

    def _drain(self):
        """把这一轮捕获结果发出去，并重新装填选择器（方便连着抓）。"""
        while True:
            try:
                payload = self._picks.get_nowait()
            except queue.Empty:
                return
            self._seq += 1
            payload["image"] = self._save_shot(payload)
            self.picked.emit(payload)
            try:
                self._page.evaluate(PICKER_JS)      # 重新装填，继续抓下一个
            except Exception:
                return

    def run(self):
        from playwright.sync_api import sync_playwright

        from smart_tool.core import browser_setup

        browser_setup.ensure_env()        # 内核可能在「程序目录旁的浏览器文件夹」里
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()
                self._page = page
                page.expose_function("__trae_pick", self._on_pick)
                page.add_init_script(PICKER_JS)
                try:
                    page.goto(self._url, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                except Exception as e:
                    self.failed.emit(f"打开网址失败：{str(e).splitlines()[0][:150]}")
                    browser.close()
                    return
                self.ready.emit(page.url or self._url)

                while not self._stop:
                    if page.is_closed():
                        self.failed.emit("浏览器窗口被关掉了，捕获结束。")
                        break
                    try:
                        page.wait_for_timeout(POLL_MS)
                    except Exception as e:
                        if self._stop:
                            break
                        self.failed.emit(
                            f"页面已关闭，捕获结束（{str(e).splitlines()[0][:80]}）"
                        )
                        break
                    self._drain()
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.failed.emit(f"启动浏览器失败：{str(e).splitlines()[0][:150]}")


class ElementPickerDialog(QDialog):
    """捕获窗口。accept 后用 result_data 取结果。"""

    def __init__(self, url: str, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("元素捕获")
        self.setMinimumWidth(640)
        self.project_dir = Path(project_dir)
        self.img_dir = self.project_dir / "img"
        self._worker: Optional[PickerWorker] = None
        self._hits: list = []
        self._shots: list = []          # 这次捕获生成的截图（收尾时清掉没用的）
        self.result_data: Optional[dict] = None
        self._init_ui(url)

    # ------------------------------
    # UI
    # ------------------------------
    def _init_ui(self, url: str):
        root = QVBoxLayout(self)

        root.addWidget(help_row(
            "点【开始捕获】，在浏览器里点元素（抓到会弹绿框提示）。",
            "元素捕获", PICKER_HELP))

        form = QFormLayout()
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://example.com/login")
        form.addRow("页面地址：", self.url_edit)
        root.addLayout(form)

        row = QHBoxLayout()
        self.btn_start = QPushButton("开始捕获")
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)
        self.btn_stop = QPushButton("关闭浏览器")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_worker)
        row.addWidget(self.btn_stop)
        self.status = QLabel("还没开始。")
        self.status.setStyleSheet("color: #888;")
        row.addWidget(self.status, 1)
        root.addLayout(row)

        self.result_label = QLabel("捕获结果会显示在这里。")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_label.setStyleSheet("color: #0f766e;")
        root.addWidget(self.result_label)

        self.preview = QLabel("（还没抓到元素）")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(120)
        self.preview.setStyleSheet(
            "border: 1px dashed #bbb; border-radius: 4px; color: #999;"
        )
        root.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("完成")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------
    # 起 / 停
    # ------------------------------
    def _start(self):
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先填页面地址。")
            return
        if self._worker is not None:
            return
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.url_edit.setEnabled(False)
        self.status.setText("正在打开浏览器…")
        self._hits = []
        self.result_data = None
        self._worker = PickerWorker(url, self.img_dir, self)
        self._worker.ready.connect(self._on_ready)
        self._worker.picked.connect(self._on_picked)
        self._worker.failed.connect(self._on_failed)
        self._worker.log.connect(self.status.setText)
        self._worker.start()

    def _stop_worker(self):
        """关掉浏览器（捕获结束，但已经抓到的结果留着）。"""
        if self._worker is not None:
            self._worker.stop()
        self.btn_stop.setEnabled(False)
        self.status.setText("已请求关闭浏览器，已抓到的结果可以点【完成】使用。")

    def _on_ready(self, url: str):
        self.status.setText(f"页面已打开，请在浏览器里点一下目标元素：{url}")

    def _on_failed(self, message: str):
        self.status.setText(message)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.url_edit.setEnabled(True)
        self._worker = None

    def _on_picked(self, payload: dict):
        self._hits.append(payload)
        xpath = payload.get("xpath") or "（生成失败）"
        count = payload.get("count", -1)
        lines = [
            f"✓ 已捕获第 {len(self._hits)} 个",
            f"元素：{payload.get('desc', '')}",
            f"XPath：{xpath}",
            f"命中：{count} 个" + ("" if count == 1 else "（不唯一，建议换更稳的写法）"),
            f"来源：{payload.get('why', '')}"
            + ("" if payload.get("top") else "（在 iframe 内，XPath 需配合框架使用）"),
        ]
        image = payload.get("image") or ""
        lines.append(f"截图：{image}" if image else "截图：没抓到（可用【选择截图…】手工裁剪）")
        self.result_label.setText("\n".join(lines))
        if image:
            self._shots.append(self.project_dir / image)
            self._show_preview(self.project_dir / image)
        self.result_data = {
            "xpath": xpath if count >= 0 else "",
            "image": image,
            "count": count,
            "desc": payload.get("desc", ""),
        }
        self.status.setText(f"已捕获 {len(self._hits)} 个元素，可以继续点，或点【完成】使用。")

    def _show_preview(self, path: Path):
        pix = QPixmap(str(path))
        if pix.isNull():
            self.preview.setText("（截图无法预览）")
            return
        self.preview.setPixmap(
            pix.scaled(self.preview.width() or 300, 110,
                       Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
        )

    # ------------------------------
    # 收尾
    # ------------------------------
    def _on_accept(self):
        if self.result_data is None:
            QMessageBox.information(
                self, "还没抓到",
                "还没有捕获到元素。\n先点【开始捕获】，在浏览器里点一下目标元素。",
            )
            return
        self.accept()

    def accept(self):
        """完成：留下的只有最后选中的那张截图。"""
        image = (self.result_data or {}).get("image") or ""
        self._shutdown(keep=(self.project_dir / image) if image else None)
        super().accept()

    def reject(self):
        """取消（含点右上角 ×、按 Esc）：关掉浏览器并清掉这次的截图。"""
        self._shutdown(keep=None)
        super().reject()

    def _shutdown(self, keep: Optional[Path]):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(4000)
            self._worker = None
        self._drop_unused_shots(keep)

    def _drop_unused_shots(self, keep: Optional[Path]):
        """删掉这次捕获生成、但最终没用上的截图。

        抓一个点一个很容易试好几次；只有最后选中的那张会被写进步骤，
        其余的留着只会在 img/ 里堆废图。
        """
        for p in self._shots:
            if keep is not None and p == keep:
                continue
            try:
                p.unlink()
            except OSError:
                pass
        self._shots.clear()


def pick_element(parent, url: str, project_dir) -> Optional[dict]:
    """开捕获窗口让用户点一个元素，返回它的信息（取消返回 None）。

    返回值就是 ElementPickerDialog.result_data：
    `{"xpath":…, "image":"img/xxx.png", "count":命中几个, "desc":元素描述}`。
    给「不想要元素截图、只要一个 XPath」的地方用（比如登录态体检、采集行定位）。
    """
    dlg = ElementPickerDialog(url, Path(project_dir), parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
        return None
    return dlg.result_data


def drop_capture_image(project_dir, data: dict) -> None:
    """「只要 XPath、不要元素截图」的地方收尾用：把那张图删掉。

    捕获器只会留下最后一张截图（其余都自己清了），但很多地方（登录态体检、
    采集行定位）根本用不上它，留着只会在项目 img/ 里堆废图。
    """
    rel = (data or {}).get("image") or ""
    if not rel:
        return
    try:
        (Path(project_dir) / rel).unlink()
    except OSError:
        pass


def guess_locator_name(data: dict, taken=None) -> str:
    """从捕获结果的元素描述里猜一个名字（给它个像样的默认值，不用现想）。

    desc 长这样：`<a>#menu-posts “文章”`、`<input>#user_login`、`<div.item> “书”`。
    优先用元素上的文字，其次用 id；重名就往后加 2、3…
    """
    desc = str((data or {}).get("desc") or "")
    m = re.search(r"[“\"](.+?)[”\"]", desc)
    base = re.sub(r"\s+", "", m.group(1)) if m else ""
    if not base:
        m = re.search(r"#([A-Za-z0-9_-]+)", desc)
        base = m.group(1) if m else ""
    base = base[:12] or "元素"
    name, i = base, 2
    while taken and name in taken:
        name = f"{base}{i}"
        i += 1
    return name


def save_captured_locator(parent, project_dir, data: dict) -> str:
    """把这次捕获到的 XPath 存进项目的「元素定位」，返回变量名（没存返回 ""）。

    - 同一个 XPath 已经存过 → 不再重复问，直接复用原来那个名字
    - 名字留空或取消 → 不存（只填在当前这个字段里）
    """
    xpath = (data or {}).get("xpath") or ""
    xpath = xpath.strip()
    if not xpath:
        return ""
    store = ProjectStore(Path(project_dir))
    locators = store.load_locators()
    for name, value in locators.items():
        if value.strip() == xpath:
            return name
    name, ok = QInputDialog.getText(
        parent, "存成「元素定位」",
        "要不要把这次抓到的元素存下来？\n"
        "存了以后，任何「定位路径」里写 {{名字}} 就能复用它，"
        "改一处全项目都跟着变。\n"
        "（留空 = 不存，只填在当前这个字段里）\n\n"
        f"XPath：{xpath[:150]}",
        text=guess_locator_name(data, locators),
    )
    name = (name or "").strip()
    if not ok or not name:
        return ""
    locators[name] = xpath
    store.save_locators(locators)
    return name
