# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：小邹RPA（Windows，onedir）。

为什么用 onedir 而不是 onefile：
    · onefile 每次启动都要把自己解压到临时目录（400MB 解压要好几秒），而且那个
      临时目录退出就删 —— 安装向导"把程序复制到安装目录"时源目录会变得不可控；
    · onedir 出来的就是一个文件夹，正好符合"复制到 Program Files / 直接删文件夹卸载"。

打了什么进去：
    · smart_tool 全部代码 + assets（logo、海报、演示项目模板）；
    · playwright 的驱动（node.exe + cli.js，约 107MB）——浏览器本体不打包，
      由安装向导下载到 %LOCALAPPDATA%\\ms-playwright（约 150MB）；
    · PyQt6、opencv(cv2)、numpy、Pillow、openpyxl、xlrd、pyautogui、uiautomation、comtypes。

怎么用：
    powershell -ExecutionPolicy Bypass -File packaging\\build.ps1
    产物：dist\\小邹RPA\\小邹RPA.exe
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

#: spec 所在目录（packaging/），仓库根在它上一层
HERE = Path(SPECPATH).resolve()          # noqa: F821  (SPECPATH 由 PyInstaller 注入)
ROOT = HERE.parent
NAME = "小邹RPA"

# ---------------------------------------------------------------
# 图标：assets/logo.png 现场转一个多尺寸 .ico 给 exe 用
# （exe 图标必须是 .ico；程序运行时的窗口/快捷方式图标另有一份缓存，见 paths.icon_file）
# ---------------------------------------------------------------
icon_path = HERE / "小邹RPA.ico"
try:
    from PIL import Image

    Image.open(ROOT / "assets" / "logo.png").convert("RGBA").save(
        icon_path, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
except Exception as exc:                 # 没图标也能打包，只是 exe 用默认图标
    print(f"[打包] 生成图标失败，跳过：{exc}")
    icon_path = None

# ---------------------------------------------------------------
# 资源与依赖
# ---------------------------------------------------------------
datas = [(str(ROOT / "assets"), "assets")]      # logo / 求打赏海报 / 演示项目模板
binaries = []
hiddenimports = []

# 这几个包的内部文件（dll、node 驱动、COM 包装等）静态分析看不全，直接整包收进来
for pkg in ("playwright", "uiautomation", "comtypes", "cv2", "numpy",
            "PIL", "openpyxl", "xlrd", "pyautogui"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(                            # noqa: F821
    [str(ROOT / "smart_tool" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[                           # 用不到的大家伙，剔掉省体积
        "tkinter", "matplotlib", "scipy", "pandas", "PyQt5", "PySide6",
        "notebook", "IPython", "pytest", "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)                        # noqa: F821

exe = EXE(                               # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                           # UPX 压缩过的 exe 容易被杀软误报
    console=False,                       # GUI 程序：不弹黑窗
    disable_windowed_traceback=False,
    icon=str(icon_path) if icon_path else None,
)

coll = COLLECT(                          # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
