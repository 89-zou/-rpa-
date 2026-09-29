/**
 * 小邹RPA 的 page-agent 打包入口。
 *
 * 构建：node build.mjs   →  产物 ../page_agent.iife.js（提交进仓库，运行时注入页面）
 * 版本：@page-agent/core 与 @page-agent/page-controller 都锁 1.12.4（见 package.json）
 *
 * 为什么要自己包一层（而不是直接用官方产物）：
 *   1) 官方的点击/输入是「页面内 dispatchEvent」，事件 isTrusted=false；
 *      我们要真人级输入，所以继承官方 PageController，只把 clickElement / inputText
 *      两个方法改成「把元素中心坐标交给 Python，由 Playwright 真实鼠标键盘执行」。
 *   2) LLM 请求也走队列（在胶水 page_agent_glue.js 里接 customFetch），
 *      所以 API Key 永远不进页面。
 *
 * 依赖的官方内部细节（升级 page-agent 版本时要重新确认）：
 *   · PageController 的 `selectorMap`（TS 里是 private，运行时就是普通属性）：index → 元素
 *   · 内置工具 click_element_by_index / input_text 内部调 pageController.clickElement / inputText
 * 拿不到元素时一律退回官方实现（super.xxx），功能不会因为内部改名而崩掉。
 */
import { PageAgentCore } from '@page-agent/core'
import { PageController } from '@page-agent/page-controller'
import * as z from 'zod/v4'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 把「页面内动作」交给桌面端执行的控制器：坐标算在页面里，点击/输入在 Python 侧。 */
export class RpaPageController extends PageController {
  /** index → 元素（selectorMap 是官方私有字段，取不到就返回 null 由调用方兜底）。 */
  _el(index) {
    try {
      const node = this.selectorMap && this.selectorMap.get(index)
      const el = node && node.ref
      return el instanceof HTMLElement ? el : null
    } catch (e) {
      return null
    }
  }

  /** 元素中心（视口 CSS 像素），顺带滚进视野。 */
  async _center(index) {
    const el = this._el(index)
    if (!el) return null
    try {
      el.scrollIntoView({ block: 'center', inline: 'nearest' })
    } catch (e) {
      /* 滚不动就算了，坐标还拿得到 */
    }
    await sleep(120 + Math.random() * 160)
    const r = el.getBoundingClientRect()
    if (!r || r.width <= 0 || r.height <= 0) return null
    return {
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      tag: String(el.tagName || '').toLowerCase(),
    }
  }

  async clickElement(index) {
    const p = await this._center(index)
    if (!p) {
      console.warn('[rpa] 取不到元素坐标，退回官方页面内点击：index=' + index)
      return super.clickElement(index)
    }
    const res = await window.__rpaAction.click(p.x, p.y)
    if (!res || !res.ok) {
      return {
        success: false,
        message: `点击 [${index}] 失败：${(res && res.error) || '桌面端没有执行'}`,
      }
    }
    await sleep(120 + Math.random() * 120)
    return { success: true, message: `✅ 已用真实鼠标点击 [${index}] <${p.tag}>` }
  }

  async inputText(index, text) {
    const p = await this._center(index)
    if (!p) {
      console.warn('[rpa] 取不到元素坐标，退回官方页面内输入：index=' + index)
      return super.inputText(index, text)
    }
    const res = await window.__rpaAction.type(p.x, p.y, String(text ?? ''))
    if (!res || !res.ok) {
      return {
        success: false,
        message: `输入失败：${(res && res.error) || '桌面端没有执行'}`,
      }
    }
    await sleep(120 + Math.random() * 120)
    return {
      success: true,
      message: `✅ 已用真实键盘输入 ${String(text ?? '').length} 个字符到 [${index}]`,
    }
  }
}

// 胶水（page_agent_glue.js）会从这里取类来实例化
window.RpaBundle = { PageAgentCore, PageController, RpaPageController, z }