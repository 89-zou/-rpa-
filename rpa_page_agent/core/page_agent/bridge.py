# -*- coding: utf-8 -*-
"""一次「智能页面任务」的完整循环：注入 → 起 agent → 轮询桥 → 收结果。

设计要点（都是踩过坑后的选择）：

· **队列 + 轮询**，不用 `page.expose_function`：Playwright 的 sync API 里，
  expose_function 的回调跑在分发线程，回调里再调 sync API 会撞线程/死锁；
  队列轮询还有个好处 —— 每一轮都能看「用户是不是点了停止」，所以**随时能中断**，
  并且能把 agent 的活动（思考/执行/完成）实时打进运行日志。
· **所有 Playwright 调用都在执行器线程**（页面的异步循环在页面里跑，我们只搬运）。
· **真实鼠标键盘**：点击/输入由本进程的 `page.mouse` / `page.keyboard` 执行
  （走 CDP 输入，事件 isTrusted=true），页面内只负责「算元素中心坐标 + 决定点哪个」。
· **API Key 不进页面**：页面的 LLM 请求（customFetch）交回这里，用项目配置去调。
"""
import json
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from rpa_page_agent.core.page_agent import config as pa_config
from rpa_page_agent.core.page_agent import llm

HERE = Path(__file__).resolve().parent
BUNDLE_FILE = HERE / "page_agent.iife.js"
GLUE_FILE = HERE / "page_agent_glue.js"

#: 轮询间隔（秒）：越小越跟手，越大越省 CPU
POLL_S = 0.15
#: 注入后等 window.__rpaAgent.ready 的上限
INJECT_TIMEOUT_S = 15
#: 每轮最多顺手处理几个 LLM 请求 / 动作（避免一轮卡太久，影响停止响应）
LLM_PER_ROUND = 3
ACTION_PER_ROUND = 6


class PageAgentError(RuntimeError):
    """智能任务出错（没构建产物、LLM 挂了、任务没做成、超时……）。"""


class PageAgentStopped(PageAgentError):
    """用户中途点了停止。"""


def bundle_ready() -> bool:
    """页面内 Agent 的产物在不在（构建一次就有了，见 bundle/build.mjs）。"""
    return BUNDLE_FILE.is_file() and GLUE_FILE.is_file()


def missing_hint() -> str:
    return ("还没构建「页面内 Agent」的产物。\n"
            f"    缺：{BUNDLE_FILE.name} / {GLUE_FILE.name}\n"
            "    构建（只要跑一次）：\n"
            "      cd rpa_page_agent/core/page_agent/bundle\n"
            "      npm install && node build.mjs")


def _first_line(exc, limit: int = 160) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:limit]


