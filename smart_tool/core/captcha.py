# -*- coding: utf-8 -*-
"""验证码识别：滑块 / 文字点选 / 计算题。

三种验证码分两条路：
- 滑块：不需要认字，只要「缺口在背景图上的横坐标」。优先用 ddddocr 的
  `slide_match`（拿滑块小图去背景图上对）；没装 ddddocr 也能用纯 OpenCV 的
  边缘匹配兜底。
- 文字点选 / 计算题：必须认字，只能靠 ddddocr（目标检测 + 文字识别）。

ddddocr 是**可选依赖**（它会一并带进 onnxruntime，加上自带的模型一共约
100 MB）。所以这个模块没装 ddddocr 也能正常 import，只是调认字的函数会抛
CaptchaError 并告诉你怎么装；**滑块完全不需要它**。

还有三个坑写在这儿免得再犯：
1. `cv2.imread(path, 0)` / `imdecode(..., 0)` 会把 PNG 的**透明像素当黑色**。
   滑块小图的透明边一旦变黑，匹配必然偏 —— 很多「识别得挺自信、位置就是错」
   都是这么来的。这里统一走 `_to_bgr()`，带 alpha 的先合成到白底。
2. ddddocr 的能力是**分了实例**的：`det=True` 的实例只出框、不给文字，而且
   再调 `classification` 会直接抛「当前识别类型为目标检测」。所以「出框 + 认字」
   得开两个实例、自己裁图拼起来。
3. ddddocr 的模型**偏好「浅色字 + 深色底」**：实测白底黑字的图，出框一个都不给、
   逐格认字也认错，同一张反色之后就全对了。所以出框、算算式这两条路都备了
   一次反色重试（见 `_invert_png`）。
"""
import ast
import importlib.util
import io
import operator
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

#: 三种验证码：键 → 中文名（界面上直接显示中文）
KINDS = (("slider", "滑块拼图"),
         ("click_text", "文字点选"),
         ("math", "计算题"))

#: 计算题认字时先把全角/中文运算符换成半角，否则「2×3」会被当成「23」。
#: 注意 str.translate 只认**单个字符**，多字词（「加上」）写进来会直接报错。
#: `十` 这一条是实测加的：OCR 很容易把 `+` 认成「十」，不换的话它会被当杂字剔掉，
#: 「3+5」就变成「35」算出一个安静的错答案。
_MATH_MAP = str.maketrans({
    "×": "*", "✕": "*", "✖": "*", "x": "*", "X": "*", "＊": "*",
    "÷": "/", "／": "/", "∕": "/",
    "＋": "+", "－": "-", "—": "-", "–": "-", "−": "-", "十": "+",
    "＝": "=", "？": "?", "，": "", ",": "",
    "加": "+", "减": "-", "乘": "*", "除": "/",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})
#: 算式里允许留下的字符
_MATH_KEEP = set("0123456789+-*/.()")
#: 安全求值只认这几个二元运算符（不用 eval，输入是外部图片认出来的，不可信）
_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
            ast.Mult: operator.mul, ast.Div: operator.truediv}
#: 滑块：边缘匹配的分低于它，才让 ddddocr 也看一眼
SLIDE_MIN_CONF = 0.5
#: 滑块：两条路给的坐标差这么多像素以上，就在日志里点出来（让人知道该怀疑）
SLIDE_DISAGREE_PX = 10
#: 探 ddddocr 能不能加载时最多等几秒（只 import 一下，正常几百毫秒）
OCR_PROBE_TIMEOUT = 60

#: ddddocr 的实例缓存（初始化很贵：要读 onnx 模型，别在循环里反复建）
_INSTANCES: Dict[str, Any] = {}
#: ddddocr 到底能不能加载（探过一次就记住；None＝还没探过）
_OCR_OK: Optional[bool] = None


class CaptchaError(Exception):
    """验证码识别出错（依赖没装、图认不出来、算不出答案……）。"""


# ------------------------------
# ddddocr：可选依赖
# ------------------------------
def ocr_installed() -> bool:
    """ddddocr 这个包装了没（**不 import**，很快；界面提示用这个）。"""
    try:
        return importlib.util.find_spec("ddddocr") is not None
    except Exception:
        return False


