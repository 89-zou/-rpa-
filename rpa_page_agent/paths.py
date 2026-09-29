# -*- coding: utf-8 -*-
"""路径管理：程序文件、资源、用户数据，三种目录分清楚。

打包（PyInstaller）以后最容易出事的就是路径，所以这里一次说明白：

· `BUNDLE_DIR` / `ROOT_DIR`  程序文件所在目录
    - 源码运行＝仓库根（`smart_tool/` 的上一层）；
    - 打包后 ＝ exe 所在目录（onefile 时是临时解包目录，只读、退出即删）。
· `ASSETS_DIR`  随程序走的资源：logo.ico / 求打赏.jpg / 内置示例项目模板。
· `DATA_DIR`    **用户数据**：projects/（项目、账号密码、图片、采集结果、登录态）、
    浏览器/（Playwright 内核）、crash.log、日志。

数据目录按这个顺序定（绿色版优先，尽量不往 C 盘塞东西）：

    1) 配置里记着的 `data_dir` 目录存在且带 `.smart_tool_home` 标记文件 → 用它；
    2) 程序旁边就有 `projects/` → 就用程序目录（整个文件夹拷走即搬家）；
    3) 都没有 → 落到 `%APPDATA%\\小邹RPA` **占位**，但不会自动创建任何东西。

前两条都在启动时判定一次；第 3 种情况由首次运行的设置窗口负责 ——
用户选好路径后会写进配置（`%APPDATA%\\小邹RPA\\config.json`）并当场生效，
同时在目标目录里写 `.smart_tool_home` 标记文件。下次启动如果找不到标记文件，
说明目录被删了/搬走了，会再次弹出设置窗口让用户重新指定。

**绝不静默在 APPDATA 里自动创建 projects/、浏览器/ 等数据文件夹**。
"""
import json
import os
import sys
from pathlib import Path

#: 程序名（配置目录、窗口标题都用它）
APP_NAME = "小邹RPA"
AUTHOR = "@小邹"
#: 内置示例项目的名字（首次运行会被复制到 projects/ 下，程序默认打开它）
DEMO_PROJECT_NAME = "采集示例-登录与采集"

#: 配置目录（用户级，永远可写）
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

#: 用户数据目录的标记文件（set_data_dir 时写入，用来验证目录是否真的是本程序的家）
#: 为什么要它：光检查 projects/ 或 浏览器/ 太脆弱，任意文件夹碰巧有这些子目录就会被认对；
#: 这个文件由本程序写入，独一无二。
MARKER_FILE = ".smart_tool_home"


def _bundle_dir() -> Path:
    """程序文件目录（打包后指向解包目录/ exe 目录）。"""
    if getattr(sys, "frozen", False):        # PyInstaller 打的包
        meipass = getattr(sys, "_MEIPASS", "")
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """exe / 源码入口所在目录（绿色版判断用这个，不是临时解包目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BUNDLE_DIR


def is_production() -> bool:
    """是不是**生产版**（用 PyInstaller 打出来的 exe）。

    开发版（源码运行）与生产版只在「启动海报」上分：
    海报是给最终用户看的，写代码时每次启动都挡一下太烦。
    数据位置、浏览器内核这套两边完全一样（源码运行时仓库里什么都有，
    不会弹设置窗口）。
    """
    return bool(getattr(sys, "frozen", False))


BUNDLE_DIR = _bundle_dir()
#: 兼容旧名字：程序文件目录
ROOT_DIR = BUNDLE_DIR
#: 资源目录（logo、海报、示例项目模板）
ASSETS_DIR = BUNDLE_DIR / "assets"


# ------------------------------
# 配置文件
# ------------------------------
def load_config() -> dict:
    """读用户配置（不存在就给个空壳，不抛异常）。

    用 utf-8-sig 读：配置文件有时候会被记事本之类改过而带上 BOM，
    普通 utf-8 读出来开头会多一个 \\ufeff 导致 json 解析失败——那样整个配置
    都会被当成空的（数据目录全丢），坑很大。
    """
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**items) -> dict:
    """合并写回用户配置。"""
    data = load_config()
    data.update(items)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass
    return data


# ------------------------------
# 用户数据目录
# ------------------------------
def configured_data_dir() -> str:
    """配置里记着的用户数据目录（没设过就是空串）。"""
    return str(load_config().get("data_dir") or "").strip()


def data_dir_valid() -> bool:
    """配置里的 data_dir 目录还存在且有效吗？

    有效＝目录存在 + 里面有本程序的标记文件（.smart_tool_home）。
    这样用户把整个文件夹拷走/删掉/改名后，程序会及时发现并提示重新指定，
    而不是静默回退到 APPDATA 乱建文件夹。

    升级迁移：老版本用户没有 .smart_tool_home，但目录里有 projects/ 或 浏览器/
    就视为有效，并当场把标记文件补上（只写一次，不影响后续判断）。

    注意：判断通过时会顺手补写标记文件（迁移用），所以别在只读场景里反复调它。
    """
    custom = configured_data_dir()
    if not custom:
        return False
    try:
        p = Path(custom).expanduser()
        if not p.is_dir():
            return False
        marker = p / MARKER_FILE
        if marker.is_file():
            return True
        # 升级迁移：没有标记文件但有项目目录或浏览器内核目录 → 补上标记
        has_projects = (p / "projects").is_dir()
        has_browser = (p / "浏览器").is_dir() or (p / "ms-playwright").is_dir()
        if has_projects or has_browser:
            try:
                marker.write_text("小邹RPA 数据目录（升级自动写入）\n",
                                  encoding="utf-8")
            except OSError:
                pass                      # 写不进就算了，不影响有效性判断
            return True
        return False
    except OSError:
        return False


def _resolve_data_dir() -> Path:
    """启动时定数据目录。

    **口径必须跟 `data_dir_ready()` 一致（都用 data_dir_valid）**，
    否则会出现「DATA_DIR 指向 A、程序却以为数据目录已经定好」的错位 ——
    那样就会在一个来路不明的文件夹里建出 projects/，正是要避免的乱建。

    优先级：
        1) 配置里记着的目录**有效**（存在 + 有标记文件）→ 用它；
        2) 程序旁边就有 projects/（绿色版）→ 就用程序目录；
        3) 都没有 → 落到 CONFIG_DIR 占位（但不会自动创建，得等首次运行的窗口来正式定）。
    """
    if data_dir_valid():
        return Path(configured_data_dir()).expanduser()
    if (app_dir() / "projects").is_dir():    # 绿色版：程序旁边就有 projects/
        return app_dir()
    return CONFIG_DIR


#: 用户数据目录 / 项目目录（启动时定下来；换位置见 set_data_dir）
DATA_DIR = _resolve_data_dir()
PROJECTS_DIR = DATA_DIR / "projects"


def data_dir_ready() -> bool:
    """数据目录是不是已经定下来了（配置有效，或程序旁边本来就有 projects/）。

    没定下来＝第一次使用，启动时要让用户选一个位置。
    配置里有 data_dir 但目录失效（被删了/搬走了）也会返回 False，
    让用户重新指定，而不是偷偷在 APPDATA 里建东西。
    """
    if data_dir_valid():
        return True
    return (app_dir() / "projects").is_dir()


def is_writable(directory: Path) -> bool:
    """这个目录能不能写（建得出来、写得进去）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def suggested_data_dir() -> Path:
    """首次运行时建议的数据位置。

    优先程序所在目录（绿色版：projects/ 与 浏览器/ 都落在 exe 旁边，
    下次启动一看就知道，配置都不用写）；程序目录写不进去（比如装在
    C:\\Program Files）就退回「文档」。
    """
    beside = app_dir()
    if is_writable(beside):
        return beside
    docs = Path.home() / "Documents" / APP_NAME
    return docs if is_writable(docs) else CONFIG_DIR