def _jsonable(variables: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """变量表 → 能塞进页面的字典（都是文本，跟项目里其它地方一致）。"""
    out: Dict[str, str] = {}
    for k, v in (variables or {}).items():
        out[str(k)] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


class PageAgentBridge:
    """把「页面内的 page-agent」和「本进程的真实鼠标键盘 / LLM」接起来。"""

    def __init__(self, page, log: Callable[[str], None] = print,
                 should_stop: Optional[Callable[[], bool]] = None,
                 config: Optional[Dict[str, Any]] = None):
        self.page = page
        self.log = log
        self.should_stop = should_stop or (lambda: False)
        self.config = config or {}
        self.injected = False
        self._llm_calls = 0

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def ensure_injected(self):
        """把 bundle + 胶水装进页面（当前页立刻生效；以后每次跳转也自动装）。"""
        if self.injected:
            return
        if not bundle_ready():
            raise PageAgentError(missing_hint())
        bundle = BUNDLE_FILE.read_text(encoding="utf-8")
        glue = GLUE_FILE.read_text(encoding="utf-8")

        # ① 挂到 context 上：以后每次导航（含点链接跳转、表单提交）都会自动注入
        try:
            ctx = self.page.context
            ctx.add_init_script(script=bundle)
            ctx.add_init_script(script=glue)
        except Exception as e:
            self.log(f"  提示：没能挂上自动注入（{_first_line(e)}），本次只装当前页")

        # ② 当前这一页现在就要能用（脚本标签 + evaluate 兜底，绕过页面 CSP）
        if not self._has_glue():
            self._run_script(bundle)
            self._run_script(glue)
            try:
                self.page.wait_for_function(
                    "() => !!(window.__rpaGlue && window.__rpaAgent && window.__rpaAgent.ready)",
                    timeout=INJECT_TIMEOUT_S * 1000)
            except Exception as e:
                raise PageAgentError(
                    f"页面内 Agent 注入后没就绪（{_first_line(e)}）。\n"
                    "    可能是页面太特殊（比如还是 about:blank）——先「打开网页」再点这个节点。") from e
        self.injected = True
        self.log("  页面内 Agent 已就绪")

    def _has_glue(self) -> bool:
        try:
            return bool(self.page.evaluate("() => !!(window.__rpaGlue && window.__rpaAgent)"))
        except Exception:
            return False

    def _run_script(self, source: str):
        """在当前页面执行一段完整脚本：先试 script 标签，不行就 evaluate 包一层。"""
        try:
            self.page.add_script_tag(content=source)
            return
        except Exception:
            pass                    # 严格 CSP 的站点会拒掉 inline script
        try:
            self.page.evaluate("(function(){ " + source + " })()")
        except Exception as e:
            raise PageAgentError(f"没能在页面里装 Agent 脚本：{_first_line(e)}") from e

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run_task(self, task: str, variables: Optional[Dict[str, Any]] = None,
                 max_steps: int = 20, timeout_s: int = 180) -> Dict[str, Any]:
        """跑一个任务，返回 {"success", "data", "steps"}；失败/超时/被停都抛异常。"""
        self.ensure_injected()
        cfg = self.config
        self.page.evaluate("(v) => { window.__RPA_VARS__ = v || {}; }",
                           _jsonable(variables))
        self.page.evaluate(
            "([c, t]) => window.__rpaAgent.start(c, t)",
            [{"model": str(cfg.get("model") or ""),
              "language": str(cfg.get("language") or "zh-CN"),
              "max_steps": int(max_steps or 20)}, str(task or "")])

        started = time.monotonic()
        last_step = 0
        limit = max(5, int(timeout_s or 180))
        while True:
            elapsed = time.monotonic() - started
            # 先看「停」和「超时」再干活：这样每次新动作之前都是最新的判断
            if self.should_stop():
                self._abort("你点了停止")
                raise PageAgentStopped("智能任务被手动停止")
            if elapsed > limit:
                self._abort(f"超过 {limit} 秒")
                raise PageAgentError(
                    f"智能任务超时（{limit} 秒，走到第 {last_step} 步）。\n"
                    "    可以：把超时调大、把任务写得更聚焦，或先手动把页面弄到合适的状态。")
            self._drain_logs()
            # 单次 LLM 调用也要有上限：不然一次调用能把循环占住好几分钟，
            # 期间的「停止」和「超时」都没机会生效
            self._serve_llm(cfg, budget_s=int(min(60, max(5, limit - elapsed)) + 1))
            self._serve_actions()
            state = self._status()
            step = int(state.get("step") or 0)
            if step > last_step:
                last_step = step
                self.log(f"  [agent] 第 {step} 步")
            if state.get("done"):
                break
            self._sleep(POLL_S)

        self._drain_logs()
        try:
            self.page.evaluate("() => window.__rpaAgent.dispose()")
        except Exception:
            pass
        success = state.get("success")
        data = str(state.get("data") or "")
        if not success:
            raise PageAgentError(
                "智能任务没做成：" + (data or state.get("error") or "（agent 没说原因）") +
                "\n    可以把任务描述得更明确（比如「登录按钮是蓝色的那个」），或加一句「额外要求」。")
        return {"success": True, "data": data, "steps": last_step,
                "llm_calls": self._llm_calls}

    # ------------------------------------------------------------------
    # 桥的几根线
    # ------------------------------------------------------------------
    def _status(self) -> Dict[str, Any]:
        try:
            return self.page.evaluate("() => window.__rpaAgent.status()") or {}
        except Exception as e:
            raise PageAgentError(f"页面没了或 Agent 状态读不到：{_first_line(e)}") from e

    def _drain_logs(self):
        try:
            lines = self.page.evaluate("() => window.__rpaAgent.log()") or []
        except Exception:
            return
        for line in lines:
            text = str(line or "").strip()
            if text:
                self.log("  " + text)

    def _serve_llm(self, cfg: Dict[str, Any], budget_s: int = llm.DEFAULT_TIMEOUT_S):
        """页面要调 LLM → 我们用它给的请求体去调（Key 只在这边）。

        budget_s：这一轮最多让单次调用等多久（跟节点剩余时间挂钩，见 run_task）。
        """
        for _ in range(LLM_PER_ROUND):
            try:
                req = self.page.evaluate("() => window.__rpaLLMQueue.shift() || null")
            except Exception:
                return
            if not req:
                return
            rid = req.get("id")
            self._llm_calls += 1
            try:
                text = llm.chat_completions(cfg, req.get("body") or "",
                                            timeout_s=max(5, int(budget_s)))
                self.page.evaluate("([i, t]) => window.__rpaLLM.reply(i, t)", [rid, text])
            except Exception as e:
                why = _first_line(e, 300)
                self.log(f"  LLM 调用失败：{why}")
                try:
                    self.page.evaluate("([i, m]) => window.__rpaLLM.fail(i, m)", [rid, why])
                except Exception:
                    pass

    def _serve_actions(self):
        """页面要点击/输入 → 我们用真实鼠标键盘做（CDP 输入，isTrusted=true）。"""
        for _ in range(ACTION_PER_ROUND):
            try:
                act = self.page.evaluate("() => window.__rpaActionQueue.shift() || null")
            except Exception:
                return
            if not act:
                return
            ok, err = False, ""
            kind = str(act.get("kind") or "")
            try:
                if kind == "click":
                    self._real_click(float(act.get("x") or 0), float(act.get("y") or 0))
                    ok = True
                elif kind == "type":
                    self._real_type(float(act.get("x") or 0), float(act.get("y") or 0),
                                    str(act.get("text") or ""))
                    ok = True
                else:
                    err = f"不认识的动作：{kind}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.log(f"  真实{ '点击' if kind == 'click' else '输入' }失败：{err}")
            try:
                self.page.evaluate("([i, o, e]) => window.__rpaAction.finish(i, o, e)",
                                   [act.get("id"), ok, err])
            except Exception:
                return

    def _real_click(self, x: float, y: float):
        x, y = self._clamp(x, y)
        self.log(f"  真实鼠标点击 ({x:.0f},{y:.0f})")
        self.page.mouse.move(x, y, steps=6)
        time.sleep(random.uniform(0.06, 0.16))
        self.page.mouse.down()
        time.sleep(random.uniform(0.04, 0.12))
        self.page.mouse.up()

    def _real_type(self, x: float, y: float, text: str):
        x, y = self._clamp(x, y)
        self.log(f"  真实键盘输入 ({x:.0f},{y:.0f})：{text[:40]}"
                 + ("…" if len(text) > 40 else ""))
        self.page.mouse.move(x, y, steps=6)
        self.page.mouse.click(x, y)
        time.sleep(random.uniform(0.12, 0.25))
        kind = self._focus_kind()
        if kind in ("field", "richtext"):       # 清掉原内容（跟官方 input_text 一个语义：替换）
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            time.sleep(0.05)
        elif kind.startswith("other"):
            self.log(f"  提示：焦点不在输入框里（{kind}），这次不清空、直接输入")
        for seg, ascii_only in _segments(text):
            if ascii_only:
                self.page.keyboard.type(seg, delay=random.randint(35, 85))
            else:
                self.page.keyboard.insert_text(seg)     # 中文/emoji：CDP 直接插入，可靠
            time.sleep(random.uniform(0.02, 0.06))

    def _focus_kind(self) -> str:
        """焦点落在什么上（决定要不要按 Ctrl+A 清空：别把整页选没了）。"""
        try:
            return str(self.page.evaluate("""() => {
                const el = document.activeElement;
                if (!el) return 'none';
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea') return 'field';
                if (el.isContentEditable) return 'richtext';
                return 'other:' + tag;
            }"""))
        except Exception:
            return "unknown"

    def _clamp(self, x: float, y: float):
        """把坐标压在视口里：外面的点根本送不到元素上。"""
        try:
            size = self.page.viewport_size or {}
            vw, vh = float(size.get("width") or 1280), float(size.get("height") or 720)
        except Exception:
            vw, vh = 1280.0, 720.0
        return min(max(x, 1.0), vw - 2.0), min(max(y, 1.0), vh - 2.0)

    def _abort(self, why: str):
        self.log(f"  停止智能任务（{why}）")
        try:
            self.page.evaluate("() => window.__rpaAgent.stop()")
        except Exception:
            pass
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self._drain_logs()
            try:
                if (self._status() or {}).get("done"):
                    break
            except Exception:
                break
            self._sleep(0.2)
        try:
            self.page.evaluate("() => window.__rpaAgent.dispose()")
        except Exception:
            pass

    def _sleep(self, seconds: float):
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)         # 页面没了也别把循环拖死


