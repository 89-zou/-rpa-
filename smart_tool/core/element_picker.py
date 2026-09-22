# -*- coding: utf-8 -*-
"""元素捕获器（网页）：在页面里点一下，同时拿到「稳健 XPath」和「元素截图」。

怎么用：工具用 Playwright 打开一个可见的浏览器窗口，往每个页面注入 `PICKER_JS`，
它会在你划过元素时画出橙框、并实时显示「这个选择器命中几个」；点一下就把结果
通过 `window.__trae_pick` 回传给 Python。

「打断 / 恢复」靠一个开关：脚本里的监听只装一次，之后由 `window.__traePickerOn`
决定它们干不干活。捕获中＝true（划过画框、点击吃掉并捕获）；打断＝false
（监听还在但直接放行，用户能正常翻页、登录、展开菜单）。这样打断不用拆监听、
恢复不用重装，来回切不会漏事件。

配套的三个小脚本：
- `ARM_JS` / `DISARM_JS`：就是把这个开关拨过去（外加收起浮层）
- `HIGHLIGHT_JS`：元素校验用——把元素滚到可见处、画个绿框，并返回命中几个
- `TOAST_JS`：把一句话贴在页面顶端（校验结果、出错提示都走它）

XPath 生成策略（从稳到糙，逐级退让）：
1. 元素自己的 id 唯一        → //*[@id="xxx"]
2. 最近的「带 id 的祖先」做锚点 → //*[@id="anc"]/div[2]/span[1]
3. 稳定属性唯一（name / placeholder / aria-label / href …）
4. 短文本唯一               → //a[normalize-space(text())="登录"]
5. 从 /html 逐级的完整路径（一定唯一，但页面一改就废）

元素截图交给 Playwright 的 `locator.screenshot()`：它裁的就是元素精确边框，
比手工裁剪准，也不受滚动/缩放影响。

注：属性值里带英文双引号的一律跳过——XPath 1.0 的字面量没法转义引号，
硬拼出来的选择器是废的，不如直接退到下一级策略。
"""
from pathlib import Path

# 截图文件名前缀（存进项目的 img/ 目录）
SHOT_PREFIX = "cap_"

