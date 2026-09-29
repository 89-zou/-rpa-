/* 页面内胶水：把 page-agent 和桌面端（Python）接起来。
 *
 * 注入顺序：先 page_agent.iife.js（暴露 window.RpaBundle），再这个文件。
 * 重复注入无害：开头有 window.__rpaGlue 守卫。
 *
 * 三条通道（都用「队列 + Python 轮询」，不用 expose_function —— 原因是
 * Playwright 的 sync API 里，expose_function 的回调跑在分发线程，回调里再调
 * sync API 有线程/死锁风险；队列轮询还能顺带实现「随时可中断」和「进度回显」）：
 *
 *   1) LLM：页面里 customFetch → __rpaLLMQueue → Python 用真 Key 调 LLM → __rpaLLM.reply
 *      （所以 API Key 永远不进页面）
 *   2) 动作：RpaPageController 把元素中心坐标交出来 → __rpaActionQueue → Python 用
 *      Playwright 真实鼠标/键盘执行（CDP 输入，isTrusted=true）→ __rpaAction.finish
 *   3) 变量：Python 每次任务前覆盖写 window.__RPA_VARS__，页面里用 get_local_variable 取
 */
(function () {
  if (window.__rpaGlue) return
  if (!window.RpaBundle || !window.RpaBundle.PageAgentCore) {
    console.error('[rpa] 胶水要在 page_agent.iife.js 之后注入')
    return
  }
  window.__rpaGlue = true

  const B = window.RpaBundle

  // ---------------------------------------------------------------
  // 变量：Python 在每次任务前整体覆盖
  // ---------------------------------------------------------------
  window.__RPA_VARS__ = window.__RPA_VARS__ || {}

  // ---------------------------------------------------------------
  // 请求队列（页面 → Python）
  // ---------------------------------------------------------------
  let seq = 0
  const llmWaiters = new Map()      // id → {resolve, reject}
  const actWaiters = new Map()      // id → resolve
  window.__rpaLLMQueue = []         // [{id, body}]
  window.__rpaActionQueue = []      // [{id, kind, x, y, text}]

  window.__rpaLLM = {
    request(body) {
      const id = ++seq
      return new Promise((resolve, reject) => {
        llmWaiters.set(id, { resolve, reject })
        window.__rpaLLMQueue.push({ id: id, body: String(body == null ? '' : body) })
      })
    },
    reply(id, text) {
      const w = llmWaiters.get(id)
      if (w) {
        llmWaiters.delete(id)
        w.resolve(String(text == null ? '' : text))
      }
    },
    fail(id, message) {
      const w = llmWaiters.get(id)
      if (w) {
        llmWaiters.delete(id)
        w.reject(new Error(String(message || 'LLM 调用失败')))
      }
    },
    pending() { return llmWaiters.size },
  }

  window.__rpaAction = {
    _ask(kind, x, y, text) {
      const id = ++seq
      return new Promise((resolve) => {
        actWaiters.set(id, resolve)
        window.__rpaActionQueue.push({
          id: id, kind: kind,
          x: Number(x), y: Number(y), text: String(text == null ? '' : text),
        })
      })
    },
    click(x, y) { return this._ask('click', x, y, '') },
    type(x, y, text) { return this._ask('type', x, y, text) },
    /** Python 执行完调这个：ok=false 时 error 里写原因。 */
    finish(id, ok, error) {
      const done = actWaiters.get(id)
      if (done) {
        actWaiters.delete(id)
        done({ ok: !!ok, error: String(error || '') })
      }
    },
    pending() { return actWaiters.size },
  }

  // ---------------------------------------------------------------
  // 活动日志（Python 轮询取走，打进运行日志）
  // ---------------------------------------------------------------
  const logs = []
  function pushLog(text) {
    if (!text) return
    logs.push(String(text))
    if (logs.length > 800) logs.splice(0, 400)      // 别无限涨
  }
  const CN = {
    thinking: '思考中…',
    executing: '执行',
    executed: '完成',
    retrying: '重试中',
    error: '出错',
  }
  function activityText(a) {
    if (!a || !a.type) return ''
    const label = CN[a.type] || a.type
    if (a.type === 'executing' || a.type === 'executed') {
      const tool = a.tool || ''
      const input = a.input ? JSON.stringify(a.input) : ''
      return `[agent] ${label} ${tool} ${input}`.trim().slice(0, 300)
    }
    if (a.type === 'error') return `[agent] 出错：${a.message || ''}`
    return `[agent] ${label}`
  }

  // ---------------------------------------------------------------
  // Agent 生命周期
  // ---------------------------------------------------------------
  const state = {
    agent: null,
    state: 'idle',      // idle | running | completed | error | stopped
    done: false,
    success: null,
    data: '',
    error: '',
  }

  window.__rpaAgent = {
    ready: true,

    /** 起一个任务。config: {model, language, max_steps}；task: 自然语言；
     *  fresh=true 表示换一个新 Agent（页面已经跳走了）；false 表示接着上一个干
     *  （同一个页面上连续的几个任务共用记忆：它记得前面点过什么、填过什么）。 */
    start(config, task, fresh) {
      config = config || {}
      if (state.agent && fresh) {              // 页面换了 / 上次异常：旧的清掉
        try { state.agent.dispose() } catch (e) { /* 忽略 */ }
        state.agent = null
      }
      state.done = false
      state.success = null
      state.data = ''
      state.error = ''
      state.state = 'running'

      let agent = state.agent
      if (!agent) {
        const controller = new B.RpaPageController({ enableMask: false, viewportExpansion: 0 })
        agent = new B.PageAgentCore({
          pageController: controller,
          baseURL: 'https://rpa-bridge.invalid/v1',   // 只是个占位：真请求由 customFetch 接管
          model: String(config.model || 'rpa-bridge'),
          language: String(config.language || 'zh-CN'),
          maxSteps: Number(config.max_steps) > 0 ? Number(config.max_steps) : 20,
          customFetch: async function (url, init) {
            const body = init && init.body ? init.body : ''
            const text = await window.__rpaLLM.request(body)
            return new Response(text, { status: 200, headers: { 'Content-Type': 'application/json' } })
          },
          customTools: {
            // 让模型能主动取「变量清单」里的值（读取数据 / 循环项都在里面）
            get_local_variable: {
              description: '从 RPA 变量上下文里取一个变量的值。当需要填写来自本地文件、' +
                '上一步产出、或循环当前项的数据时用它。变量路径如 文章.标题、loop.item.内容。',
              inputSchema: B.z.object({
                variablePath: B.z.string().describe('变量路径，如 文章.标题 或 loop.item.内容'),
              }),
              execute: async function (args) {
                const parts = String((args && args.variablePath) || '').split('.')
                let v = window.__RPA_VARS__ || {}
                for (let i = 0; i < parts.length; i++) {
                  if (v == null) break
                  v = v[parts[i]]
                }
                if (v == null) return ''
                return typeof v === 'string' ? v : JSON.stringify(v)
              },
            },
          },
        })
        agent.addEventListener('activity', function (e) {
          pushLog(activityText(e && e.detail))
        })
        agent.addEventListener('statuschange', function () {
          try { state.state = agent.status || state.state } catch (err) { /* 忽略 */ }
        })
        state.agent = agent
        pushLog('[agent] 建好了一个 Agent（这一步开始）')
      } else {
        pushLog('[agent] 接着上一个 Agent 干（它记得前面做过的事）')
      }

      pushLog(`[agent] 开始任务：${String(task).slice(0, 120)}`)
      agent.execute(String(task || ''))
        .then(function (res) {
          state.success = !!(res && res.success)
          state.data = (res && res.data) ? String(res.data) : ''
        })
        .catch(function (err) {
          state.success = false
          state.error = String((err && err.message) || err || '未知错误')
        })
        .finally(function () {
          state.done = true
          if (state.state === 'running') state.state = 'completed'
        })
      return true
    },

    /** Python 每轮问一次：做完了没、第几步、还欠多少请求。 */
    status() {
      let step = 0
      try {
        step = state.agent && state.agent.history ? state.agent.history.length : 0
      } catch (e) { /* 忽略 */ }
      return {
        state: state.state,
        done: !!state.done,
        success: state.success,
        data: state.data,
        error: state.error,
        step: step,
        pending_llm: window.__rpaLLM.pending(),
        pending_action: window.__rpaAction.pending(),
      }
    },

    /** 取走增量日志（Python 打进运行日志）。 */
    log() {
      const out = logs.slice()
      logs.length = 0
      return out
    },

    /** 停止当前任务（Python 点「停止」或超时时调）。 */
    stop() {
      try {
        if (state.agent) state.agent.stop()
        state.state = 'stopped'
      } catch (e) { /* 忽略 */ }
    },

    /** 清理（任务彻底结束后调，把 controller 的资源放掉）。 */
    dispose() {
      try {
        if (state.agent) state.agent.dispose()
      } catch (e) { /* 忽略 */ }
      state.agent = null
      state.done = true
      state.state = 'idle'
    },
  }
})()