# ======================================================================
# 给执行器用的两个薄接口
# ======================================================================
def run_agent_task(executor, step) -> Dict[str, Any]:
    """执行「智能页面任务」节点（step.action == "agent"）。"""
    task = executor._resolve_value(step.agent_task or "").strip()
    if not task:
        raise ValueError("「智能页面任务」还没写任务：双击节点，用一句话说清要做什么"
                         "（例：在标题框填「今天天气」，然后点发布）")
    hints = executor._resolve_value(step.agent_hints or "").strip()
    if hints:
        task = f"{task}\n\n额外要求：{hints}"

    cfg = pa_config.load(executor.project_dir)
    bad = pa_config.problem(cfg)
    if bad:
        raise ValueError(bad)
    if not bundle_ready():
        raise PageAgentError(missing_hint())

    max_steps = int(step.agent_max_steps or cfg.get("max_steps") or 20)
    timeout_s = int(step.agent_timeout or cfg.get("timeout_s") or 180)
    first = task.splitlines()[0][:60]
    executor.log(f"  智能任务：{first}（最多 {max_steps} 步，超时 {timeout_s} 秒）")
    bridge = PageAgentBridge(executor._page, executor.log,
                             should_stop=lambda: bool(getattr(executor, "_stop", False)),
                             config=cfg)
    result = bridge.run_task(task, variables=executor.variables,
                             max_steps=max_steps, timeout_s=timeout_s)
    said = str(result.get("data") or "").strip().replace("\n", " ")
    executor.log(f"  智能任务完成（{result.get('steps')} 步"
                 f"，LLM 调用 {result.get('llm_calls')} 次）：{said[:200]}")
    return result


