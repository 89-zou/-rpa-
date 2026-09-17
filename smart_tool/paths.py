# -*- coding: utf-8 -*-
"""路径管理：统一项目内的关键路径。"""
from pathlib import Path

# 项目根目录（smart_tool 包的父目录，即 .../smart_tool/）
ROOT_DIR = Path(__file__).resolve().parent.parent

# 项目存储目录（每个子目录是一个自动化项目）
PROJECTS_DIR = ROOT_DIR / "projects"


def ensure_dirs():
    """确保关键目录存在。"""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
