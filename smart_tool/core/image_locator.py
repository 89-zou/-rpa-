# -*- coding: utf-8 -*-
"""截图定位：OpenCV 多尺度模板匹配（灰度 + 边缘双通道）。

两种用法
--------
1) 网页（Playwright）：`locate_on_page()` 在 viewport 里找，必要时滚动整页找。
2) 桌面（整屏截图）：`best_match()` 在一张图里找，坐标是这张图自己的像素。

匹配策略（浅色 / 低对比界面下最容易出问题，所以做了三层防护）
------------------------------------------------------------
- **灰度 + 边缘双通道**：灰度（TM_CCOEFF_NORMED）先试；浅色界面上按钮和背景几乎同色时，
  灰度会给出「一片虚高」，这时改用 Canny 边缘图再匹配一次——边缘只看结构不看明暗。
- **假高分抑制**：只有当「另一个位置也过了阈值、而且分差很小」时才判为不可信
  （`ambiguous`）并放弃，避免在纯色区域里随便点一个最高点。唯一命中的模板不受影响。
- **缩放记忆**：命中过的倍数记下来，下次先在它附近 ±2% 细扫，再退到默认网格；
  换分辨率 / 改系统缩放后照样能对上，也更快。

坐标约定（避免历史偏移问题，全部显式化）
- 模板/截图内坐标：像素 px
- 网页路径对外输出：viewport CSS 像素，与 page.mouse.click 1:1
- 桌面路径对外输出：截图图像的像素，调用方加屏幕原点（见 desktop.screen_origin）
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from playwright.sync_api import Page

# Windows 常见显示缩放 100/125/150/175/200%，模板可能来自不同缩放的屏幕。
# scale = 模板相对当前截图的缩放系数，1.0 最先试。
DEFAULT_SCALES: Tuple[float, ...] = (
    1.0, 1.25, 0.8, 1.5, 0.667, 1.75, 0.571, 0.5, 2.0,
)
DEFAULT_THRESHOLD = 0.8
#: 命中过的缩放：下次优先在这些倍数附近细扫（按比例增减）
FINE_STEPS: Tuple[float, ...] = (0.0, -0.02, 0.02, -0.04, 0.04, -0.06, 0.06)
_SCALE_CACHE: Dict[str, float] = {}
#: 「另一个位置也过了阈值」且分差小于它 → 认为一片都差不多，不可信
AMBIGUOUS_MARGIN = 0.05
#: 找第二高峰时躲开最高峰的半径（按模板宽高的比例）
PEAK_SUPPRESS_RATIO = 0.5
#: 模板灰度标准差低于它 ＝ 几乎是纯色（会提醒一句，仍然继续匹配）
FLAT_STD = 6.0
#: 自动裁边：一整条（行/列）的标准差 ≤ 它 ＝ 这条是「空边」（多半是背景）
TRIM_STD = 3.0
#: 自动裁边：每边最多裁掉多少比例 / 裁完至少留多大
TRIM_MAX_RATIO = 0.4
TRIM_KEEP = 8
#: 边缘通道的阈值可以比灰度低这么多 —— 边缘图里模板的背景部分是 0，
#: 分数天然比灰度低（实测同一张图：灰度 0.85 / 边缘 0.63），不降就永远轮不到它
EDGE_THRESHOLD_DROP = 0.2
#: Canny 边缘通道的两个阈值
CANNY_LOW, CANNY_HIGH = 50, 150
MAX_SCROLL_STEPS = 15

#: 匹配用的通道名（日志里说清楚命中的是哪一个）
CHANNEL_CN = {"gray": "灰度", "edge": "边缘"}


class ImageNotFoundError(Exception):
    """截图匹配失败。"""


@dataclass
class MatchResult:
    """在一张图里匹配到的位置（那张图自己的像素坐标，中心点）。"""
    x: float
    y: float
    width: float
    height: float
    confidence: float
    scale: float
    channel: str = "gray"       # gray＝灰度命中；edge＝改用边缘图命中


@dataclass
class ImageMatch:
    """网页路径的匹配结果（viewport CSS 像素，中心点坐标）。"""
    x: float           # 中心 x（CSS px）
    y: float           # 中心 y（CSS px）
    width: float       # 匹配框宽（CSS px）
    height: float      # 匹配框高（CSS px）
    confidence: float  # 相关系数 0~1
    scale: float       # 命中的模板缩放系数
    scroll_y: float    # 命中时页面已滚动距离（信息字段）


def describe(match: Optional[MatchResult]) -> str:
    """一句话说清这次匹配（写日志用）。"""
    if match is None:
        return "没匹配到"
    return (f"置信度={match.confidence:.3f} 缩放={match.scale:g} "
            f"通道={CHANNEL_CN.get(match.channel, match.channel)}")


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


def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _to_edges(gray: np.ndarray) -> np.ndarray:
    """边缘通道：只看结构，不看明暗（浅色界面的救星）。"""
    return cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)


def _auto_trim(gray: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    """把模板四周的「空边」裁掉，返回 (裁后的图, (上, 下, 左, 右))。

    判据是**这一整条行/列有没有变化**（标准差），不是「颜色像不像背景」——
    浅色界面上按钮本身就是浅色的，按颜色判会把按钮一起吃掉。

    为什么值得做：模板里带的那一圈背景，一旦变色（窗口失焦变灰、深色主题、
    渐变背景），相关系数会被它拉下来（实测 1.00 → 0.71/0.42），
    而裁掉之后同一场景回到 0.82/0.57。框得越松的模板受益越大。
    """
    h, w = gray.shape[:2]
    top = bot = left = right = 0
    max_v, max_h = int(h * TRIM_MAX_RATIO), int(w * TRIM_MAX_RATIO)

    def flat(line: np.ndarray) -> bool:
        return float(line.std()) <= TRIM_STD

    while top + bot < max_v and h - top - bot > TRIM_KEEP and flat(gray[top, :]):
        top += 1
    while top + bot < max_v and h - top - bot > TRIM_KEEP and flat(gray[h - 1 - bot, :]):
        bot += 1
    while left + right < max_h and w - left - right > TRIM_KEEP and flat(gray[:, left]):
        left += 1
    while left + right < max_h and w - left - right > TRIM_KEEP and flat(gray[:, w - 1 - right]):
        right += 1
    if not (top or bot or left or right):
        return gray, (0, 0, 0, 0)
    core = gray[top:h - bot, left:w - right]
    if float(core.std()) < 2.0:          # 裁完几乎没结构了 → 干脆别裁
        return gray, (0, 0, 0, 0)
    return core, (top, bot, left, right)


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
# 缩放候选（带记忆）
# ------------------------------
def scale_candidates(key: Optional[str],
                     scales: Optional[Sequence[float]] = None) -> Tuple[float, ...]:
    """这次要试哪些缩放：命中过就先在它附近细扫，再退到默认网格。"""
    if scales is not None:
        return tuple(scales)
    out: List[float] = []
    cached = _SCALE_CACHE.get(key) if key else None
    if cached:
        out.extend(round(cached * (1 + step), 4) for step in FINE_STEPS)
    out.extend(DEFAULT_SCALES)
    seen: set = set()
    uniq: List[float] = []
    for v in out:
        if 0.2 <= v <= 3.0 and v not in seen:
            seen.add(v)
            uniq.append(v)
    return tuple(uniq)


def remember_scale(key: Optional[str], scale: float):
    """记住这次命中的缩放（下次优先试它附近）。"""
    if key:
        _SCALE_CACHE[key] = float(scale)


def forget_scales(key: Optional[str] = None):
    """清掉缩放记忆（key 为空则全清）——换分辨率后想从头试就用它。"""
    if key is None:
        _SCALE_CACHE.clear()
    else:
        _SCALE_CACHE.pop(key, None)


# ------------------------------
# 模板匹配
# ------------------------------
def _match_one_scale(
    screen: np.ndarray,
    template: np.ndarray,
    scale: float,
) -> Optional[Tuple[np.ndarray, int, int]]:
    """单尺度匹配。返回 (结果图, 模板宽, 模板高)；模板比画面大时返回 None。"""
    if abs(scale - 1.0) < 1e-6:
        templ = template
    else:
        th0, tw0 = template.shape[:2]
        new_w = max(1, int(round(tw0 * scale)))
        new_h = max(1, int(round(th0 * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        templ = cv2.resize(template, (new_w, new_h), interpolation=interpolation)

    th, tw = templ.shape[:2]
    sh, sw = screen.shape[:2]
    if th > sh or tw > sw:
        return None
    result = cv2.matchTemplate(screen, templ, cv2.TM_CCOEFF_NORMED)
    return result, tw, th


def _peaks(result: np.ndarray, tw: int, th: int) -> Tuple[float, Tuple[int, int], float]:
    """(最高峰, 位置, 第二高峰)；第二高峰会躲开最高峰周围一块。

    用切片取四周的最大值，避免为「抹掉最高峰」而整图复制一份（整屏时很贵）。
    """
    _, best_val, _, best_loc = cv2.minMaxLoc(result)
    bx, by = int(best_loc[0]), int(best_loc[1])
    mx = max(2, int(tw * PEAK_SUPPRESS_RATIO))
    my = max(2, int(th * PEAK_SUPPRESS_RATIO))
    x0, x1 = max(0, bx - mx), min(result.shape[1], bx + mx + 1)
    y0, y1 = max(0, by - my), min(result.shape[0], by + my + 1)
    parts = [result[:y0, :], result[y1:, :],
             result[y0:y1, :x0], result[y0:y1, x1:]]
    second = max((float(p.max()) for p in parts if p.size), default=-1.0)
    return float(best_val), (bx, by), second


def _search(
    screen: np.ndarray,
    template: np.ndarray,
    scales: Sequence[float],
    threshold: float,
    channel: str,
    trace: Optional[List[str]] = None,
) -> Optional[MatchResult]:
    """在 screen（灰度或边缘图）里多尺度找 template。

    返回**可信**的最佳命中；不可信的（一片虚高）返回 None，并在 trace 里写一句原因。
    """
    if screen.size == 0 or template.size == 0:
        return None
    th0, tw0 = template.shape[:2]
    sh, sw = screen.shape[:2]
    if th0 > sh or tw0 > sw:
        if trace is not None:
            trace.append(f"{CHANNEL_CN.get(channel, channel)}：模板比画面还大，跳过")
        return None

    best: Optional[Tuple[float, int, int, int, int, float, float]] = None
    # (置信度, x, y, w, h, scale, 第二高峰)
    for scale in scales:
        r = _match_one_scale(screen, template, scale)
        if r is None:
            continue
        result, tw, th = r
        val, loc, second = _peaks(result, tw, th)
        if best is None or val > best[0]:
            best = (val, int(loc[0]), int(loc[1]), tw, th, scale, second)

    if best is None:
        return None
    val, x, y, w, h, scale, second = best
    label = CHANNEL_CN.get(channel, channel)
    if val < threshold:
        if trace is not None:
            trace.append(f"{label}：最高只有 {val:.3f}（阈值 {threshold:g}）")
        return None
    # 只有「别处也过了阈值、而且分差很小」才算不可信 —— 唯一命中的模板照旧放行
    if second >= threshold and (val - second) < AMBIGUOUS_MARGIN:
        if trace is not None:
            trace.append(
                f"{label}：命中 {val:.3f}，但另一处也有 {second:.3f}（分差太小），"
                "不可信、已忽略 —— 换一张更干净的模板，或把相似度调高一点"
            )
        return None
    return MatchResult(x=x + w / 2, y=y + h / 2, width=w, height=h,
                       confidence=val, scale=scale, channel=channel)


def _to_original(hit: MatchResult, cut: Tuple[int, int, int, int],
                 orig_wh: Tuple[int, int]) -> MatchResult:
    """把「裁边后模板」的命中点，换算回**用户原来框的那个范围**的中心。

    裁边只是内部优化：用户框的是红框那一块，点击点必须是红框中心，
    不能因为内部裁了几行几列就把点漂走。
    """
    top, bot, left, right = cut
    if not (top or bot or left or right):
        return hit
    th_o, tw_o = orig_wh
    s = float(hit.scale)
    hit.x = hit.x + s * (tw_o / 2 - left) - hit.width / 2
    hit.y = hit.y + s * (th_o / 2 - top) - hit.height / 2
    hit.width = tw_o * s
    hit.height = th_o * s
    return hit


def best_match(
    screen_bgr: np.ndarray,
    template_bgr: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
    scales: Optional[Sequence[float]] = None,
    use_edges: bool = True,
    key: Optional[str] = None,
    trace: Optional[List[str]] = None,
) -> Optional[MatchResult]:
    """在一张图里找模板（不涉及浏览器，桌面端也用它）。

    :param threshold: 相似度阈值（0~1）；浅色界面调低一点更容易命中
    :param scales: 指定缩放候选；留空＝用「上次命中的附近 + 默认网格」
    :param use_edges: 灰度不可信时，改用边缘图再试一次（阈值自动降 EDGE_THRESHOLD_DROP）
    :param key: 缩放记忆的键（一般传模板文件路径）
    :param trace: 传一个列表进来，会把「为什么没匹配上」写进去（调用方拿去打日志）
    返回 MatchResult（坐标是这张图自己的像素，中心点），没匹配到返回 None。
    """
    gray_screen = _to_gray(screen_bgr)
    gray_templ, cut = _auto_trim(_to_gray(template_bgr))
    orig_wh = _to_gray(template_bgr).shape[:2]
    if cut != (0, 0, 0, 0) and trace is not None:
        trace.append("模板自动去掉了四周的空白边"
                     f"（上{cut[0]} 下{cut[1]} 左{cut[2]} 右{cut[3]} 像素）")
    if float(gray_templ.std()) < FLAT_STD and trace is not None:
        trace.append(f"提醒：模板几乎是纯色的（标准差 {gray_templ.std():.1f}），"
                     "很难可靠匹配 —— 建议框住带纹理/边框的部分")

    cands = scale_candidates(key, scales)
    gray_hit = _search(gray_screen, gray_templ, cands, threshold, "gray", trace)
    if gray_hit is not None:
        remember_scale(key, gray_hit.scale)
        return _to_original(gray_hit, cut, orig_wh)

    if not use_edges:
        return None
    edge_threshold = max(0.4, float(threshold) - EDGE_THRESHOLD_DROP)
    edge_hit = _search(_to_edges(gray_screen), _to_edges(gray_templ),
                       cands, edge_threshold, "edge", trace)
    if edge_hit is not None:
        remember_scale(key, edge_hit.scale)
        return _to_original(edge_hit, cut, orig_wh)
    return None


# ------------------------------
# 网页：在 viewport 里找（必要时滚动整页找）
# ------------------------------
def _try_match_in_viewport(
    page: Page,
    template_bgr: np.ndarray,
    threshold: float,
    scales: Sequence[float],
    key: Optional[str] = None,
    trace: Optional[List[str]] = None,
) -> Optional[ImageMatch]:
    """在当前 viewport 截图上匹配，低于阈值返回 None。"""
    screen, shot_w, shot_h = _grab_viewport(page)
    hit = best_match(screen, template_bgr, threshold=threshold,
                     scales=scales, key=key, trace=trace)
    if hit is None:
        return None

    # 像素坐标 → CSS 像素坐标：按截图尺寸与 viewport 尺寸的比例换算。
    # headless=False 时 viewport_size 可能为 None（跟随窗口），此时 1:1。
    vp = page.viewport_size
    css_w = vp["width"] if vp else shot_w
    css_h = vp["height"] if vp else shot_h
    ratio_x = css_w / shot_w
    ratio_y = css_h / shot_h

    return ImageMatch(
        x=hit.x * ratio_x,
        y=hit.y * ratio_y,
        width=hit.width * ratio_x,
        height=hit.height * ratio_y,
        confidence=hit.confidence,
        scale=hit.scale,
        scroll_y=float(page.evaluate("window.scrollY")),
    )


def locate_on_page(
    page: Page,
    template_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
    scales: Optional[Sequence[float]] = None,
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

    key = str(template_path)
    trace: List[str] = []
    match = _try_match_in_viewport(page, template, threshold,
                                  tuple(scales) if scales else scale_candidates(key),
                                  key=key, trace=trace)
    if match:
        hit = MatchResult(match.x, match.y, match.width, match.height,
                          match.confidence, match.scale)
        log(f"  截图匹配成功: {describe(hit)}")
        return match

    if not scroll_on_fail:
        raise ImageNotFoundError(
            f"截图匹配失败（阈值 {threshold}）：{template_path.name}"
            + _why(trace)
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

        trace.clear()
        match = _try_match_in_viewport(page, template, threshold,
                                      scale_candidates(key),
                                      key=key, trace=trace)
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
        "或截图包含了过多背景。" + _why(trace)
    )


def _why(trace: Optional[List[str]]) -> str:
    """把匹配过程中的原因拼成一句（没有就返回空串）。"""
    if not trace:
        return ""
    return "\n    " + "\n    ".join(dict.fromkeys(trace[-2:]))
