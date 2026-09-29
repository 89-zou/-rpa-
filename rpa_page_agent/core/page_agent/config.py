# -*- coding: utf-8 -*-
"""智能页面任务用的 LLM 配置：存在**项目里**（steps.json 的 page_agent 键），所有智能节点共用。

密钥只留在本地项目文件里 —— 页面内跑的是 page-agent，它发 LLM 请求时会走
customFetch 交回 Python 进程（见 page_agent_glue.js），所以 Key 永远不进页面。
"""
from pathlib import Path
from typing import Any, Dict, Tuple

from rpa_page_agent.core.project_store import ProjectStore

#: 常用服务商预设：(名字, base_url, 推荐模型)。用户点一下就填好，填错也好改。
PRESETS: Tuple[Tuple[str, str, str], ...] = (
    ("通义千问（阿里云百炼）", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen3.5-plus"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-v4-flash"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-5.4-mini"),
    ("本机 Ollama", "http://localhost:11434/v1", "qwen3:14b"),
    ("本机 LM Studio", "http://127.0.0.1:1234/v1", "qwen/qwen3.5-27b"),
)

DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT_S = 180


def load(project_dir) -> Dict[str, Any]:
    """读项目的智能配置；没配过时给一份「预设模板」，方便直接在界面上改。"""
    cfg = ProjectStore(Path(project_dir)).load_page_agent()
    if not cfg.get("base_url") or not cfg.get("model"):
        _name, base, model = PRESETS[0]
        cfg["base_url"] = cfg.get("base_url") or base
        cfg["model"] = cfg.get("model") or model
    cfg["language"] = cfg.get("language") or DEFAULT_LANGUAGE
    cfg["max_steps"] = int(cfg.get("max_steps") or DEFAULT_MAX_STEPS)
    cfg["timeout_s"] = int(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
    return cfg


def save(project_dir, base_url: str = "", api_key: str = "", model: str = "",
         language: str = DEFAULT_LANGUAGE, max_steps: int = DEFAULT_MAX_STEPS,
         timeout_s: int = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """写回项目（不动别的键），返回写进去的那份配置。"""
    store = ProjectStore(Path(project_dir))
    store.save_page_agent(base_url=base_url, api_key=api_key, model=model,
                          language=language, max_steps=max_steps,
                          timeout_s=timeout_s)
    return store.load_page_agent()


def is_local(base_url: str) -> bool:
    """是不是本机地址（本机模型不需要 Key）。"""
    url = str(base_url or "").lower()
    return "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url


def problem(cfg: Dict[str, Any]) -> str:
    """配置能不能用：空串＝可以，否则是一句人话（显示在运行日志/编辑器里）。"""
    if not str(cfg.get("base_url") or "").strip():
        return "还没填 LLM 接口地址（双击智能节点，在「LLM 设置」里填）"
    if not str(cfg.get("model") or "").strip():
        return "还没填模型名（如 qwen3.5-plus / deepseek-v4-flash）"
    if not str(cfg.get("api_key") or "").strip() and not is_local(cfg.get("base_url")):
        return "还没填 API 密钥（双击智能节点，在「LLM 设置」里填；本机模型不用填）"
    return ""