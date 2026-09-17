# -*- coding: utf-8 -*-
"""截图定位：OpenCV 多尺度模板匹配。

用户自备目标元素的截图（任意截图工具，裁剪出按钮/输入框区域即可），
放入项目 img/ 目录，steps.json 中 locator 写：
    {"type": "image", "value": "img/step_03.png"}

执行时截取当前 viewport，用 cv2.matchTemplate 多尺度匹配找到元素，
换算成 viewport CSS 像素坐标后用鼠标点击。

坐标约定（避免历史偏移问题，全部显式化）：
- 模板/截图内坐标：像素 px
- 对外输出：viewport CSS 像素，与 page.mouse.click 1:1
- 滚动查找时，始终在"当前 viewport"截图匹配，故输出坐标无需加滚动偏移
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import cv2
import numpy as np
from playwright.sync_api import Page

# Windows 常见显示缩放 100/125/150/175/200%，模板可能来自不同缩放的屏幕。
# scale = 模板相对当前页面截图的缩放系数，1.0 最先试。
DEFAULT_SCALES: Tuple[float, ...] = (
    1.0, 1.25, 0.8, 1.5, 0.667, 1.75, 0.571, 0.5, 2.0,
)
DEFAULT_THRESHOLD = 0.8
MAX_SCROLL_STEPS = 15


class ImageNotFoundError(Exception):
    """截图匹配失败。"""


@dataclass
class ImageMatch:
    """匹配结果（viewport CSS 像素，中心点坐标）。"""
    x: float           # 中心 x（CSS px）
    y: float           # 中心 y（CSS px）
    width: float       # 匹配框宽（CSS px）
    height: float      # 匹配框高（CSS px）
    confidence: float  # 相关系数 0~1
    scale: float       # 命中的模板缩放系数
    scroll_y: float    # 命中时页面已滚动距离（信息字段）


# ------------------------------
# 图像 IO（兼容中文路径）
# ------------------------------
def imread_unicode(path: Path) -> np.ndarray:
    """cv2.imread 不支持中文路径，用 fromfile + imdecode 替代。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取截图文件：{path}")
    return img


def _grab_viewport(page: Page) -> Tuple[np.ndarray, int, int]:
    """截取当前 viewport（返回 bytes 解码，不落盘）。

    返回 (BGR 图, 截图宽 px, 截图高 px)。
    """
    png = page.screenshot(type="png")  # 默认 full_page=False，仅当前 viewport
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("页面截图解码失败")
    h, w = img.shape[:2]
    return img, w, h


