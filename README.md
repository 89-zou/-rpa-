# 小邹RPA（smart_tool）

> 一个跑在 Windows 上的**流程自动化工具**：把「打开网页 / 点按钮 / 填表单 / 采数据 / 循环 / 判断」这些
> 操作画成流程图，点一下就跑；也能在节点里直接写 Python / JavaScript。
> 图形化编排 + 真实浏览器（Playwright）+ 桌面截图定位，给不会写代码的人和想省事的开发者用。

作者：**@小邹** ｜ 协议：[MIT](LICENSE) ｜ 界面与代码注释全中文

**目录**：[它是什么](#它是什么) · [功能](#功能) · [快速开始](#快速开始) · [怎么用](#怎么用) · [目录结构](#目录结构) · [打包](#打包) · [给 AI 用的接口](#给-ai-用的接口) · [常见问题](#常见问题) · [English](#english)

---

## 它是什么

你在界面上搭一条流程，程序替你点。两种场景：

| 场景 | 怎么干活 |
|---|---|
| **网页自动化** | Playwright 驱动 Chromium：XPath 定位元素、等页面加载、拦住整页跳转；元素点不到时可以**用截图兜底**（OpenCV 模板匹配再找一次） |
| **桌面应用** | 截图定位 + 系统级鼠标键盘（pyautogui），可选 UI Automation 抓控件名字和坐标 |

流程存成 JSON（`projects/<项目名>/steps.json`），**项目和数据在同一个文件夹里**：拷到别的电脑、放 U 盘，接着跑。

## 功能

**节点（17 种）**

| 分组 | 节点 |
|---|---|
| 网页 | 打开网页、点击、填入、下拉选择、暂停等人工（验证码之类，可设自动恢复信号） |
| 桌面 | 激活窗口、按键（快捷键）、等待 |
| 数据 | 读取数据（txt / csv / xlsx / xls / json / 文件夹，字段可自定义挑）、采集数据（页面上抓字段存结果） |
| 结构 | 循环、条件 if/else（多分支）、组合（把一串步骤收成一个，画面清爽） |
| 代码 | 自由代码（Python / JavaScript）、调用函数（函数库里的函数） |
| 其它 | 提示 / 日志 |

**编排界面**

- 画布：拖节点摆位置、拉箭头连线、Ctrl+滚轮缩放（缩放跟着项目记住）、自动排版
- 流程编辑：列表式 + 缩进显示块结构，每个循环体 / 分支末尾有「＋ 新增节点」
- 循环 / 条件 / 组合可以**任意嵌套**（分支里放循环、循环里放条件都行），整块可拖动、可增删移
- 节点编号只数真节点：循环结束、条件结束、组合结束这些**结构标记不占编号**

**写代码也不憋屈**

- 自由代码节点就是一个**真正的函数**：Python 按 Python 语法、JS 按 ES6 语法（自动缩进）
- `@变量名` 读写变量清单、`/图片名` 引用图片库、`#文件=路径` 把文件路径当参数传进来
- 保存即进**函数库**，别处用「调用函数」节点复用；改了函数名，调用它的节点自动跟着改
- 执行有超时刹车（卡住的循环 / 死循环会被中断）

**数据与登录态**

- 变量清单、图片库、命名元素定位（一处定义，多处引用）
- 采集结果导出 Excel / CSV
- 登录态保存复用：登录一次存 cookie，下次自动跳过登录步骤（可配置「已登录就跳过」）

**给 AI 用**

- `smart_tool/core/api.py` 把 36 个能力包成工具（读能力清单、增删改节点、校验、跑流程、导数据……），JSON 调用，AI 可以照着描述直接编排项目

## 快速开始

需要 **Windows 10/11**、**Python 3.11+**（开发用的是 3.13）。

```bat
git clone https://github.com/89-zou/-rpa-.git smart_tool
cd smart_tool

:: 1) 建虚拟环境、装依赖（一定要装在项目里的 .venv，别装全局）
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

:: 2) 装浏览器内核（约 700 MB，只装一次）
.venv\Scripts\python -m playwright install chromium

:: 3) 启动
.venv\Scripts\python -m smart_tool.main
```

> **内核装哪儿？** 默认在 `%LOCALAPPDATA%\ms-playwright`。想让它待在项目里
> （不往 C 盘塞东西、拷 U 盘就能带走、也不怕被清理软件删），**在项目目录下建一个
> 叫 `浏览器` 的文件夹**就行——程序会自动认这个目录，下载和启动都用它。
> 也可以直接设环境变量 `PLAYWRIGHT_BROWSERS_PATH` 指到别处（优先级最高）。
> 下载慢的话用国内镜像：
>
> ```powershell
> $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
> .venv\Scripts\python -m playwright install chromium
> ```

不想开界面、只想跑某个项目：

```bat
.venv\Scripts\python run_cli.py 采集示例-登录与采集
```

第一次打开会默认载入内置示例项目 **`采集示例-登录与采集`**（42 个节点，两个可练习的站点：
quotes.toscrape.com 登录随便填、saucedemo.com 用 `standard_user / secret_sauce`），
把每种节点都示范了一遍——照着它改最快。

## 怎么用

1. **新建 / 载入项目**：主页顶上【新建项目…】【载入项目…】。
2. **画流程**：画布上双击空白处或右键新增节点，双击节点填参数（要定位的元素可以用【捕获元素】直接点页面上取 XPath）。
3. **跑**：点【开始运行】，右下角小窗看日志，可以暂停 / 继续 / 停止。
4. **看结果**：采集的数据在「采集结果」里，能导出 Excel / CSV。

数据放哪儿：

- 项目就在 `projects/<项目名>/`（含 `steps.json`、`data/`、`img/`、`auth/`）
- 程序目录旁边有 `projects/` → 就用程序目录（绿色版）；否则用 `%APPDATA%\小邹RPA`
- 设置（数据目录、默认项目等）在 `%APPDATA%\小邹RPA\config.json`

## 目录结构

```
smart_tool/
├─ smart_tool/
│  ├─ main.py                GUI 入口（python -m smart_tool.main）
│  ├─ paths.py               三种目录：程序 / 资源 / 用户数据
│  ├─ core/                  不依赖界面的核心
│  │  ├─ project_store.py      项目与步骤读写（steps.json）
│  │  ├─ blocks.py             把线性步骤解析成块树（循环 / 条件 / 分支 / 组合）
│  │  ├─ step_executor.py      执行引擎（Playwright / 桌面 / 数据 / 代码）
│  │  ├─ data_sources.py       读 txt / csv / excel / json / 文件夹
│  │  ├─ free_code.py          自由代码节点：语法解析、Python / JS 代码生成
│  │  ├─ api.py                给 AI / 自动化调用的工具层（36 个工具 + 命令行）
│  │  ├─ image_locator.py      截图定位（OpenCV 模板匹配 + 多尺度）
│  │  ├─ desktop*.py           桌面场景（截图、鼠标键盘、UI Automation）
│  │  ├─ auth_store.py         登录态保存与复用
│  │  └─ crash_guard.py        原生崩溃兜底（写 crash.log）
│  ├─ ui/                    PyQt6 界面（画布、流程编辑、各类面板）
│  └─ assets/ …              随程序走的资源
├─ assets/                   logo、演示项目模板（安装时复制到用户目录）
├─ packaging/                打包成单文件 exe 的脚本（.spec + build.ps1）
├─ projects/                 你的项目（只有内置示例进版本库）
├─ run_cli.py                命令行跑一个项目
└─ requirements.txt
```

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# 产物：dist\小邹RPA.exe（单文件，约 144 MB；浏览器内核运行时才下载）
```

## 给 AI 用的接口

每个能力都有 JSON 入口，AI（或你的脚本）可以照着能力清单自己编排项目：

```bat
:: 列出所有工具
.venv\Scripts\python -m smart_tool.core.api tools

:: 看某个节点有哪些字段
.venv\Scripts\python -m smart_tool.core.api describe click

:: 调一个工具
.venv\Scripts\python -m smart_tool.core.api call list_steps "{\"project\":\"采集示例-登录与采集\"}"
```

`describe_tools()` 会按函数签名生成 OpenAI tools 格式的 JSON Schema，可直接喂给模型。

## 常见问题

| 现象 | 怎么办 |
|---|---|
| 报「还没装浏览器内核」 | 跑一次 `.venv\Scripts\python -m playwright install chromium`（慢就用上面的国内镜像） |
| 想让内核待在项目里 / 不占 C 盘 | 在项目目录下建一个 `浏览器` 文件夹，内核就装在那儿、也从那儿启动（也可以设 `PLAYWRIGHT_BROWSERS_PATH`） |
| 内核莫名不见了（清理软件、卸载程序删过） | 重新装一次即可；装到项目旁的 `浏览器` 文件夹里最不容易被波及 |
| 元素点不到 | 优先用【捕获元素】取 XPath；页面结构会变的，给定位配一张截图兜底（点不到就用模板匹配再试） |
| 程序闪退、没提示 | 看 `crash.log`（在用户数据目录里），里面有异常调用栈 |
| 换了电脑 / 项目搬家后「文件不存在」 | 不用改：数据源路径找不到时，会按文件名在项目目录里自动找同名文件，日志里会写一句 |
| 卡在某个循环里 | 每个节点都能设超时；自由代码的循环也有超时刹车，到点中断 |
| 想改默认数据目录 | 删掉 `%APPDATA%\小邹RPA\config.json` 重开程序，或直接改里面的 `data_dir` |

## 开源版说明

这个仓库是**开发版**：直接源码运行，进去就是主界面。作者发行版里的**启动广告页**、
**安装向导**（选安装文件夹、建快捷方式、登记系统卸载入口）和**卸载程序**不在这里——
`main.py` 对它们的导入是可选的，所以缺了这些文件照样跑，写流程、跑流程都不受影响。

## English

> **XiaoZou RPA (smart_tool)** — a Windows desktop tool for **building automation flows visually**:
> open pages, click, fill forms, scrape data, loop, branch — or drop into real Python / JavaScript
> inside a node. Powered by PyQt6 + Playwright, with OpenCV template matching as a fallback locator
> for both web and desktop apps.

**Highlights**

- **17 node types**: open page, click, fill, select, pause-for-human, activate window, hotkey, delay,
  read data (txt / csv / xlsx / xls / json / folder), collect data, loop, if/else with multiple
  branches, group (collapse a run of steps into one card), free code (Python / ES6 JavaScript),
  call a function from the project's function library, note/log.
- **Nested blocks**: loops, conditions and groups can be nested arbitrarily; blocks can be dragged as a
  whole. Structural markers (loop end / condition end / group end) don't consume step numbers.
- **Real code nodes**: a node is a real function; `@var` reads/writes the variable list, `/image`
  references the image library, `#file=path` passes a file path argument. Saved functions go to a
  per-project function library and can be called from other nodes; renaming propagates automatically.
  Timeouts stop runaway loops.
- **Data & sessions**: variables, image library, named locators, export collected rows to Excel / CSV,
  save & reuse login state (cookies) so runs skip the login steps when already signed in.
- **AI-friendly API**: `smart_tool/core/api.py` exposes 36 tools (list capabilities, add/update/delete
  steps, validate, run, export records, …) with JSON schemas, plus a small CLI.
- **Portable by design**: a project folder contains both the flow and its data; copy the folder to move
  a project to another machine. If a hard-coded data path is missing, the file is looked up by name
  inside the project folder automatically.

**Quick start (Windows, Python 3.11+)**

```bat
git clone https://github.com/89-zou/-rpa-.git smart_tool
cd smart_tool
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium     :: ~700 MB on disk, once
.venv\Scripts\python -m smart_tool.main                 :: launch the GUI
```

The bundled demo project `采集示例-登录与采集` (42 nodes, two practice sites) is loaded on first
launch and exercises every node type. Headless run of a project:
`.venv\Scripts\python run_cli.py 采集示例-登录与采集`.

> **Where the browser kernel lives**: by default `%LOCALAPPDATA%\ms-playwright`. To keep it
> inside the project (nothing on the C: drive, portable on a USB stick), just create a folder
> named `浏览器` next to the code — the app will download to and launch from it automatically.
> `PLAYWRIGHT_BROWSERS_PATH` still wins if it is set. In mainland China the CDN can be very slow;
> use a mirror:
>
> ```powershell
> $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
> .venv\Scripts\python -m playwright install chromium
> ```

**Package a single-file exe**

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# -> dist\小邹RPA.exe (~144 MB, single file)
```

**Note on this repository**: it is the *developer* build. The author's distribution-only parts — the
startup ad page, the installer wizard (install folder / shortcuts / uninstall entry) and the
uninstaller — are intentionally not included; `main.py` imports them optionally, so everything still
runs from source.

**License**: [MIT](LICENSE) ｜ **Author**: @小邹
