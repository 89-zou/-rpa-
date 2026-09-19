# -*- coding: utf-8 -*-
"""登录态（cookie / localStorage）存管：登录一次，以后直接用。

文件就是 Playwright 的 storage_state 格式（`{"cookies": [...], "origins": [...]}`），
放在 `projects/<项目名>/auth/<名字>.json`。

**为什么不自己把 cookie 拼成字符串/变量**：
- `document.cookie` 读不到 httpOnly 的 cookie（WordPress 的登录 cookie 正是），
- localStorage 里的 token 也完全不在 cookie 里，
- 手拼还容易漏 domain / path / expires / SameSite，站点会判定无效，
  表现为「明明带了 cookie 还是被踢回登录页」。
storage_state 是 Playwright 原生格式，上面这些一次存全、一次读回。

登录态会过期，所以执行器那边配套做了三件事：用之前先「体检」（查一个登录后才有的
元素在不在）、体检不过就整条流程重跑一遍（走完整登录步骤）、跑完把最新 cookie 存回去
（续期）。
"""
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

#: 登录态放在项目里的哪个子目录
AUTH_DIR_NAME = "auth"
#: 没起名时的默认名字
DEFAULT_NAME = "默认登录"
#: 名字里不允许出现的字符（它会变成文件名）
_BAD_CHARS = '\\/:*?"<>|\n\r\t'
MAX_NAME_LEN = 40


@dataclass
class AuthState:
    """一个登录态文件的基本情况（列表与日志里展示用）。"""

    name: str
    path: Path
    saved_at: Optional[float] = None      # 文件修改时间（≈ 最后一次保存）
    cookies: int = 0
    origins: int = 0                      # 有几个站点存了 localStorage
    expires_at: Optional[float] = None    # 最早的 cookie 过期时间；None＝都是会话级
    size: int = 0

    @property
    def exists(self) -> bool:
        return self.path.exists()


def auth_dir(project_dir) -> Path:
    return Path(project_dir) / AUTH_DIR_NAME


def safe_name(name: str) -> str:
    """把用户输入的名字清理成能当文件名的样子。"""
    text = "".join("_" if c in _BAD_CHARS else c for c in str(name or ""))
    return text.strip().strip(".")[:MAX_NAME_LEN] or DEFAULT_NAME


def state_path(project_dir, name: str) -> Path:
    return auth_dir(project_dir) / f"{safe_name(name)}.json"


def read_state(path) -> dict:
    """读登录态文件（坏文件当空处理）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def summarize(path) -> AuthState:
    """一个登录态文件的情况（cookie 数、最早过期时间…）。"""
    path = Path(path)
    state = AuthState(name=path.stem, path=path)
    if not path.exists():
        return state
    try:
        state.saved_at = path.stat().st_mtime
        state.size = path.stat().st_size
    except OSError:
        pass
    data = read_state(path)
    cookies = data.get("cookies") or []
    state.cookies = len(cookies)
    state.origins = len(data.get("origins") or [])
    stamps = [float(c.get("expires") or -1) for c in cookies
              if isinstance(c, dict)]
    future = [t for t in stamps if t > 0]
    state.expires_at = min(future) if future else None
    return state


def list_states(project_dir) -> List[AuthState]:
    """项目里的所有登录态，按最后保存时间从新到旧。"""
    folder = auth_dir(project_dir)
    if not folder.exists():
        return []
    items = [summarize(p) for p in sorted(folder.glob("*.json"))]
    return sorted(items, key=lambda s: s.saved_at or 0, reverse=True)


def delete_state(project_dir, name: str) -> bool:
    path = state_path(project_dir, name)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def rename_state(project_dir, old: str, new: str) -> Path:
    """改名；新名字已存在则直接覆盖（用户明确要这么干）。"""
    src, dst = state_path(project_dir, old), state_path(project_dir, new)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def import_state(project_dir, src, name: str) -> Path:
    """从外部文件导入一份登录态。"""
    dst = state_path(project_dir, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    return dst


def describe(state: AuthState) -> str:
    """一行中文摘要（日志与界面上用）。"""
    if not state.exists:
        return "还没保存过"
    bits = [f"保存于 {time.strftime('%m-%d %H:%M', time.localtime(state.saved_at))}"
            if state.saved_at else "保存时间未知",
            f"{state.cookies} 个 cookie"]
    if state.origins:
        bits.append(f"{state.origins} 个站点的 localStorage")
    if state.expires_at:
        bits.append("最早 " + time.strftime("%m-%d %H:%M",
                                            time.localtime(state.expires_at)) + " 过期")
    else:
        bits.append("都是会话级 cookie")
    return "，".join(bits)


def describe_path(path) -> str:
    return describe(summarize(path))