# ------------------------------
# 模板匹配
# ------------------------------
def _match_one_scale(
    screen_gray: np.ndarray,
    template_gray: np.ndarray,
    scale: float,
) -> Optional[Tuple[float, int, int, int, int]]:
    """单尺度匹配。返回 (置信度, 左上角x, 左上角y, 宽, 高)，模板比画面大时返回 None。"""
    if abs(scale - 1.0) < 1e-6:
        templ = template_gray
    else:
        th0, tw0 = template_gray.shape[:2]
        new_w = max(1, int(round(tw0 * scale)))
        new_h = max(1, int(round(th0 * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        templ = cv2.resize(template_gray, (new_w, new_h), interpolation=interpolation)

    th, tw = templ.shape[:2]
    sh, sw = screen_gray.shape[:2]
    if th > sh or tw > sw:
        return None

    result = cv2.matchTemplate(screen_gray, templ, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    # 注意：maxLoc = (列=x, 行=y)，OpenCV 数组切片才是 [y, x]
    return float(max_val), int(max_loc[0]), int(max_loc[1]), tw, th


def _try_match_in_viewport(
    page: Page,
    template_gray: np.ndarray,
    threshold: float,
    scales: Tuple[float, ...],
) -> Optional[ImageMatch]:
    """在当前 viewport 截图上做多尺度匹配，低于阈值返回 None。"""
    screen, shot_w, shot_h = _grab_viewport(page)
    screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)

    best = None  # (scale, conf, x, y, w, h)
    for scale in scales:
        r = _match_one_scale(screen_gray, template_gray, scale)
        if r is None:
            continue
        conf, x, y, w, h = r
        if best is None or conf > best[1]:
            best = (scale, conf, x, y, w, h)

    if best is None or best[1] < threshold:
        return None

    scale, conf, x, y, w, h = best

    # 像素坐标 → CSS 像素坐标：按截图尺寸与 viewport 尺寸的比例换算。
    # headless=False 时 viewport_size 可能为 None（跟随窗口），此时 1:1。
    vp = page.viewport_size
    css_w = vp["width"] if vp else shot_w
    css_h = vp["height"] if vp else shot_h
    ratio_x = css_w / shot_w
    ratio_y = css_h / shot_h

    return ImageMatch(
        x=(x + w / 2) * ratio_x,
        y=(y + h / 2) * ratio_y,
        width=w * ratio_x,
        height=h * ratio_y,
        confidence=conf,
        scale=scale,
        scroll_y=float(page.evaluate("window.scrollY")),
    )


def best_match(
    screen_bgr: np.ndarray,
    template_bgr: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
    scales: Tuple[float, ...] = DEFAULT_SCALES,
) -> Optional[Tuple[float, int, int, int, int, float]]:
    """在一张图里找模板（不涉及浏览器，桌面端也用它）。

    返回 (置信度, 左上角 x, 左上角 y, 宽, 高, 命中的缩放)；没到阈值返回 None。
    坐标是这张图自己的像素坐标。
    """
    screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
    best = None      # (conf, x, y, w, h, scale)
    for scale in scales:
        r = _match_one_scale(screen_gray, template_gray, scale)
        if r is None:
            continue
        conf, x, y, w, h = r
        if best is None or conf > best[0]:
            best = (conf, x, y, w, h, scale)
    if best is None or best[0] < threshold:
        return None
    return (best[0], best[1], best[2], best[3], best[4], best[5])


def locate_on_page(
    page: Page,
    template_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
    scales: Tuple[float, ...] = DEFAULT_SCALES,
    scroll_on_fail: bool = True,
    log: Callable[[str], None] = print,
) -> ImageMatch:
    """在页面中定位截图模板。

    流程：当前 viewport 匹配 → 失败则分段向下滚动逐屏匹配 → 到底仍失败抛异常。
    输出坐标始终是当前 viewport 的 CSS 像素，可直接 page.mouse.click。
    """
    if not template_path.exists():
        raise ImageNotFoundError(f"截图文件不存在：{template_path}")

    template = imread_unicode(template_path)
    th, tw = template.shape[:2]
    if th < 4 or tw < 4:
        raise ImageNotFoundError(f"截图尺寸过小（{tw}x{th}），请重新裁剪：{template_path.name}")
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    match = _try_match_in_viewport(page, template_gray, threshold, scales)
    if match:
        log(
            f"  截图匹配成功: 置信度={match.confidence:.3f} 缩放={match.scale} "
            f"坐标=({match.x:.0f},{match.y:.0f})"
        )
        return match

    if not scroll_on_fail:
        raise ImageNotFoundError(
            f"截图匹配失败（阈值 {threshold}）：{template_path.name}"
        )

    # 分段滚动查找：每屏滚 80%，留 20% 重叠避免元素恰好跨屏被切碎
    log("  当前视口未匹配到截图，开始向下滚动查找...")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(100)
    last_y = -1.0
    for _ in range(MAX_SCROLL_STEPS):
        page.evaluate(
            "window.scrollBy(0, Math.floor(window.innerHeight * 0.8))"
        )
        page.wait_for_timeout(150)
        cur_y = float(page.evaluate("window.scrollY"))
        if cur_y == last_y:
            break  # 已到页面底部
        last_y = cur_y

        match = _try_match_in_viewport(page, template_gray, threshold, scales)
        if match:
            log(
                f"  滚动 {cur_y:.0f}px 后匹配成功: 置信度={match.confidence:.3f} "
                f"缩放={match.scale} 坐标=({match.x:.0f},{match.y:.0f})"
            )
            return match

    page.evaluate("window.scrollTo(0, 0)")
    raise ImageNotFoundError(
        f"截图匹配失败（阈值 {threshold}，已整页滚动查找）：{template_path.name}。"
        "可能原因：元素不在页面上、页面未加载完成、截图与实际界面差异过大，"
        "或截图包含了过多背景。"
    )
