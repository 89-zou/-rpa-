# Checklist

* [ ] 开工前已 git commit 当前状态（可回退）

* [ ] `core/ai/__init__.py` 包存在

* [ ] `core/ai/config.py` 能读写 `config.json` 的 `ai_config` 字段（base\_url / api\_key / model / temperature / max\_context\_tokens / keep\_recent）

* [ ] `core/ai/llm_client.py` 能通过 OpenAI 兼容 `/v1/chat/completions` 发送请求并解析流式/非流式响应

* [ ] LLM 客户端处理网络错误和 HTTP 错误码，给出中文错误提示且不崩溃

* [ ] `core/ai/browser_tools.py` 的 5 个工具（ai\_browser\_open / get\_structure / capture\_element / screenshot / close）功能正常

* [ ] `ai_browser_open` 打开可见浏览器并返回页面标题 + 交互元素摘要

* [ ] `ai_browser_get_structure` 返回页面交互元素的标签、文本、属性、建议 XPath

* [ ] `ai_browser_capture_element` 复用 `element_picker.PICKER_JS`，用户点选后返回 XPath

* [ ] `core/ai/web_tools.py` 的 2 个工具（ai\_web\_search / ai\_web\_fetch）功能正常

* [ ] `ai_web_search` 通过 DuckDuckGo 返回标题+链接+摘要

* [ ] `ai_web_fetch` 返回去标签的纯文本

* [ ] `api.py` 的 `TOOLS` 字典新增了全部 7 个工具，`describe_tools()` 和 `call_tool()` 能覆盖它们

* [ ] `core/ai/agent.py` 的系统提示词包含程序描述、边界限制、工具清单、自由代码语法

* [ ] Agent 循环正确执行 tool\_calls 并回填结果，直到 AI 返回纯文本

* [ ] 上下文压缩：token 数超阈值时用 LLM 生成结构化摘要替换旧消息（不丢弃，而是压缩）

* [ ] 摘要保留：用户需求、已完成操作、当前任务、关键变量/项目名/步骤、用户偏好

* [ ] 工具结果超 2000 字符时在历史中截断为前 500 字符

* [ ] Agent 回调接口（on\_text / on\_tool\_start / on\_tool\_result / on\_error / on\_finished / on\_compact）正常工作

* [ ] 边界限制：AI 拒绝回答/执行与程序无关的内容

* [ ] `ui/ai_settings_dialog.py` 能配置 base\_url / api\_key / model / temperature / max\_context\_tokens / keep\_recent 并保存

* [ ] 设置对话框有常用 provider 快捷预设（OpenAI / DeepSeek / 通义千问 / Ollama）

* [ ] api\_key 在输入框中以密码模式显示

* [ ] `ui/ai_chat_tab.py` 聊天界面正常显示用户和 AI 消息（流式输出）

* [ ] 未配置 API key 时显示提示和设置按钮

* [ ] 帮助模式可调用只读工具（list\_actions / describe\_action / free\_code\_guide）

* [ ] Enter 发送、Shift+Enter 换行

* [ ] 上下文压缩时聊天区域显示灰色提示（如「— 对话历史已压缩 —」）

* [ ] `ui/ai_chat_panel.py` 浮动面板可显示在主页上

* [ ] 创作模式可调用所有 api.py 工具（含浏览器交互和联网）

* [ ] 工具执行后主页画布自动刷新

* [ ] `ui/main_window.py` 新增「AI帮助中心」标签页

* [ ] 创作模式切换：点击按钮 → 切到主页 → 显示浮动面板

* [ ] 退出创作模式：浮动面板收起，回到普通编辑模式

* [ ] `requirements.txt` 新增 `httpx` 且已安装到 `.venv`

* [ ] `python -m compileall -q smart_tool` 0 错误

* [ ] 现有功能不受影响：画布编排、流程编辑、运行/停止、项目管理正常

* [ ] `项目进度.md` 已更新（已实现、待办、变更记录）