def _probe_import() -> bool:
    """在子进程里试着 import 一次，看它到底能不能加载。"""
    if getattr(sys, "frozen", False):
        # 打包成 exe 之后起不了「干净的解释器」来探（sys.executable 就是 exe 自己），
        # 只能就地 import 一次。所以打包时要么把能用的 onnxruntime 打进去，
        # 要么干脆别带 ddddocr —— 别带一个「装得上、一加载就崩」的版本。
        try:
            import ddddocr  # noqa: F401
        except Exception:
            return False
        return True
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import ddddocr"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=OCR_PROBE_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except Exception:
        return False


def ocr_available() -> bool:
    """ddddocr 现在真的能用吗（探测结果缓存，只探一次）。

    **为什么要在子进程里探、而不是直接 import 试试**：它依赖 onnxruntime 这类
    本地扩展，加载失败时可能不抛异常、而是让进程「访问违例」直接退出
    （本机实测某个版本就是这样，连报错的机会都没有）。这种崩法 `except` 抓不住，
    真发生在 GUI 里会把整个程序带走 —— 所以宁可多起一个进程问一句，
    崩也只崩这个探子。
    """
    global _OCR_OK
    if _OCR_OK is None:
        _OCR_OK = _probe_import()
    return _OCR_OK


def ensure_ocr():
    """用不了 ddddocr 时，给一句能照着做的中文提示（分清「没装」和「装了加载不了」）。"""
    if ocr_available():
        return
    if not ocr_installed():
        raise CaptchaError(
            "「文字点选 / 计算题」验证码要认字，需要 ddddocr，但没装上。\n"
            "    在项目的 .venv 里执行：.venv\\Scripts\\python -m pip install ddddocr\n"
            "    （会一并装上 onnxruntime；模型是随库来的，不用另外下，但加起来约 100 MB）\n"
            "    ── 滑块验证码不需要它，没装也能用。"
        )
    raise CaptchaError(
        "ddddocr 装是装上了，但在你这台机器上加载不起来（多半是它依赖的\n"
        "onnxruntime 和系统里的 VC++ 运行时对不上：本机实测 VC++ 是 14.23 时，\n"
        "装新版 onnxruntime 会加载失败，换老版本就好了）。\n"
        "    可以试：.venv\\Scripts\\python -m pip install \"onnxruntime==1.20.1\"\n"
        "    或者装一遍「Microsoft Visual C++ 2015-2022 可再发行组件」。\n"
        "    ── 滑块验证码不需要它，现在就能用。"
    )


def _instance(key: str):
    """按用途取 ddddocr 的实例（det＝只出框；ocr＝认字），建好就缓存。"""
    if key in _INSTANCES:
        return _INSTANCES[key]
    ensure_ocr()
    import ddddocr
    try:
        if key == "det":
            inst = ddddocr.DdddOcr(det=True, show_ad=False)
        else:
            inst = ddddocr.DdddOcr(show_ad=False)
    except TypeError:
        # 老版本没有 show_ad 参数
        inst = ddddocr.DdddOcr(det=(key == "det"))
    _INSTANCES[key] = inst
    return inst


# ------------------------------
# 图片工具
# ------------------------------
def _try(fn, what: str, *args, **kwargs):
    """调 ddddocr 的方法，把它自己的异常统一转成 CaptchaError。

    它抛的是 ImageProcessError 这类自己的异常类型，直接往上冒的话，
    外面就只能看到一句英文栈，不知道是「验证码图不对」还是「识别没认出来」。
    """
    try:
        return fn(*args, **kwargs)
    except CaptchaError:
        raise
    except Exception as e:
        raise CaptchaError(f"识别{what}失败：{e}") from e


def _to_bgr(data: bytes) -> np.ndarray:
    """bytes → OpenCV 三通道图；带 alpha 的先合成到白底。

    直接 `cv2.imdecode(..., 0)` 会把透明像素当黑色，滑块小图的透明边一变黑，
    匹配就偏了。这里是唯一入口，所有图都从这儿过。
    """
    with Image.open(io.BytesIO(data)) as im:
        if im.mode in ("RGBA", "LA", "PA"):
            im = im.convert("RGBA")
            base = Image.new("RGB", im.size, (255, 255, 255))
            base.paste(im, mask=im.split()[-1])
            im = base
        else:
            im = im.convert("RGB")
        arr = np.array(im)
    return np.ascontiguousarray(arr[:, :, ::-1])       # RGB → BGR


