# -*- coding: utf-8 -*-
"""AI 服务商清单 + 「只粘一个 Key 就认出是哪家」的自动识别。

为什么要识别：用户手里只有一个 API 密钥，让他自己去查「我这家该填哪个接口地址、
模型叫什么」太劝退。这里的做法是 —— 拿这个 Key 挨家去问一句
`GET {base_url}/models`，谁回 200 就是谁，顺手把那家**真实可用的模型名**抄回来
（这样界面上列出的模型名一定是真的，不用我们凭空猜）。

几点约定：

· 全部走 **OpenAI 兼容**接口（`/chat/completions`）：page-agent 只会说这一种话；
· 不在列表里的服务商也不影响用 —— 界面上「接口地址 / 模型」都能手填；
· 探测是**并发**的（20 家串行要等一分钟），先按 Key 前缀把最像的排前面，
  谁先成功用谁；都不成功就把每家的错误摊给用户看。

清单里每条：
    name     显示名
    base_url OpenAI 兼容接口地址（填到配置里的那个，末尾不带 /）
    signup   去这家**拿 Key 的页面**（点一下就用系统浏览器打开）
    home     官网 / 控制台首页
    prefix   Key 的固定开头（用来把探测顺序排前面；空元组＝没有特征）
    local    是不是本机模型（不用 Key，探测时单独处理）
    note     需要提醒用户的一句话（可空）
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 探测超时（秒）。国内直连国外的服务商大多会一直挂着，所以给短一点。
PROBE_TIMEOUT_S = 6
#: 并发探测的线程数
MAX_WORKERS = 10

PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "signup": "https://bailian.console.aliyun.com/",
        "home": "https://bailian.console.aliyun.com/",
        "prefix": ("sk-",),
    },
    {
        "name": "DeepSeek（深度求索）",
        "base_url": "https://api.deepseek.com/v1",
        "signup": "https://platform.deepseek.com/api_keys",
        "home": "https://platform.deepseek.com/",
        "prefix": ("sk-",),
    },
    {
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "signup": "https://platform.moonshot.cn/console/api-keys",
        "home": "https://platform.moonshot.cn/",
        "prefix": ("sk-",),
    },
    {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "signup": "https://open.bigmodel.cn/usercenter/apikeys",
        "home": "https://open.bigmodel.cn/",
        "prefix": (),
    },
    {
        "name": "火山引擎方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "signup": "https://ark.volcengine.com/region:cn-beijing/apikey",
        "home": "https://console.volcengine.com/ark",
        "prefix": (),
        "note": "要在控制台「开通管理」里开通模型，模型名（Model ID）也照控制台上的填",
    },
    {
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "signup": "https://cloud.siliconflow.cn/account/ak",
        "home": "https://cloud.siliconflow.cn/",
        "prefix": ("sk-",),
    },
    {
        "name": "百度千帆（文心）",
        "base_url": "https://qianfan.baidubce.com/v2",
        "signup": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
        "home": "https://console.bce.baidu.com/qianfan/",
        "prefix": ("bce-v3/",),
    },
    {
        "name": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "signup": "https://console.cloud.tencent.com/hunyuan/api-key",
        "home": "https://console.cloud.tencent.com/hunyuan",
        "prefix": ("sk-",),
    },
    {
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "signup": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "home": "https://platform.minimaxi.com/",
        "prefix": (),
    },
    {
        "name": "阶跃星辰 StepFun",
        "base_url": "https://api.stepfun.com/v1",
        "signup": "https://platform.stepfun.com/interface-key",
        "home": "https://platform.stepfun.com/",
        "prefix": (),
    },
    {
        "name": "百川智能",
        "base_url": "https://api.baichuan-ai.com/v1",
        "signup": "https://platform.baichuan-ai.com/console/apikey",
        "home": "https://platform.baichuan-ai.com/",
        "prefix": (),
    },
    {
        "name": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "signup": "https://console.xfyun.cn/services/cbm",
        "home": "https://xinghuo.xfyun.cn/",
        "prefix": (),
        "note": "密钥要填「APIKey:APISecret」两段拼起来（中间一个冒号）",
    },
    {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "signup": "https://platform.openai.com/api-keys",
        "home": "https://platform.openai.com/",
        "prefix": ("sk-proj-", "sk-"),
        "note": "国内直连不通，一般要配合代理",
    },
    {
        "name": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "signup": "https://console.anthropic.com/settings/keys",
        "home": "https://console.anthropic.com/",
        "prefix": ("sk-ant-",),
        "note": "国内直连不通，一般要配合代理",
    },
    {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "signup": "https://aistudio.google.com/apikey",
        "home": "https://aistudio.google.com/",
        "prefix": ("AIza",),
        "note": "国内直连不通，一般要配合代理",
    },
    {
        "name": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "signup": "https://console.x.ai/",
        "home": "https://console.x.ai/",
        "prefix": ("xai-",),
    },
    {
        "name": "OpenRouter（一家通吃）",
        "base_url": "https://openrouter.ai/api/v1",
        "signup": "https://openrouter.ai/keys",
        "home": "https://openrouter.ai/",
        "prefix": ("sk-or-",),
        "note": "一个 Key 能用几百个模型（含 GPT / Claude / Gemini）",
        # 这家的 /models 是公开的（不带 Key 也能看），所以「列得出模型」说明不了
        # Key 是这家的 —— 识别到它时要再发一次真请求确认（见 _confirm_chat）
        "public_models": True,
    },
    {
        "name": "Groq（快）",
        "base_url": "https://api.groq.com/openai/v1",
        "signup": "https://console.groq.com/keys",
        "home": "https://console.groq.com/",
        "prefix": ("gsk_",),
    },
    {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "signup": "https://console.mistral.ai/api-keys",
        "home": "https://console.mistral.ai/",
        "prefix": (),
    },
    {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "signup": "https://api.together.ai/settings/api-keys",
        "home": "https://api.together.ai/",
        "prefix": (),
    },
    {
        "name": "本机 Ollama",
        "base_url": "http://localhost:11434/v1",
        "signup": "https://ollama.com/download",
        "home": "https://ollama.com/",
        "prefix": (),
        "local": True,
        "note": "不用 Key；装了 Ollama 并拉过模型才探测得到",
    },
    {
        "name": "本机 LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "signup": "https://lmstudio.ai/",
        "home": "https://lmstudio.ai/",
        "prefix": (),
        "local": True,
        "note": "不用 Key；要在 LM Studio 里把本地服务打开",
    },
)

#: 识别失败时给用户看的一句话
MANUAL_HINT = ("没认出来是哪一家。可以自己填「接口地址」和「模型」——"
               "现在各家基本都是 OpenAI 兼容接口，"
               "在下面表格里点一行的【拿去注册】就能看到自己的接口地址。")


def all_providers() -> Tuple[Dict[str, Any], ...]:
    """全部服务商（界面上的那张表用它）。"""
    return PROVIDERS


def provider_by_base(base_url: str) -> Optional[Dict[str, Any]]:
    """按接口地址反查服务商（界面上显示「当前用的是谁」）。"""
    want = str(base_url or "").strip().rstrip("/").lower()
    for p in PROVIDERS:
        if p["base_url"].lower() == want:
            return p
    return None


def guess_order(api_key: str) -> List[Dict[str, Any]]:
    """按 Key 的特征排一下探测顺序：开头对得上的排前面。

    Key 为空（本机模型）时只探测本机那两家，免得拿着空 Key 去满世界敲别人家门。
    """
    key = str(api_key or "").strip()
    if not key:
        return [p for p in PROVIDERS if p.get("local")]

    def matched(prov) -> int:
        """Key 命中这家的哪个前缀；命中得越长越可信（sk-ant- 比 sk- 精确）。"""
        return max((len(pre) for pre in (prov.get("prefix") or ())
                    if key.startswith(pre)), default=0)

    hit, rest = [], []
    for p in PROVIDERS:
        if p.get("local"):
            continue                       # 本机模型不需要 Key，也不参与识别
        (hit if matched(p) else rest).append(p)
    hit.sort(key=matched, reverse=True)
    return hit + rest


def models_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/") + "/models"


def chat_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/") + "/chat/completions"


def _http(url: str, api_key: str, timeout: float,
          data: Optional[bytes] = None) -> Tuple[int, str]:
    """发一个请求，返回 (状态码, 响应原文)。状态码 0＝连都没连上。"""
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        # Anthropic 走 x-api-key；别的家不认这个头也只会忽略，不会因此报错
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _snippet(text: str, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


def _pick_models(text: str) -> List[str]:
    """从 /models 的响应里把模型名都抠出来（各家格式略有差别，都认一下）。"""
    try:
        data = json.loads(text)
    except Exception:
        return []
    rows = []
    if isinstance(data, dict):
        for key in ("data", "models", "result"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    elif isinstance(data, list):
        rows = data
    out: List[str] = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            name = row.get("id") or row.get("name") or row.get("model")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    # 去重但保持服务端的顺序
    seen, uniq = set(), []
    for name in out:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    return uniq


def probe(provider: Dict[str, Any], api_key: str,
          timeout: float = PROBE_TIMEOUT_S) -> Tuple[bool, List[str], str]:
    """拿 Key 问一家：`GET /models` 通不通。返回 (通不通, 模型列表, 说明)。"""
    url = models_url(provider["base_url"])
    code, text = _http(url, api_key, timeout)
    if code == 200:
        models = _pick_models(text)
        if models:
            return True, models, "好"
        return False, [], "接口通了，但没列出模型名（可能这家不支持 /models）"
    if code == 0:
        return False, [], text
    if code in (401, 403):
        return False, [], f"HTTP {code}（Key 不是这家的）"
    return False, [], f"HTTP {code}：{_snippet(text)}"


#: 挑「试调用」模型时的偏好（越靠前越像能聊天的主力模型）
_CHAT_HINTS = ("gpt-4", "gpt-5", "claude", "gemini", "qwen", "deepseek",
               "llama", "grok", "glm", "kimi", "mistral")
#: 一看就不是「聊天」的模型名
_NOT_CHAT = ("embed", "rerank", "audio", "image", "tts", "whisper",
             "moderation", "speech", "video", "ocr")


def _chat_candidates(models: List[str], limit: int = 3) -> List[str]:
    """从模型列表里挑几个「最像能聊天」的来试（列表可能有几百个、顺序也乱）。"""
    def score(name: str) -> int:
        low = name.lower()
        s = 0
        for i, hint in enumerate(_CHAT_HINTS):
            if hint in low:
                s = max(s, 100 - i * 5)
        if any(bad in low for bad in _NOT_CHAT):
            s -= 100
        if ":" in low:                    # OpenRouter 的 :batch / :free 这类变体先跳过
            s -= 30
        return -s

    return sorted(models, key=score)[:limit]


def _confirm_chat(provider: Dict[str, Any], api_key: str, models: List[str],
                  timeout: float) -> Tuple[bool, str, str]:
    """真发一次最小对话请求，确认「这个 Key 真的能用」+「这个模型真的能调」。

    只在 /models 是公开的那些服务商上用（现在只有 OpenRouter）—— 不然拿到一个
    无效的 Key 也可能因为「模型列表人人可见」而被误认成是这家的。

    返回 (通不通, 说明, 试通的模型名)。
    """
    tried: List[str] = []
    for model in _chat_candidates(models):
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1, "stream": False}
        code, text = _http(chat_url(provider["base_url"]), api_key, timeout,
                           data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if code == 200:
            return True, f"{model} 试调成功", model
        tried.append(f"{model} → {code or '连不上'}")
        if code in (401, 403):            # Key 不对，换模型也没用
            break
    return False, "；".join(tried[:2]) or "没有可试的模型", ""


def identify(api_key: str, timeout: float = PROBE_TIMEOUT_S,
             on_progress: Optional[Callable[[str], None]] = None
             ) -> Dict[str, Any]:
    """粘一个 Key，挨家问一遍，认出是哪家 + 抄回真实模型名。

    返回：
        {"ok": bool, "provider": {...} | None, "models": [...],
         "tried": [(服务商名, 说明), ...], "error": "..."}
    """
    key = str(api_key or "").strip()
    order = guess_order(key)
    if not order:
        return {"ok": False, "provider": None, "models": [], "tried": [],
                "error": "还没填 API 密钥"}
    if on_progress:
        on_progress(f"正在识别（{len(order)} 家并发试探）…")

    def run(p):
        ok, models, why = probe(p, key, timeout)
        return p, ok, models, why

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(order))) as pool:
        results = list(pool.map(run, order))

    # 按探测顺序挑第一个成功的（顺序＝前缀特征的匹配程度）
    extra: Dict[str, str] = {}
    for p, ok, models, why in results:
        if not ok:
            continue
        if p.get("public_models"):
            # 这家的模型列表人人可见，得真调一次才算数
            good, note, used = _confirm_chat(p, key, models, timeout)
            extra[p["name"]] = f"列表公开，真调一次：{note}"
            if not good:
                continue
            # 把「试通过的那个」排最前：界面上默认就选它，省得用户挑花眼
            models = [used] + [m for m in models if m != used]
        return {"ok": True, "provider": p, "models": models,
                "tried": [(p2["name"], extra.get(p2["name"], why2))
                          for p2, _o, _m, why2 in results],
                "error": ""}
    tried = [(p["name"], extra.get(p["name"], why))
             for p, _ok, _m, why in results]
    return {"ok": False, "provider": None, "models": [], "tried": tried,
            "error": MANUAL_HINT}


def fetch_models(base_url: str, api_key: str = "",
                 timeout: float = PROBE_TIMEOUT_S) -> Tuple[List[str], str]:
    """只查某个地址有哪些模型（用户手填了接口地址时用）。返回 (模型列表, 说明)。"""
    ok, models, why = probe({"base_url": base_url}, api_key, timeout)
    return (models, "好") if ok else ([], why)