def set_data_dir(path) -> Path:
    """把「用户数据目录」定下来：写进配置，并**当场生效**（改内存里的模块变量）。

    启动早期（建主界面之前）调用最省事，不用重启程序。
    会在目录里写一个标记文件（.smart_tool_home），下次启动用它验证目录没丢。
    """
    global DATA_DIR, PROJECTS_DIR
    target = Path(str(path)).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    # 写标记文件，让下次启动能认出这是本程序的家。
    # 注意：别在这里调 marker.stat() —— 文件当时还不存在会抛异常，
    # 整段被 except 吞掉，标记就写不出来了（踩过这个坑）。
    marker = target / MARKER_FILE
    try:
        marker.write_text("小邹RPA 数据目录（这个文件用来识别数据文件夹，别删）\n",
                          encoding="utf-8")
    except OSError:
        pass                              # 写不进就跳过，别因此报死
    save_config(data_dir=str(target))
    DATA_DIR = target
    PROJECTS_DIR = DATA_DIR / "projects"
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return target


def crash_log() -> Path:
    """崩溃日志放在用户数据目录。"""
    return DATA_DIR / "crash.log"


def trace_log() -> Path:
    """窗口状态跟踪日志（排查「抓完元素界面点不动」用的），也放用户数据目录。

    跟 crash.log 分开：crash.log 只记崩了的异常，这个记的是「没崩但界面废了」
    的现场（谁可见、谁被禁用、模态栈顶上是谁）。
    """
    return DATA_DIR / "picker_trace.log"


def ensure_dirs():
    """确保关键数据目录存在 —— 只在「数据目录已经定下来」时才动手。

    数据目录还没定的时候 DATA_DIR 只是 %APPDATA% 的**占位**，
    光调一下这个函数就会在 APPDATA 里建出 projects/ —— 正是要避免的乱建。
    （命令行 run_cli.py、api.py 这类不经过首次运行窗口的路子都靠这道守卫。）
    """
    if data_dir_ready():
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------
# 资源：图标 / 海报 / 内置演示项目模板
# ------------------------------
def templates_dir() -> Path:
    """内置模板目录（安装时从这儿把演示项目复制到用户数据目录）。"""
    return ASSETS_DIR / "templates"


def demo_template_dir() -> Path:
    """演示项目模板的目录。"""
    return templates_dir() / DEMO_PROJECT_NAME


def logo_file() -> Path:
    """原始 logo（png，1024×1024 那种方图最合适）。"""
    return ASSETS_DIR / "logo.png"


def poster_file() -> Path:
    """启动海报（求打赏图）。"""
    for name in ("求打赏.jpg", "求打赏.png", "poster.jpg", "poster.png"):
        p = ASSETS_DIR / name
        if p.is_file():
            return p
    return ASSETS_DIR / "求打赏.jpg"


def icon_file() -> Path:
    """应用图标（.ico）：从 logo.png 生成，缓存在配置目录。

    为什么要生成：Windows 的快捷方式 / exe 图标要 .ico，而 logo 是 png。
    缓存放配置目录（用户级、永远可写），不污染程序目录和项目数据目录。
    """
    ico = CONFIG_DIR / "logo.ico"
    src = logo_file()
    if ico.is_file() and (not src.is_file()
                          or ico.stat().st_mtime >= src.stat().st_mtime):
        return ico
    if not src.is_file():
        return ico                # 没有 logo：返回路径，调用方自己判断存不存在
    try:
        from PIL import Image
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.open(src).convert("RGBA")
        img.save(ico, format="ICO",
                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                        (64, 64), (128, 128), (256, 256)])
    except Exception:
        pass
    return ico
