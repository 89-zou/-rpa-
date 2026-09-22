# -*- coding: utf-8 -*-
"""元素捕获的「收尾」工具：把抓到的结果存进项目、或把用不上的截图删掉。

真正的捕获流程（藏窗口、控制器小窗、打断/恢复/校验、浏览器会话）在
`picker_controller.py` 里，这里只放捕获**之后**要干的事：
- `save_captured_locator`：把 XPath 存进项目「元素定位」，之后写 {{名字}} 就能复用
- `drop_capture_image`：只要 XPath、不要元素截图的地方，把那张图删掉
- `guess_locator_name`：给「元素定位」起个像样的默认名字
"""
import re
from pathlib import Path

from PyQt6.QtWidgets import QInputDialog

from smart_tool.core.project_store import ProjectStore


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
