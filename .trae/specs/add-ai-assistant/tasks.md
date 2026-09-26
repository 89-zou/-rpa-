# Tasks

- [ ] Task 0: 提交当前状态到 git（确保能回退）
  - [ ] SubTask 0.1: 在 `smart_tool/` 目录执行 `git add -A && git commit -m "AI 助手功能：开工前提交当前状态"`

- [ ] Task 1: 创建 AI 配置存储模块 (`core/ai/config.py`)
  - [ ] SubTask 1.1: 创建 `core/ai/__init__.py` 空包
  - [ ] SubTask 1.2: 实现 `load_ai_config()` / `save_ai_config()` 读写 `config.json` 的 `ai_config` 字段（复用 `paths.load_config`/`save_config`）
  - [ ] SubTask 1.3: 字段：`base_url`（默认 `https://api.openai.com/v1`）、`api_key`、`model`、`temperature`（默认 0.7）、`max_context_tokens`（默认 80000，超长触发 LLM 摘要压缩）、`keep_recent`（默认 6，压缩时保留最近几条不压缩）

- [ ] Task 2: 实现 OpenAI 兼容 LLM 客户端 (`core/ai/llm_client.py`)
  - [ ] SubTask 2.1: 用 `httpx` 实现 `chat(messages, tools, stream=True)` → 返回流式 chunks（text delta + tool_calls）
  - [ ] SubTask 2.2: 处理 SSE 流式解析（`data: {...}` 行 + `[DONE]` 结束标记）
  - [ ] SubTask 2.3: 非流式 `chat(messages, tools, stream=False)` → 返回完整 response（含 content + tool_calls）
  - [ ] SubTask 2.4: 错误处理：网络超时、HTTP 错误码、JSON 解析失败 → 抛出带中文说明的异常

- [ ] Task 3: 扩展 api.py — 浏览器交互工具 (`core/ai/browser_tools.py`)
  - [ ] SubTask 3.1: 维护一个模块级 Playwright 浏览器会话（`_browser_state`：playwright / browser / page），在独立线程中运行
  - [ ] SubTask 3.2: `ai_browser_open(url, project="")` → 打开可见浏览器、导航、返回页面标题+URL+交互元素摘要
  - [ ] SubTask 3.3: `ai_browser_get_structure()` → 注入 JS 提取页面交互元素（input/button/a/select/textarea 的标签、文本、属性、建议 XPath）
  - [ ] SubTask 3.4: `ai_browser_capture_element(prompt="")` → 注入 `element_picker.PICKER_JS`，等待用户点选，返回 XPath + 描述 + 命中数
  - [ ] SubTask 3.5: `ai_browser_screenshot(name="", project="")` → 截图保存到项目 `img/`
  - [ ] SubTask 3.6: `ai_browser_close()` → 关闭浏览器会话
  - [ ] SubTask 3.7: 在 `api.py` 中注册这 5 个工具到 `TOOLS` 和 `_register`

- [ ] Task 4: 扩展 api.py — 联网工具 (`core/ai/web_tools.py`)
  - [ ] SubTask 4.1: `ai_web_search(query, max_results=5)` → 用 httpx 请求 DuckDuckGo HTML (`https://html.duckduckgo.com/html/?q=...`)，解析结果（标题、链接、摘要）
  - [ ] SubTask 4.2: `ai_web_fetch(url)` → 用 httpx 抓取页面，用 `html.parser` 去标签转纯文本，保留链接
  - [ ] SubTask 4.3: 在 `api.py` 中注册这 2 个工具到 `TOOLS` 和 `_register`

- [ ] Task 5: 实现 Agent 核心 (`core/ai/agent.py`)
  - [ ] SubTask 5.1: 构建系统提示词：程序描述 + 边界限制 + 可用工具清单（从 `api.describe_tools()` 生成）+ 自由代码语法说明（`api.free_code_guide()`）
  - [ ] SubTask 5.2: Agent 循环：发消息 → 收到 tool_calls → 执行 `api.call_tool` → 回填结果 → 继续发消息，直到 AI 只返回文本
  - [ ] SubTask 5.3: 上下文压缩（LLM 摘要）：当近似 token 数（字符数/3.5）超过 `max_context_tokens` 时，将较早消息（保留最近 `keep_recent` 条不压缩）发送给 LLM 生成结构化摘要，用摘要替换旧消息。摘要 prompt 参考 Claude Code / Codex CLI 方案：保留用户需求、已完成操作、当前任务、关键变量/项目名/步骤、用户偏好
  - [ ] SubTask 5.4: 工具结果截断：工具调用返回结果超过 2000 字符时，在历史中只保留前 500 字符 + 「…（已截断）」
  - [ ] SubTask 5.5: 回调接口：`on_text(text)` 流式文本、`on_tool_start(name, args)`、`on_tool_result(name, result)`、`on_error(msg)`、`on_finished()`、`on_compact(summary)`（压缩时通知 UI）
  - [ ] SubTask 5.6: 边界限制：系统提示词中明确要求 AI 只回答和执行与小邹RPA 相关的内容

