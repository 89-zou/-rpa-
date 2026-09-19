# -*- coding: utf-8 -*-
"""路径管理：程序文件、资源、用户数据，三种目录分清楚。

打包（PyInstaller）以后最容易出事的就是路径，所以这里一次说明白：

· `BUNDLE_DIR` / `ROOT_DIR`  程序文件所在目录
     - 源码运行＝仓库根（`smart_tool/` 的上一层）；
     - 打包后 ＝ exe 所在目录（onefile 时是临时解包目录，只读、退出即删）。
· `ASSETS_DIR`  随程序走的资源：logo.ico / 求打赏.jpg 这类图。
· `DATA_DIR`    **用户数据**：projects/（项目、账号密码、图片、采集结果、登录态）、
     crash.log、日志。这个目录必须可写，所以：
       1) 安装时用户指定过 → 用指定的；
       2) 程序旁边有 `projects/`（绿色版）→ 就用程序目录；
       3) 都不满足 → `%APPDATA%\\小邹RPA`。

配置文件固定在 `%APPDATA%\\小邹RPA\\config.json`（换安装位置也不丢设置）。
"""
import json
import os
import sys
from pathlib import Path

#: 程序名（配置目录、快捷方式、窗口标题都用它）
APP_NAME = "小邹RPA"
AUTHOR = "@小邹"
#: 内置演示项目的名字（安装时会被复制到用户数据目录，程序启动默认打开它）
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

    开发版（源码运行）与生产版的区别，只在这一处分：
    · 开发版：入口直接进主界面 —— 不弹安装向导、不显示启动海报、
      不往注册表/系统里写任何东西（那套只有交付给用户时才有意义）；
    · 生产版：第一次运行弹安装向导（选文件夹、构建环境、建快捷方式、
      登记卸载入口），之后每次启动显示启动海报再进主界面。

    想手动测那套流程：开发版照样可以 `python -m smart_tool.setup_wizard`
    或 `python -m smart_tool.main --setup`。
    """
    return bool(getattr(sys, "frozen", False))


BUNDLE_DIR = _bundle_dir()
#: 兼容旧名字：程序文件目录
ROOT_DIR = BUNDLE_DIR
#: 资源目录（logo、海报）
ASSETS_DIR = BUNDLE_DIR / "assets"


# ------------------------------
# 配置文件
# ------------------------------
def load_config() -> dict:
    """读用户配置（不存在就给个空壳，不抛异常）。

    用 utf-8-sig 读：配置文件有时候会被记事本之类改过而带上 BOM，
    普通 utf-8 读出来开头会多个 \\ufeff 导致 json 解析失败——那样整个配置
    都会被当成空的（数据目录、安装位置全丢），坑很大。
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
def _resolve_data_dir() -> Path:
    custom = str(load_config().get("data_dir") or "").strip()
    if custom:
        try:
            p = Path(custom).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass                    # 指定的目录不可用就退回默认，别让程序起不来
    portable = app_dir() / "projects"       # 绿色版：程序旁边就有 projects/
    if portable.is_dir():
        return app_dir()
    return CONFIG_DIR


#: 用户数据目录 / 项目目录 / 崩溃日志（启动时定下来；安装向导改完需重启生效）
DATA_DIR = _resolve_data_dir()
PROJECTS_DIR = DATA_DIR / "projects"


def set_data_dir(path) -> Path:
    """把「用户数据目录」写进配置（下次启动生效），返回规范化后的路径。"""
    target = Path(str(path)).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    save_config(data_dir=str(target))
    return target


def crash_log() -> Path:
    """崩溃日志放在用户数据目录（安装到 Program Files 也能写）。"""
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
