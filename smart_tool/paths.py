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

    1) 设置里选过 → 用选的那个；
    2) 程序旁边就有 `projects/` → 就用程序目录（整个文件夹拷走即搬家）；
    3) 都没有 → 先落在 `%APPDATA%\\小邹RPA`，并提示用户选一个正式位置。

前两条都在启动时判定一次；第 3 种情况由首次运行的设置窗口负责 ——
用户选好路径后会写进配置（`%APPDATA%\\小邹RPA\\config.json`）并当场生效。
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


def _resolve_data_dir() -> Path:
    custom = configured_data_dir()
    if custom:
        try:
            p = Path(custom).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass                    # 指定的目录不可用就退回默认，别让程序起不来
    if (app_dir() / "projects").is_dir():    # 绿色版：程序旁边就有 projects/
        return app_dir()
    return CONFIG_DIR


#: 用户数据目录 / 项目目录（启动时定下来；换位置见 set_data_dir）
DATA_DIR = _resolve_data_dir()
PROJECTS_DIR = DATA_DIR / "projects"


def data_dir_ready() -> bool:
    """数据目录是不是已经定下来了（配置里选过，或程序旁边本来就有 projects/）。

    没定下来＝第一次使用，启动时要让用户选一个位置。
    """
    if configured_data_dir():
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
    """
    global DATA_DIR, PROJECTS_DIR
    target = Path(str(path)).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    save_config(data_dir=str(target))
    DATA_DIR = target
    PROJECTS_DIR = DATA_DIR / "projects"
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return target


def crash_log() -> Path:
    """崩溃日志放在用户数据目录。"""
    return DATA_DIR / "crash.log"


def ensure_dirs():
    """确保关键目录存在。"""
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