- [ ] Task 6: AI 设置对话框 (`ui/ai_settings_dialog.py`)
  - [ ] SubTask 6.1: QDialog 表单：base_url（QLineEdit）、api_key（QLineEdit password 模式）、model（QLineEdit）、temperature（QDoubleSpinBox 0~2）、max_context_tokens（QSpinBox 10000~200000）、keep_recent（QSpinBox 2~20）
  - [ ] SubTask 6.2: 预设几个常见 provider 快捷填入（OpenAI / DeepSeek / 通义千问 / Ollama 本地）
  - [ ] SubTask 6.3: 保存时调用 `config.save_ai_config()`，读取时调用 `config.load_ai_config()`

- [ ] Task 7: AI 帮助中心标签页 (`ui/ai_chat_tab.py`)
  - [ ] SubTask 7.1: 聊天界面：QTextBrowser 显示对话（用户靠右蓝色、AI 靠左灰色），QLineEdit + 发送按钮
  - [ ] SubTask 7.2: 顶部工具栏：设置按钮、模式切换按钮（帮助模式 / 创作模式）、清空对话按钮
  - [ ] SubTask 7.3: Agent 在 QThread 中运行，通过信号更新 UI（流式文本、工具调用状态、压缩通知、错误）
  - [ ] SubTask 7.4: 未配置 API key 时显示提示 + 设置按钮
  - [ ] SubTask 7.5: 帮助模式：Agent 可调用 `list_actions`、`describe_action`、`free_code_guide` 等只读工具
  - [ ] SubTask 7.6: 发送快捷键（Enter 发送 / Shift+Enter 换行）
  - [ ] SubTask 7.7: 上下文压缩时在聊天区域显示一条灰色提示（如「— 对话历史已压缩 —」）

- [ ] Task 8: 创作模式浮动聊天面板 (`ui/ai_chat_panel.py`)
  - [ ] SubTask 8.1: 浮动 QWidget（无父窗口边框、可拖拽、半透明背景），嵌入主页右侧或底部
  - [ ] SubTask 8.2: 聊天区域 + 输入框 + 关闭按钮
  - [ ] SubTask 8.3: Agent 在创作模式下可调用所有 api.py 工具（含浏览器交互、联网）
  - [ ] SubTask 8.4: 工具执行后发信号通知主页刷新画布（`project_changed` 信号）

- [ ] Task 9: 集成到 MainWindow (`ui/main_window.py`)
  - [ ] SubTask 9.1: 新增 `ai_tab = AIChatTab()` 并 `tabs.addTab(ai_tab, "AI帮助中心")`
  - [ ] SubTask 9.2: 创作模式联动：`ai_tab` 发 `enter_creation_mode` 信号 → 切到主页标签 → 显示浮动面板
  - [ ] SubTask 9.3: 浮动面板的 `project_changed` 信号 → 调用 `web_tab._load_project()` 刷新画布
  - [ ] SubTask 9.4: 浮动面板的 `exit_creation_mode` 信号 → 收起面板

- [ ] Task 10: 更新依赖与文档
  - [ ] SubTask 10.1: `requirements.txt` 新增 `httpx`
  - [ ] SubTask 10.2: 安装 httpx 到 `.venv`：`.venv\Scripts\python -m pip install httpx`
  - [ ] SubTask 10.3: 更新 `项目进度.md`：已实现节增加 AI 助手、待办勾掉 AI 助手项、变更记录新增本轮

# Task Dependencies
- [Task 0] 最先执行（git 提交）
- [Task 2] depends on [Task 1]
- [Task 3] 独立
- [Task 4] 独立
- [Task 5] depends on [Task 2, 3, 4]
- [Task 6] depends on [Task 1]
- [Task 7] depends on [Task 5, 6]
- [Task 8] depends on [Task 5, 7]
- [Task 9] depends on [Task 7, 8]
- [Task 10] depends on [Task 9]
