# -*- coding: utf-8 -*-
"""AI（大模型）配置：**整个程序共用一份**，存在数据目录的 `ai_settings.json` 里。

为什么不再存在项目里：一个 Key 就是给整个程序用的，挨个项目填一遍太烦。
界面上的入口是主窗口的【AI 设置】标签页（ui/ai_settings_tab.py）。

密钥只留在本机这个文件里 —— 页面内跑的是 page-agent，它发 LLM 请求时会走
customFetch 交回 Python 进程（见 page_agent_glue.js），所以 Key 永远不进页面、
不进运行日志。

老版本把 base_url / api_key / model 存在项目 steps.json 的 page_agent 键里，
`load()` 读到这种老项目会**自动搬到全局**（搬一次，之后以全局为准）。
"""
import json
from pathlib import Path
from typing import Any, Dict

from rpa_page_agent import paths
from rpa_page_agent.core.project_store import ProjectStore

#: 配置文件放在**数据目录**（跟 projects/ 一起，绿色版整个文件夹拷走就跟着走）
SETTINGS_NAME = "ai_settings.json"

DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT_S = 180


def settings_file() -> Path:
    """AI 设置文件的位置。"""
    return Path(paths.DATA_DIR) / SETTINGS_NAME


def load_global() -> Dict[str, Any]:
    """读本机的 AI 设置（没有/坏了都当空配置，不抛异常）。"""
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_global(base_url: str = "", api_key: str = "", model: str = "",
                provider: str = "") -> Dict[str, Any]:
    """写回本机的 AI 设置，返回写进去的那份。"""
    data = load_global()
    data.update({
        "base_url": str(base_url or "").strip(),
        "api_key": str(api_key or "").strip(),
        "model": str(model or "").strip(),
        "provider": str(provider or "").strip(),
    })
    path = settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass                        # 写不进去也别让界面崩，用户重开再填
    return data


def _project_defaults(project_dir) -> Dict[str, Any]:
    """项目里跟「任务」节点有关的默认值（语言 / 步数 / 超时）。"""
    if not project_dir:
        return {}
    try:
        return ProjectStore(Path(project_dir)).load_page_agent()
    except Exception:
        return {}


def load(project_dir=None) -> Dict[str, Any]:
    """合成一份「当前生效的 AI 配置」：全局的接口信息 + 项目的默认步数/超时。"""
    cfg = load_global()
    project = _project_defaults(project_dir)

    # 老项目里自己存过接口信息 → 搬到全局（只搬一次），免得用户发现「我配的没了」
    if (not cfg.get("base_url")) and project.get("base_url"):
        cfg = save_global(project.get("base_url", ""), project.get("api_key", ""),
                          project.get("model", ""), "")

    return {
        "base_url": str(cfg.get("base_url") or ""),
        "api_key": str(cfg.get("api_key") or ""),
        "model": str(cfg.get("model") or ""),
        "provider": str(cfg.get("provider") or ""),
        "language": str(project.get("language") or DEFAULT_LANGUAGE),
        "max_steps": int(project.get("max_steps") or DEFAULT_MAX_STEPS),
        "timeout_s": int(project.get("timeout_s") or DEFAULT_TIMEOUT_S),
    }


def is_local(base_url: str) -> bool:
    """是不是本机地址（本机模型不需要 Key）。"""
    url = str(base_url or "").lower()
    return "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url


def problem(cfg: Dict[str, Any]) -> str:
    """配置能不能用：空串＝可以，否则是一句人话（显示在运行日志/编辑器里）。"""
    if not str(cfg.get("base_url") or "").strip():
        return "还没配 AI：打开【AI 设置】标签页，把 API 密钥粘进去就行"
    if not str(cfg.get("model") or "").strip():
        return "还没选模型（打开【AI 设置】标签页，识别完在模型下拉里选一个）"
    if not str(cfg.get("api_key") or "").strip() and not is_local(cfg.get("base_url")):
        return "还没填 API 密钥（打开【AI 设置】标签页填；本机模型不用填）"
    return ""


def describe(cfg: Dict[str, Any]) -> str:
    """一句话说清「现在用的是谁、哪个模型」。"""
    if problem(cfg):
        return problem(cfg)
    who = str(cfg.get("provider") or "").strip() or str(cfg.get("base_url") or "")
    return f"{who} ｜ {cfg.get('model')}"


def provider_name(cfg: Dict[str, Any]) -> str:
    """当前配置的服务商名（没记就用接口地址兜底）。"""
    return str(cfg.get("provider") or cfg.get("base_url") or "").strip()
