# -*- coding: utf-8 -*-
"""AI 配置：**只用 DeepSeek，用户只需要填一个 API 密钥。**

接口地址和模型都写死在这里 —— 用户不用管、也填不错：

    · 开放平台（注册 / 拿 Key）：https://platform.deepseek.com/
    · 接口地址：https://api.deepseek.com/v1（OpenAI 兼容）
    · 模型：deepseek-v4-flash

密钥存在本机数据目录的 `ai_settings.json`（跟 projects/ 放一起，绿色版拷走跟着走），
界面上的入口是主窗口的【AI 设置】标签页（ui/ai_settings_tab.py）。

密钥不进页面：页面内跑的是 page-agent，它发 LLM 请求时会走 customFetch 交回
本进程（见 page_agent_glue.js），所以 Key 永远不进网页、不进运行日志。
"""
import json
from pathlib import Path
from typing import Any, Dict

from rpa_page_agent import paths
from rpa_page_agent.core.project_store import ProjectStore

#: 服务商（只有这一家）
PROVIDER_NAME = "DeepSeek"
#: 注册 / 拿 Key 的页面
PROVIDER_HOME = "https://platform.deepseek.com/"
#: 接口地址（OpenAI 兼容）
BASE_URL = "https://api.deepseek.com/v1"
#: 用的模型
MODEL = "deepseek-v4-flash"

#: 配置文件放在**数据目录**（用户只要填一个 Key，其余都是常量）
SETTINGS_NAME = "ai_settings.json"

DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT_S = 180


def settings_file() -> Path:
    """AI 设置文件的位置。"""
    return Path(paths.DATA_DIR) / SETTINGS_NAME


def load_key() -> str:
    """读本机存的 API 密钥（没有/文件坏了都当空，不抛异常）。"""
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(data.get("api_key") or "").strip() if isinstance(data, dict) else ""


def save_key(api_key: str) -> None:
    """把密钥写回本机（别的键原样保留）。"""
    data: Dict[str, Any] = {}
    try:
        raw = json.loads(settings_file().read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        pass
    data["api_key"] = str(api_key or "").strip()
    path = settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass                        # 写不进去也别让界面崩，用户重开再填


def _project_defaults(project_dir) -> Dict[str, Any]:
    """项目里跟「任务」节点有关的默认值（语言 / 步数 / 超时）。"""
    if not project_dir:
        return {}
    try:
        return ProjectStore(Path(project_dir)).load_page_agent()
    except Exception:
        return {}


def load(project_dir=None) -> Dict[str, Any]:
    """当前生效的 AI 配置：接口地址和模型是常量，只有密钥是用户填的。"""
    project = _project_defaults(project_dir)
    return {
        "base_url": BASE_URL,
        "model": MODEL,
        "api_key": load_key(),
        "provider": PROVIDER_NAME,
        "language": str(project.get("language") or DEFAULT_LANGUAGE),
        "max_steps": int(project.get("max_steps") or DEFAULT_MAX_STEPS),
        "timeout_s": int(project.get("timeout_s") or DEFAULT_TIMEOUT_S),
    }


def problem(cfg: Dict[str, Any]) -> str:
    """配置能不能用：空串＝可以，否则是一句人话（显示在运行日志/编辑器里）。"""
    if not str(cfg.get("api_key") or "").strip():
        return ("还没填 DeepSeek 的 API 密钥：主窗口【AI 设置】标签页里粘一下就行"
                "（没有 Key 去 " + PROVIDER_HOME + " 注册）")
    return ""


def describe(cfg: Dict[str, Any]) -> str:
    """一句话说清「现在用的是谁、哪个模型」。"""
    return f"{PROVIDER_NAME} ｜ {MODEL}"