# 注入到每个页面（含 iframe）的选择器脚本
PICKER_JS = r"""
(function () {
  if (window.__traePickerReady) { return; }
  window.__traePickerReady = true;

  var STYLE_BOX = 'position:fixed;z-index:2147483646;pointer-events:none;display:none;'
    + 'border:2px solid #f08a24;background:rgba(240,138,36,.14);'
    + 'box-sizing:border-box;border-radius:2px';
  var STYLE_TIP = 'position:fixed;z-index:2147483647;pointer-events:none;display:none;'
    + 'background:#1f2937;color:#fff;font:12px/1.5 Consolas,Menlo,monospace;'
    + 'padding:4px 7px;border-radius:4px;max-width:72vw;white-space:pre-wrap;'
    + 'box-shadow:0 2px 8px rgba(0,0,0,.35)';

  // 点中之后「盖章」用的两个浮层：绿色粗框 + 屏幕顶端的大提示条。
  // 为什么要有它们：捕获器自己的窗口被浏览器盖住了，只更新那边的文字，
  // 用户在页面里什么都看不到，新手会以为「点了没反应」。
  var STYLE_OK = 'position:fixed;z-index:2147483646;pointer-events:none;display:none;'
    + 'border:3px solid #16a34a;background:rgba(22,163,74,.16);'
    + 'box-sizing:border-box;border-radius:3px';
  var STYLE_TOAST = 'position:fixed;z-index:2147483647;pointer-events:none;'
    + 'left:50%;top:14px;transform:translateX(-50%);max-width:86vw;'
    + 'background:#15803d;color:#fff;font:13px/1.7 "Microsoft YaHei",sans-serif;'
    + 'padding:9px 16px;border-radius:8px;white-space:pre-wrap;text-align:center;'
    + 'box-shadow:0 6px 20px rgba(0,0,0,.4)';

  // 四个浮层都挂在 window 上：连着抓下一个时会重新装填脚本，复用同一批节点，
  // 免得每抓一次就往页面里多贴四个 div。
  function layer(key, css) {
    var name = '__traeLayer_' + key;
    if (!window[name]) {
      var el = document.createElement('div');
      el.style.cssText = css;
      window[name] = el;
    }
    return window[name];
  }

  var box = layer('box', STYLE_BOX);
  var tip = layer('tip', STYLE_TIP);
  var okBox = layer('ok', STYLE_OK);
  var toast = layer('toast', STYLE_TOAST);

  function mount() {
    var root = document.documentElement;
    if (!root) { return; }
    // 四个都是「贴上去就不管」的浮层：页面自己重画 DOM 时（SPA 切页）要补回去
    var layers = [box, tip, okBox, toast];
    for (var i = 0; i < layers.length; i++) {
      if (!root.contains(layers[i])) { root.appendChild(layers[i]); }
    }
  }
  mount();
  window.__traeMountTimer = setInterval(mount, 800);

  function tagOf(el) { return (el.tagName || '').toLowerCase(); }

  function countOf(p) {
    try {
      var r = document.evaluate(p, document, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      return r.snapshotLength;
    } catch (e) { return -1; }
  }

  function nth(el) {
    var i = 1, s = el;
    while ((s = s.previousElementSibling)) {
      if (s.tagName === el.tagName) { i += 1; }
    }
    return i;
  }

  function absolutePath(el) {
    var parts = [], n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      parts.unshift(tagOf(n) + '[' + nth(n) + ']');
      n = n.parentElement;
    }
    parts.unshift('html[1]');
    return '/' + parts.join('/');
  }

  function relativePath(ancestor, el) {
    var parts = [], n = el;
    while (n && n !== ancestor) {
      parts.unshift(tagOf(n) + '[' + nth(n) + ']');
      n = n.parentElement;
    }
    return parts.join('/');
  }

  function usable(v) {
    return v && v.indexOf('"') < 0 && v.indexOf('\n') < 0;
  }

  var ATTRS = ['name', 'data-testid', 'aria-label', 'placeholder',
               'title', 'alt', 'for', 'href'];

  function candidate(el) {
    if (usable(el.id)) {
      var p1 = '//*[@id="' + el.id + '"]';
      if (countOf(p1) === 1) { return { path: p1, why: 'id' }; }
    }
    var anc = el.parentElement;
    while (anc && anc !== document.documentElement) {
      if (usable(anc.id)) {
        var rel = relativePath(anc, el);
        if (rel) {
          var p2 = '//*[@id="' + anc.id + '"]/' + rel;
          if (countOf(p2) === 1) { return { path: p2, why: '锚点 #' + anc.id }; }
        }
      }
      anc = anc.parentElement;
    }
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      var v = el.getAttribute ? el.getAttribute(a) : null;
      if (!usable(v)) { continue; }
      // href="#" / javascript:… 这类空壳链接不算「稳定属性」，退回按文本找
      if (a === 'href' && (v.charAt(0) === '#' || v.indexOf('javascript:') === 0)) {
        continue;
      }
      var p3 = '//' + tagOf(el) + '[@' + a + '="' + v + '"]';
      if (countOf(p3) === 1) { return { path: p3, why: '@' + a }; }
    }
    var text = (el.textContent || '').trim();
    if (usable(text) && text.length <= 30 && el.children.length === 0) {
      var p4 = '//' + tagOf(el) + '[normalize-space(text())="' + text + '"]';
      if (countOf(p4) === 1) { return { path: p4, why: '文本' }; }
    }
    return { path: absolutePath(el), why: '完整路径' };
  }

  function labelOf(el) {
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    var s = '<' + tagOf(el) + '>';
    if (el.id) { s += '#' + el.id; }
    if (t) { s += ' “' + t + '”'; }
    return s;
  }

  function draw(el, cand) {
    var r = el.getBoundingClientRect();
    box.style.display = 'block';
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
    var hits = countOf(cand.path);
    tip.textContent = labelOf(el) + '\n' + cand.path + '\n'
      + '命中 ' + hits + ' 个（' + cand.why + '）'
      + (hits === 1 ? '' : '  ← 不唯一，注意核对')
      + '\n左键点一下＝捕获这个元素';
    tip.style.display = 'block';
    var top = r.top > 62 ? r.top - 52 : r.bottom + 8;
    tip.style.left = Math.max(4, Math.min(r.left, window.innerWidth - 120)) + 'px';
    tip.style.top = Math.max(4, Math.min(top, window.innerHeight - 46)) + 'px';
  }

  function hide() {
    box.style.display = 'none';
    tip.style.display = 'none';
  }

  function stamp(el, cand, hits) {
    /* 点中之后在页面里「盖个章」：绿框留住 3 秒 + 顶端弹一条绿提示。
       捕获器自己的窗口在浏览器下面，用户看不见，所以反馈必须画在页面里。
       绿框与提示条是常驻的两个浮层（见上面的 layer/mount），这里只改位置和文字。
       计时器也挂在 window 上：重新装填脚本后还能取消上一轮的手，免得提前收掉。 */
    try {
      window.__traePickTotal = (window.__traePickTotal || 0) + 1;
      var r = el.getBoundingClientRect();
      okBox.style.left = r.left + 'px';
      okBox.style.top = r.top + 'px';
      okBox.style.width = r.width + 'px';
      okBox.style.height = r.height + 'px';
      okBox.style.display = 'block';
      toast.textContent = '✓ 已捕获第 ' + window.__traePickTotal + ' 个：' + labelOf(el)
        + '\nXPath：' + (cand.path || '（生成失败）') + '　命中 ' + hits + ' 个'
        + (hits === 1 ? '' : '（不唯一，建议重抓一个更准的）')
        + '\n点控制器上的【结束】即可写进步骤；想先自己操作页面就点【打断】';
      toast.style.display = 'block';
      if (window.__traeOkTimer) { clearTimeout(window.__traeOkTimer); }
      window.__traeOkTimer = setTimeout(function () {
        okBox.style.display = 'none';
        toast.style.display = 'none';
      }, 3000);
    } catch (err) { /* 反馈画不出来也不影响捕获本身 */ }
  }

  function disarm() {
    window.__traePickerReady = false;
    window.__traePickerOn = false;          // 拨到「打断」：监听还在，但一律放行
    if (window.__traeMountTimer) { clearInterval(window.__traeMountTimer); }
    removeEventListener('mousemove', onMove, true);
    removeEventListener('click', onClick, true);
    removeEventListener('keydown', onKey, true);
    hide();
  }

  var lastEl = null, lastAt = 0;

  function onMove(e) {
    // 打断状态：什么都不做，让鼠标事件正常传给页面
    if (!window.__traePickerOn) { return; }
    var el = e.target;
    if (!el || el.nodeType !== 1 || el === box || el === tip) { return; }
    var now = Date.now();
    if (el === lastEl && now - lastAt < 300) { return; }
    lastEl = el;
    lastAt = now;
    try { draw(el, candidate(el)); } catch (err) { hide(); }
  }

  function onClick(e) {
    // 打断状态：不吞事件，用户就是在正常操作网页
    if (!window.__traePickerOn) { return; }
    var el = e.target;
    if (!el || el.nodeType !== 1) { return; }
    e.preventDefault();
    e.stopPropagation();
    var cand;
    try { cand = candidate(el); } catch (err) { cand = { path: '', why: '生成失败' }; }
    var r = el.getBoundingClientRect();
    var hits = countOf(cand.path);
    var payload = {
      xpath: cand.path,
      why: cand.why,
      desc: labelOf(el),
      count: hits,
      top: window.top === window,
      frame: location.href,
      rect: [r.left, r.top, r.width, r.height]
    };
    stamp(el, cand, hits);
    disarm();
    if (typeof window.__trae_pick === 'function') { window.__trae_pick(payload); }
  }

  function onKey(e) {
    if (e.key === 'Escape') { hide(); }
  }

  addEventListener('mousemove', onMove, true);
  addEventListener('click', onClick, true);
  addEventListener('keydown', onKey, true);
})();
"""