def _crop_png(data: bytes, x1: int, y1: int, x2: int, y2: int) -> bytes:
    """从图上裁一块出来，转成 PNG 字节（给认字用）。"""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB").crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def _invert_png(data: bytes) -> bytes:
    """反色（浅底深字 ↔ 深底浅字）。

    为什么需要它：ddddocr 的模型**明显偏好「浅色字 + 深色底」**。实测拿白底黑字
    的图给它，出框一个都不给、逐格认字也认错；同一张反色之后就全对了。
    真实验证码大多是深字浅底，所以主路径不动，只在主路径拿不到东西时补一次 ——
    不赌哪一边，谁说得通用谁。
    """
    with Image.open(io.BytesIO(data)) as im:
        img = ImageOps.invert(im.convert("RGB"))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


# ------------------------------
# 滑块
# ------------------------------
def slide_offset(target: bytes, background: bytes) -> Tuple[float, float, str]:
    """缺口在背景图上的横坐标 → (偏移, 把握程度, 怎么算出来的)。

    主力是**边缘匹配**：滑块小图就是从背景上抠下来的那一块，两者的边缘几乎
    完全重合 —— 这个信号又强又干净，而且对不对自己看得见（分低就是没对上）。

    ddddocr 的 `slide_match` 只在边缘匹配没把握（分低于 SLIDE_MIN_CONF）时才上，
    因为它的分**不能当准信**：实测在一张很干净的合成图上它报了 1.00 的把握、
    坐标却偏了 100 像素。所以这里不但按分选，还额外比一下两条路的结果 ——
    差得多就在第三个返回值里点出来（会打进日志），免得出问题时无从下手。

    返回的第三个值会打进运行日志，一眼能看出坐标是哪条路给的。
    """
    cv_x, cv_conf = _slide_offset_cv(target, background)
    dd = _slide_by_ddddocr(target, background)      # 没装/认不出就是 None

    if dd is None or cv_conf >= SLIDE_MIN_CONF:
        how = "边缘匹配"
        if dd is not None and abs(dd[0] - cv_x) >= SLIDE_DISAGREE_PX:
            how += f"（ddddocr 给的是 {dd[0]:.0f}，两边差得多，以我为准；"
            how += "要是滑不过去，把这条日志发出来看看）"
        return cv_x, cv_conf, how

    if dd[1] > cv_conf:
        how = "ddddocr（边缘匹配没把握）"
        if abs(dd[0] - cv_x) >= SLIDE_DISAGREE_PX:
            how += f"（边缘匹配给的是 {cv_x:.0f}，两边差得多，注意核对）"
        return dd[0], dd[1], how
    return cv_x, cv_conf, "边缘匹配（把握偏低；ddddocr 也没更有把握）"


def _slide_by_ddddocr(target: bytes, background: bytes
                      ) -> Optional[Tuple[float, float]]:
    """ddddocr 的缺口位置 → (偏移, 它自己报的把握)；用不了就 None。"""
    if not ocr_installed():
        return None
    try:
        # slide_match 是 DdddOcr 的通用方法，借认字的实例调就行 ——
        # 每建一个实例都要读一遍 onnx 模型，别为它再单开一个
        res = _instance("ocr").slide_match(target, background,
                                           simple_target=True)
        x = res.get("target_x")
        if x is None:
            box = res.get("target") or []
            x = box[0] if box else None
        if x is None:
            return None
        return float(x), float(res.get("confidence") or 0.0)
    except Exception:
        return None                   # 它认不出来就算了，边缘匹配的分还在


def _slide_offset_cv(target: bytes, background: bytes) -> Tuple[float, float]:
    """纯 OpenCV：两边都取边缘再匹配（不需要 ddddocr）。"""
    t = _to_bgr(target)
    b = _to_bgr(background)
    if t.size == 0 or b.size == 0:
        raise CaptchaError("滑块图或背景图读不出来（空图）。")
    tg = cv2.Canny(cv2.cvtColor(t, cv2.COLOR_BGR2GRAY), 100, 200)
    bg = cv2.Canny(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), 100, 200)
    if tg.shape[0] > bg.shape[0] or tg.shape[1] > bg.shape[1]:
        raise CaptchaError(
            "滑块小图比背景图还大，没法匹配 —— 多半是两张图传反了。")
    res = cv2.matchTemplate(bg, tg, cv2.TM_CCOEFF_NORMED)
    _min_v, score, _min_l, loc = cv2.minMaxLoc(res)
    return float(loc[0]), float(score)


# ------------------------------
# 点选 / 计算题：认字
# ------------------------------
def read_text(image: bytes) -> str:
    """整图认字（题干那种一行的图用）。"""
    ensure_ocr()
    return (_try(_instance("ocr").classification, "认字", image) or "").strip()


