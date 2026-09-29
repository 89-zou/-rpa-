# -*- coding: utf-8 -*-
"""在桌面端进程里调 LLM（OpenAI 兼容的 /chat/completions），密钥不进页面。

为什么用标准库 urllib：这个项目没装 httpx / requests；而且我们只是「原样转发」
页面传来的请求体（messages / tools 都由 PageAgent 自己组织好），一个 POST 就够。

兼容性来自「透传」：通义（百炼）、DeepSeek、OpenAI、本机 Ollama / LM Studio
都是 OpenAI 兼容接口，只要 base_url + model（+ 需要时的 key）填对就能用。
"""
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple

#: 失败重试次数（网络抖动 / 5xx / 限流才重试；4xx 直接报出来，重试没意义）
RETRY = 2
DEFAULT_TIMEOUT_S = 120


class LLMError(RuntimeError):
    """调 LLM 出错（配置缺失、网络不通、返回不正常）。"""


def _endpoint(cfg: Dict[str, Any]) -> str:
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMError("还没填 LLM 接口地址")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _snippet(text: str, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def chat_completions(cfg: Dict[str, Any], body_json: str,
                     timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    """把页面传来的请求体转发给 LLM，返回响应原文（JSON 字符串）。

    · 模型名以项目配置为准（页面里传的是占位值）；
    · api_key 有才带 Authorization（本机模型不用）；
    · 失败重试 RETRY 次（0.8s / 1.6s 退避），仍失败抛 LLMError（带状态码与响应片段）。
    """
    url = _endpoint(cfg)
    key = str(cfg.get("api_key") or "").strip()
    body = body_json if isinstance(body_json, str) else json.dumps(body_json)
    try:                                  # 统一模型名、去掉 stream（我们要整包 JSON）
        data = json.loads(body)
        if isinstance(data, dict):
            if cfg.get("model"):
                data["model"] = str(cfg["model"])
            data.pop("stream", None)
            body = json.dumps(data, ensure_ascii=False)
    except Exception:
        pass                              # 不是 JSON 就原样发（少见，交给服务端报错）
    raw = body.encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last = ""
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = _snippet(e.read().decode("utf-8", "replace"))
            except Exception:
                pass
            last = f"HTTP {e.code}：{detail or e.reason}"
            if e.code < 500 and e.code not in (408, 429):
                break                     # Key 不对 / 模型不存在 → 重试也没用
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < RETRY:
            time.sleep(0.8 * (2 ** attempt))
    raise LLMError(f"调 LLM 失败（{url}）：{last}")


def ping(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """给编辑器里的【试一下】按钮用：发一次最小请求，返回 (通不通, 说明)。"""
    body = {"model": str(cfg.get("model") or ""),
            "messages": [{"role": "user", "content": "只回一个字：好"}],
            "max_tokens": 8}
    try:
        text = chat_completions(cfg, json.dumps(body, ensure_ascii=False), timeout_s=30)
    except Exception as e:
        return False, str(e)
    try:
        data = json.loads(text)
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        return True, f"接口通了：{content[:40] or '（模型没回内容，但接口是通的）'}"
    except Exception:
        return False, f"返回的不是合法 JSON：{_snippet(text)}"