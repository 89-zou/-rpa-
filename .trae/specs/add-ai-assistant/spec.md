# AI 助手功能 Spec

## Why

项目已预留了 36 个工具的 API 接口层（`core/api.py`），但尚未接入任何 AI 能力。用户需要在程序内获得 AI 帮助：解答功能用法、辅助编写自由代码节点、通过自然语言描述自动创建自动化项目。本变更在主页旁新增「AI 帮助中心」标签页，并提供帮助模式和创作模式两种交互方式。

## What Changes

### 新增
- **「AI 帮助中心」标签页**：在 `MainWindow` 的 `QTabWidget` 中新增第二个标签，内含聊天界面
- **OpenAI 兼容 LLM 客户端**：用户自填 `base_url` + `api_key` + `model`，通过 `/v1/chat/completions` 调用，支持流式输出和 function calling
- **Agent 核心**：系统提示词（含程序能力清单 + 边界限制）、工具调用循环、上下文管理（对话历史 + 超长截断）
- **帮助模式**：聊天问答——用户提问程序用法、请求帮助编写自由代码、讨论流程设计思路
- **创作模式**：切回主页 + 浮动聊天框——AI 通过 `api.py` 工具直接创建/编辑项目（建项目、加步骤、设变量等），支持一次性完成或逐步交互
- **浏览器交互工具**（扩展 `api.py`）：打开页面、获取页面结构摘要、触发元素捕获、截图
- **联网工具**（扩展 `api.py`）：网络搜索（DuckDuckGo HTML）、网页抓取（HTTP + HTML 转文本）
- **AI 设置对话框**：配置 API key / base_url / model / temperature / max_context_tokens（默认 80000，超长触发 LLM 摘要压缩）/ keep_recent（默认 6，压缩时保留最近几条不压缩），存储在 `%APPDATA%\小邹RPA\config.json` 中
- **依赖**：`requirements.txt` 新增 `httpx`（LLM API 调用 + 网页抓取）

### 修改
- `ui/main_window.py`：新增 AI 帮助中心标签页 + 创作模式浮动面板
- `core/api.py`：注册新增的浏览器交互工具和联网工具
- `paths.py`：新增 `ai_config` 读写辅助（复用 `load_config`/`save_config`）
- `requirements.txt`：新增 `httpx`

## Impact
- Affected code: `ui/main_window.py`, `core/api.py`, `paths.py`, `requirements.txt`
- New files: `core/ai/__init__.py`, `core/ai/config.py`, `core/ai/llm_client.py`, `core/ai/agent.py`, `core/ai/browser_tools.py`, `core/ai/web_tools.py`, `ui/ai_chat_tab.py`, `ui/ai_settings_dialog.py`, `ui/ai_chat_panel.py`
- 现有功能不受影响：AI 功能是纯增量，不改动现有流程编排、执行、画布等逻辑

## ADDED Requirements

### Requirement: AI 帮助中心标签页
系统 SHALL 在主页旁新增一个名为「AI 帮助中心」的标签页，提供聊天界面供用户与 AI 交互。

#### Scenario: 首次打开 AI 帮助中心
- **WHEN** 用户点击「AI 帮助中心」标签
- **AND** 尚未配置 API key
- **THEN** 聊天区域显示提示「请先配置 AI 设置」
- **AND** 提供设置按钮打开 AI 设置对话框

#### Scenario: 已配置后正常聊天
- **WHEN** 用户已配置 API key 并输入消息
- **THEN** 消息显示在聊天区域（用户消息靠右、AI 回复靠左）
- **AND** AI 回复以流式方式逐步显示
- **AND** AI 思考/调用工具期间显示状态提示

### Requirement: OpenAI 兼容 LLM 客户端
系统 SHALL 提供一个 OpenAI 兼容的 LLM 客户端，用户通过设置对话框配置连接参数。

#### Scenario: 配置 API 参数
- **WHEN** 用户在设置对话框中填写 base_url、api_key、model 并保存
- **THEN** 配置存储到 `%APPDATA%\小邹RPA\config.json` 的 `ai_config` 字段
- **AND** 后续 LLM 调用使用这些参数

#### Scenario: 默认值
- **WHEN** 用户未填写 base_url
- **THEN** 默认使用 `https://api.openai.com/v1`
- **AND** model 默认为空（要求用户填写）

#### Scenario: API 调用失败
- **WHEN** LLM API 返回错误（网络问题、key 无效、额度不足等）
- **THEN** 聊天区域显示错误信息
- **AND** 不崩溃程序

### Requirement: Agent 核心与工具调用
系统 SHALL 实现一个 Agent 循环，支持 function calling（工具调用）来编排项目。

#### Scenario: 工具调用循环
- **WHEN** AI 决定需要调用工具（如 create_project、add_steps）
- **THEN** 系统执行对应工具（通过 `api.call_tool`）
- **AND** 将工具结果返回给 AI
- **AND** AI 继续推理直到给出最终文本回复
- **AND** 每次工具调用在聊天界面显示执行状态和结果摘要

#### Scenario: 上下文压缩（LLM 摘要）
- **WHEN** 对话历史的近似 token 数超过阈值（默认模型上下文窗口的 80%，或配置的 `max_tokens`）
- **THEN** 系统将较早的消息（保留最近 6 条不压缩）发送给 LLM 生成结构化摘要
- **AND** 用摘要替换旧消息（格式：一条 role=system 的摘要消息）
- **AND** 新的上下文为：系统提示词 + 摘要 + 最近 6 条消息
- **AND** 摘要内容包含：用户需求与目标、已完成的操作及结果、当前进行中的任务、关键变量/项目名/步骤信息、用户偏好与约束
- **WHEN** 工具调用结果过长（超过 2000 字符）
- **THEN** 在历史中只保留前 500 字符 + 「…（已截断，完整结果见上方）」

