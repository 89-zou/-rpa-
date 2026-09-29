/**
 * 构建脚本：在本目录下跑 `node build.mjs`
 *
 * 产物：../page_agent.iife.js（提交进仓库；运行时由 Python 读出来注入页面）
 * 依赖：@page-agent/core + @page-agent/page-controller（锁 1.12.4）+ zod + esbuild
 *
 * 重建步骤（改了 entry.js 或要升级 page-agent 时）：
 *     cd rpa_page_agent/core/page_agent/bundle
 *     npm install          # 第一次，或改了 package.json
 *     node build.mjs
 * 升版本时记得：改 package.json 里的版本 → 重新构建 → 跑一遍
 * projects/_verify_1 里的注入自检脚本（确认 window.RpaBundle 还在）。
 */
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const outfile = path.join(here, '..', 'page_agent.iife.js')

const banner = `/* 小邹RPA（rpa_page_agent）用的 page-agent 产物
 * 来源：@page-agent/core + @page-agent/page-controller v1.12.4（MIT License，Alibaba Group）
 *       + zod v4；用 bundle/build.mjs 打包成 IIFE，暴露 window.RpaBundle
 * 别手改这个文件：改 bundle/entry.js 后重新构建（见 bundle/build.mjs 顶部说明） */`

await build({
  entryPoints: [path.join(here, 'entry.js')],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['chrome110'],
  minify: true,
  legalComments: 'none',
  outfile,
  banner: { js: banner },
})

console.log('写好 ' + outfile)