#: 【恢复】把开关拨回「捕获中」——划过画框、点击吃掉并捕获
ARM_JS = r"""
  () => { window.__traePickerOn = true; return true; }
"""

#: 【打断】把开关拨到「放行」，顺手收起橙框与提示条
#: （故意不动 ok / toast 两个浮层：校验结果可能还挂在页面上）
DISARM_JS = r"""
  () => {
    try {
      window.__traePickerOn = false;
      var b = window.__traeLayer_box, t = window.__traeLayer_tip;
      if (b) { b.style.display = 'none'; }
      if (t) { t.style.display = 'none'; }
      return true;
    } catch (e) { return false; }
  }
"""

#: 【校验元素】把元素滚到可见处 + 画绿框，并回传「命中几个」。
#: 返回值：>0 命中个数；0 = 页面上找不到；-1 = XPath 写错了。
#: 命中几个跟「能不能用」是两件事：命中多个说明这个写法不够准，得改。
HIGHLIGHT_JS = r"""
  (xpath) => {
    try {
      var r = document.evaluate(xpath, document, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      var n = r.snapshotLength;
      if (n < 1) { return 0; }
      var el = r.snapshotItem(0);
      if (el.scrollIntoView) { el.scrollIntoView({block: 'center'}); }
      var ok = window.__traeLayer_ok;
      if (ok) {
        var b = el.getBoundingClientRect();
        ok.style.left = b.left + 'px';
        ok.style.top = b.top + 'px';
        ok.style.width = b.width + 'px';
        ok.style.height = b.height + 'px';
        ok.style.display = 'block';
        if (window.__traeCheckTimer) { clearTimeout(window.__traeCheckTimer); }
        window.__traeCheckTimer = setTimeout(function () {
          ok.style.display = 'none';
        }, 2500);
      }
      return n;
    } catch (e) { return -1; }
  }
"""