#### Scenario: 边界限制
- **WHEN** 用户提问与程序无关的内容
- **THEN** AI 礼貌拒绝并引导回程序相关话题
- **WHEN** 用户要求执行与程序无关的操作
- **THEN** AI 拒绝执行

### Requirement: 帮助模式
系统 SHALL 提供帮助模式，用户可以提问程序功能用法、请求编写自由代码、讨论流程设计。

#### Scenario: 询问功能用法
- **WHEN** 用户问「怎么创建循环」
- **THEN** AI 基于程序的能力清单（`list_actions`、`describe_action`、`free_code_guide`）回答用法

#### Scenario: 帮助写自由代码
- **WHEN** 用户描述「我想把采集到的标题里的空格去掉」
- **THEN** AI 生成符合自由代码语法的函数（`@变量` 读写、`log()` 等）
- **AND** 解释代码逻辑

### Requirement: 创作模式
系统 SHALL 提供创作模式，AI 通过 `api.py` 工具直接在主页创建/编辑项目。

#### Scenario: 切换到创作模式
- **WHEN** 用户在 AI 帮助中心点击「切换到创作模式」
- **THEN** UI 切换到主页标签
- **AND** 主页上出现浮动聊天面板
- **AND** 用户可以在聊天框描述需求

#### Scenario: 一次性完成
- **WHEN** 用户描述「帮我建一个打开百度搜索'测试'的流程」
- **THEN** AI 调用 `create_project`、`add_steps` 等工具创建完整项目
- **AND** 主页画布实时刷新显示新项目
- **AND** AI 在聊天中报告创建结果

#### Scenario: 逐步交互
- **WHEN** 用户描述部分需求
- **THEN** AI 完成已描述的部分
- **AND** AI 主动询问用户下一步要做什么
- **AND** 用户继续描述后 AI 继续执行

#### Scenario: 退出创作模式
- **WHEN** 用户点击浮动面板的关闭按钮
- **THEN** 浮动面板收起
- **AND** 回到普通编辑模式

### Requirement: 浏览器交互工具（扩展 api.py）
系统 SHALL 在 `api.py` 中新增浏览器交互工具，供 AI 在创作模式下使用。

#### Scenario: 打开页面并获取结构
- **WHEN** AI 调用 `ai_browser_open(url)`
- **THEN** 系统打开可见浏览器窗口并导航到 URL
- **AND** 返回页面标题、URL 和交互元素摘要（表单/按钮/链接/输入框的 XPath + 文本）

#### Scenario: 获取页面结构
- **WHEN** AI 调用 `ai_browser_get_structure()`
- **THEN** 返回当前页面的交互元素列表（标签、文本、属性、建议 XPath）

#### Scenario: 元素捕获
- **WHEN** AI 调用 `ai_browser_capture_element(prompt)`
- **THEN** 系统在浏览器中注入元素选择器（复用 `element_picker.PICKER_JS`）
- **AND** 用户在页面上点选元素
- **AND** 返回捕获的 XPath、元素描述、命中数

#### Scenario: 截图
- **WHEN** AI 调用 `ai_browser_screenshot(name, project)`
- **THEN** 对当前页面截图并保存到项目的 `img/` 目录
- **AND** 返回文件路径

#### Scenario: 关闭浏览器
- **WHEN** AI 调用 `ai_browser_close()`
- **THEN** 关闭 AI 打开的浏览器窗口

### Requirement: 联网工具（扩展 api.py）
系统 SHALL 在 `api.py` 中新增网络搜索和网页抓取工具。

#### Scenario: 网络搜索
- **WHEN** AI 调用 `ai_web_search(query)`
- **THEN** 使用 DuckDuckGo HTML 搜索
- **AND** 返回前 N 条结果（标题、链接、摘要）

#### Scenario: 网页抓取
- **WHEN** AI 调用 `ai_web_fetch(url)`
- **THEN** 抓取指定 URL 的页面内容
- **AND** 返回纯文本（去除 HTML 标签，保留链接和文本结构）

### Requirement: AI 设置存储
系统 SHALL 将 AI 配置存储在用户配置文件中，与现有配置共存。

#### Scenario: 读取配置
- **WHEN** AI 功能初始化
- **THEN** 从 `%APPDATA%\小邹RPA\config.json` 的 `ai_config` 字段读取配置
- **AND** 未配置时返回默认值

#### Scenario: 保存配置
- **WHEN** 用户在设置对话框保存
- **THEN** 配置写入 `config.json` 的 `ai_config` 字段
- **AND** api_key 不明文显示（对话框中用密码模式输入）

## MODIFIED Requirements

### Requirement: MainWindow 标签页
MainWindow 原有单个「主页」标签，现在新增「AI 帮助中心」标签。标签切换时各自独立工作，创作模式下通过信号联动（AI 工具操作项目 → 主页画布刷新）。

### Requirement: api.py 工具注册
`api.py` 的 `TOOLS` 字典新增 7 个工具（5 个浏览器交互 + 2 个联网），通过 `_register` 注册，`describe_tools` 和 `call_tool` 自动覆盖。
