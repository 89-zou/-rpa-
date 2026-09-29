# -*- coding: utf-8 -*-
"""页面内 AI Agent（官方 page-agent）的接入层。

一句话说清分工：
· **页面里**跑 page-agent：读页面（脱水 DOM 树）→ 想下一步 → 决定点哪个元素 / 填什么；
· **本进程**干两件页面干不了的事：真实鼠标键盘（CDP 输入，isTrusted=true）、调 LLM（Key 不进页面）。

对外就这几样：
· `run_agent_task(executor, step)`  —— 「任务」节点
· `bundle_ready()` / `missing_hint()` —— 页面内产物在不在（没构建时给人话提示）
· `PageAgentBridge`                 —— 需要自己控循环时用
"""
from rpa_page_agent.core.page_agent.bridge import (      # noqa: F401
    PageAgentBridge, PageAgentError, PageAgentStopped, bundle_ready,
    missing_hint, run_agent_task,
)

__all__ = [
    "PageAgentBridge", "PageAgentError", "PageAgentStopped",
    "bundle_ready", "missing_hint", "run_agent_task",
]