def read_boxes(image: bytes) -> List[Tuple[int, int, int, int, str]]:
    """出框 + 每格认字 → [(x1,y1,x2,y2,字), ...]。

    ddddocr 的能力分了两个实例：det 那个只给坐标、不给文字，所以这里自己裁
    每一格、交给认字的实例。**框的顺序是乱的**（不按左右），别当顺序用 ——
    要点哪几格由题干决定（见 order_targets）。

    主路径一个框都没给时，反色再试一次（模型对深浅敏感，见 _invert_png）；
    裁图也从反色那张上裁，保证框和像素是同一套。
    """
    ensure_ocr()
    det = _instance("det")
    raw = _try(det.detection, "找文字块", image) or []
    source = image
    if not raw:
        flipped = _invert_png(image)
        raw = _try(det.detection, "找文字块", flipped) or []
        if raw:
            source = flipped
    out: List[Tuple[int, int, int, int, str]] = []
    for box in raw:
        if len(box) < 4:
            continue
        x1, y1, x2, y2 = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        if x2 <= x1 or y2 <= y1:
            continue
        try:
            text = (_try(_instance("ocr").classification, "认字",
                         _crop_png(source, x1, y1, x2, y2)) or "").strip()
        except CaptchaError:
            text = ""                 # 单格认不出不算致命，留空、让它被题干筛掉
        out.append((x1, y1, x2, y2, text))
    return out


def order_targets(prompt: str, texts: List[str]) -> List[int]:
    """要依次点哪几格 → 下标列表，按**题干里的先后**排。

    不去解析题干的语法（「请依次点击：圈、流、伟」写法千奇百怪），改成
    「题干里出现了哪几格的字，就按题干里的位置排」—— 这样题干怎么写都能用，
    认错的格子自动就排除了。
    """
    hits: List[Tuple[int, int]] = []
    for idx, t in enumerate(texts):
        t = (t or "").strip()
        if not t:
            continue
        pos = prompt.find(t)
        if pos >= 0:
            hits.append((pos, idx))
    if not hits:
        # 整格没对上（识别成了两个字、或题干只给单个字）：退一步按单字找
        for idx, t in enumerate(texts):
            for ch in (t or "").strip():
                pos = prompt.find(ch)
                if pos >= 0:
                    hits.append((pos, idx))
                    break
    hits.sort()
    return [idx for _pos, idx in hits]


# ------------------------------
# 计算题
# ------------------------------
def normalize_math(text: str) -> str:
    """把认出的一行字收拾成纯算式（全角/中文运算符先换掉，再剔杂字）。"""
    s = (text or "").translate(_MATH_MAP)
    s = "".join(ch for ch in s if ch in _MATH_KEEP)
    return s.strip("=").strip()


def _eval_node(node) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Div) and right == 0:
            raise CaptchaError("算式里有除以 0。")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    raise CaptchaError("算式里有不认识的写法。")


def solve_math(text: str) -> Tuple[str, str]:
    """认到的文字 → (算式, 答案文本)。\"3+5=?\" → (\"3+5\", \"8\")。"""
    expr = normalize_math(text)
    if not expr:
        raise CaptchaError(
            f"这一行没认出算式（识别结果：{text!r}）。\n"
            "   多半是验证码太糊 / 有干扰线，点【换一张】重试。")
    try:
        value = _eval_node(ast.parse(expr, mode="eval"))
    except CaptchaError:
        raise
    except Exception as e:
        raise CaptchaError(f"算式 {expr!r} 算不出来：{e}") from e
    if value == int(value):
        return expr, str(int(value))
    return expr, ("%g" % value)


def solve_math_image(image: bytes) -> Tuple[str, str, str]:
    """验证码图 → (认到的原文, 算式, 答案)；认不出算式就反色再认一次。

    为什么要试两种极性：模型对深浅敏感，同一张图正着认可能把「+」认成「十」，
    反过来认就对了。所以不赌哪一边 —— 谁认出的算式能算通就用谁。
    （这也是为什么这里不是先 read_text 再 solve_math：得让「算不算得通」
    来当裁判，而不是先定死用哪次认字的结果。）
    """
    last: Optional[CaptchaError] = None
    ensure_ocr()
    for data in (image, _invert_png(image)):
        text = (_try(_instance("ocr").classification, "认字", data) or "").strip()
        try:
            expr, answer = solve_math(text)
            return text, expr, answer
        except CaptchaError as e:
            last = e
    raise last if last else CaptchaError("这张图没认出算式。")