def run_upload(executor, step) -> Dict[str, Any]:
    """执行「上传文件」节点（step.action == "upload"）：文件由本进程接管。

    两条路，从稳到活：
    1. 填了 input[type=file] 的 XPath → 直接 set_input_files（不依赖按钮/弹窗）；
    2. 没填 → 让智能 Agent 找到并点「上传按钮」，本进程用 expect_file_chooser 接下文件。
    """
    raw = executor._resolve_value(step.upload_file or "").strip()
    if not raw:
        raise ValueError("「上传文件」还没填文件路径：双击节点填（可以写 {{变量}}，"
                         "比如循环里写 {{loop.item.path}}）")
    path = Path(raw)
    if not path.is_absolute() and executor.project_dir:
        path = Path(executor.project_dir) / path
    if not path.is_file():
        raise FileNotFoundError(
            f"要上传的文件不存在：{path}\n"
            "    路径可以写 {{变量}}；相对路径是相对项目目录。")

    page = executor._page
    selector = (step.locator.value if step.locator else "").strip()
    if selector:
        try:
            page.set_input_files(f"xpath={selector}", str(path))
            executor.log(f"  已把文件交给上传框：{path.name}")
            return {"how": "input[type=file]", "file": str(path)}
        except Exception as e:
            executor.log(f"  直接交给上传框没成（{_first_line(e)}）→ 改用「点按钮 + 文件选择器」")

    desc = executor._resolve_value(step.upload_desc or "").strip() or "上传文件的按钮"
    cfg = pa_config.load(executor.project_dir)
    bad = pa_config.problem(cfg)
    if bad:
        raise ValueError(bad + "（点按钮这条路要用智能 Agent 定位，需要先配好 LLM）")
    if not bundle_ready():
        raise PageAgentError(missing_hint())
    executor.log(f"  让智能 Agent 去点「{desc}」，文件由本程序接管")
    try:
        with page.expect_file_chooser(timeout=120000) as fc:
            bridge = PageAgentBridge(page, executor.log,
                                     should_stop=lambda: bool(getattr(executor, "_stop", False)),
                                     config=cfg)
            bridge.run_task(f"点击这个按钮：{desc}", variables=executor.variables,
                            max_steps=6, timeout_s=120)
    except PageAgentStopped:
        raise
    except Exception as e:
        if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
            raise TimeoutError(
                "点了按钮，但一直没等到文件选择器（也可能是这个按钮本来就不弹窗）。\n"
                "    最稳的办法：用【捕获元素…】把页面上 input[type=file] 的 XPath 填进"
                "「上传框定位」，那样直接喂文件。") from e
        raise
    chooser = fc.value
    chooser.set_files(str(path))
    executor.log(f"  已通过文件选择器上传：{path.name}")
    return {"how": "file-chooser", "file": str(path)}


def _segments(text: str):
    """把文本切成「纯 ASCII 段 / 其它段」：ASCII 逐键打字更像人，中文走插入。"""
    out = []
    buf, ascii_only = "", None
    for ch in str(text or ""):
        cur = ord(ch) < 128
        if ascii_only is None or cur == ascii_only:
            buf += ch
            ascii_only = cur
        else:
            out.append((buf, ascii_only))
            buf, ascii_only = ch, cur
    if buf:
        out.append((buf, bool(ascii_only)))
    return out