#: 把一句话贴在页面顶端（校验结果、出错提示走它）。
#: 刻意做成自给自足：浮层不在就自己建一个 —— 反馈画在页面上，
#: 用户才不会盯着控制器那一行小字猜页面里到底发生了什么。
TOAST_JS = r"""
  (payload) => {
    try {
      var t = window.__traeLayer_toast;
      if (!t) {
        t = document.createElement('div');
        t.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;'
          + 'left:50%;top:14px;transform:translateX(-50%);max-width:86vw;'
          + 'color:#fff;font:13px/1.7 "Microsoft YaHei",sans-serif;'
          + 'padding:9px 16px;border-radius:8px;white-space:pre-wrap;text-align:center;'
          + 'box-shadow:0 6px 20px rgba(0,0,0,.4)';
        window.__traeLayer_toast = t;
      }
      if (!document.documentElement.contains(t)) {
        document.documentElement.appendChild(t);
      }
      t.textContent = payload.text || '';
      t.style.background = payload.ok ? '#15803d' : '#b45309';
      t.style.display = 'block';
      if (window.__traeToastTimer) { clearTimeout(window.__traeToastTimer); }
      window.__traeToastTimer = setTimeout(function () {
        t.style.display = 'none';
      }, 3500);
      return true;
    } catch (e) { return false; }
  }
"""


def next_shot_path(img_dir: Path, stamp: str) -> Path:
    """给这次捕获起一个不重名的截图路径：img/cap_20260917_203512.png。"""
    return Path(img_dir) / f"{SHOT_PREFIX}{stamp}.png"
