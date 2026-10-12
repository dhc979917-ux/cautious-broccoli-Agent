// README artwork: render the real frontend with synthetic data, then add a frame.
// Run from the repository root after `npm run build --prefix frontend`.
// Install the screenshot dependency with `npm install --prefix test`.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, mkdir } from 'node:fs/promises'
import { dirname, resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || '../../test/node_modules/playwright/index.mjs')
const output = dirname(fileURLToPath(import.meta.url))
const dist = resolve(output, '../../frontend/dist')
await mkdir(output, { recursive: true })
const server = createServer(async (req, res) => {
  const path = resolve(dist, decodeURIComponent(new URL(req.url, 'http://localhost').pathname).slice(1))
  if (path !== dist && !path.startsWith(dist + sep)) { res.writeHead(403); res.end(); return }
  const types = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' }
  try { res.setHeader('Content-Type', types[extname(path)] || 'text/html'); res.end(await readFile(path)) }
  catch { res.setHeader('Content-Type', 'text/html'); res.end(await readFile(resolve(dist, 'index.html'))) }
})
await new Promise(done => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const user = { id: 1, username: '演示用户', account: 'demo', role: 'admin', agent_permissions: [], permissions: [], must_change_password: false }
const agents = [
  { id: 1, name: '工具助手', description: '文档处理、报告生成与日常工具任务', agent_type: 'dialog' },
  { id: 2, name: '数据分析师', description: '从数据探索到图表分析，让结论有据可依', agent_type: 'task' },
  { id: 3, name: '知识助手', description: '结合企业知识库，检索文档并辅助问答', agent_type: 'dialog' },
].map(a => ({ ...a, is_active: true, model_config_name: 'qwen-plus', created_at: '2026-10-01T08:00:00Z' }))
const resources = [
  ['文档办公套件', 'package', '处理 PDF、Word、Excel 与演示文稿，将文件任务串联成可复用的工作流程。'],
  ['数据分析', 'skill', '整理表格、分析趋势与异常，输出可复现的图表和分析报告。'],
  ['知识检索', 'skill', '检索企业文档与参考资料，为问答提供上下文和来源线索。'],
  ['报告生成', 'skill', '组织资料与分析结论，生成结构清晰的报告与交付文件。'],
  ['文本翻译', 'skill', '辅助中英文内容转换，统一专业术语和文档表达。'],
  ['格式转换', 'skill', '在 Markdown、JSON 与表格之间转换内容，减少重复整理。'],
].map(([title, kind, summary], i) => ({ id: `demo-${i}`, title, kind, summary, author: '演示技能库', origin: 'system', allow_fork: true, archived: false, blocked: false, revision: 0, created_at: '2026-10-01T08:00:00Z', published_version_id: `v-${i}` }))
const shots = [
  { name: 'workspace', route: '/', ready: '.home-display', eyebrow: '01 / WORKSPACE', title: '一句提示，开启智能协作', subtitle: '统一工作台 · Agent 选择 · 技能与知识入口', tint: '#0f766e' },
  { name: 'skills', route: '/skills', ready: '.hub-resource', eyebrow: '02 / SKILL CENTER', title: '让专业能力，成为可复用技能', subtitle: '技能中心 · 个人创作 · 版本与分享', tint: '#4338ca' },
  { name: 'agents', route: '/admin/agents', ready: '.ant-table-row', eyebrow: '03 / AGENT MANAGEMENT', title: '集中配置，协同管理', subtitle: 'Agent 管理 · 模型配置 · 资源绑定', tint: '#0369a1' },
]
let browser
try {
  browser = await chromium.launch({ headless: true, ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) })
  for (const shot of shots) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    await page.addInitScript(user => {
      localStorage.setItem('auth-storage', JSON.stringify({ state: { token: 'readme-demo-only', user, theme: 'light', motionEffect: false, selectedAgentId: 1, selectedSkills: [], selectedMcps: null }, version: 6 }))
    }, user)
    // Every API request is fulfilled locally; no backend or real credentials are used.
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '').replace(/\/$/, '')
      let data = []
      if (path === '/auth/me') data = user
      else if (path === '/admin/access') data = { can_view_people: true, can_manage: true }
      else if (path.startsWith('/notifications')) data = { items: [], unread: 0 }
      else if (path === '/agents' || path === '/agents/all') data = agents
      else if (path === '/skill-hub/system') data = resources
      else if (path === '/models') data = { models: ['qwen-plus'], default: 'qwen-plus', display_names: { 'qwen-plus': '通义千问 Plus' } }
      else if (path === '/models/policies') data = { models: ['qwen-plus'], policies: [] }
      else if (path.endsWith('/versioning')) data = { enabled: false }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
    })
    await page.goto(base + shot.route, { waitUntil: 'networkidle' })
    await page.locator(shot.ready).first().waitFor()
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(900)
    assert.deepEqual(errors, [], `${shot.name}: frontend render errors`)
    const screenshot = await page.screenshot()
    // The original UI pixels are preserved inside the decorative browser frame.
    await page.setViewportSize({ width: 1600, height: 1220 })
    await page.setContent(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
      *{box-sizing:border-box}body{margin:0;width:1600px;height:1220px;color:#ecfdf5;font-family:Arial,'PingFang SC',sans-serif;background:radial-gradient(ellipse at 95% 0%,${shot.tint}88,transparent 55%),radial-gradient(ellipse at 0% 100%,#115e5955,transparent 55%),#0b1220}
      body:before{content:'';position:absolute;inset:0;background-image:linear-gradient(#ffffff04 1px,transparent 1px),linear-gradient(90deg,#ffffff04 1px,transparent 1px);background-size:48px 48px}
      header{position:relative;margin:54px 80px 34px;display:flex;justify-content:space-between;align-items:center}.eyebrow{font-size:15px;letter-spacing:3px;color:#5eead4}h1{font-size:38px;margin:12px 0 10px;font-weight:600;letter-spacing:1px}p{font-size:17px;color:#a4b6ca;margin:0}.brand{color:#d5f4ec;font-size:21px;letter-spacing:1px}.brand small{display:block;text-align:right;color:#8398ae;font-size:12px;margin-top:10px;letter-spacing:2px}
      .window{position:relative;margin:0 80px;border:1px solid #ffffff26;border-radius:16px;overflow:hidden;box-shadow:0 30px 80px #0007;background:#f7f9fc}.bar{height:44px;background:#edf1f5;display:flex;align-items:center;padding:0 18px;gap:8px}.dot{width:10px;height:10px;border-radius:50%;background:#fb7185}.dot:nth-child(2){background:#fbbf24}.dot:nth-child(3){background:#34d399}.address{margin:auto;color:#66758a;font-size:12px;letter-spacing:1px;padding-right:45px}.window img{display:block;width:1438px;height:899px;object-fit:fill}footer{position:relative;margin:20px 82px;display:flex;justify-content:space-between;font-size:12px;color:#7f96ad;letter-spacing:1px}
      </style><header><div><div class="eyebrow">${shot.eyebrow}</div><h1>${shot.title}</h1><p>${shot.subtitle}</p></div><div class="brand">TaskPilot<small>ENTERPRISE AGENT PLATFORM</small></div></header><div class="window"><div class="bar"><i class="dot"></i><i class="dot"></i><i class="dot"></i><span class="address">TaskPilot / ${shot.name}</span></div><img alt="真实前端界面，使用演示数据" src="data:image/png;base64,${screenshot.toString('base64')}"></div><footer><span>真实前端界面 · 演示数据</span><span>AGENTSCOPE × FASTAPI × REACT</span></footer></html>`)
    await page.evaluate(() => Promise.all([...document.images].map(img => img.decode())))
    await page.screenshot({ path: resolve(output, `showcase-${shot.name}.png`) })
    console.log(`Generated showcase-${shot.name}.png`)
    await page.close()
  }
} finally {
  await browser?.close()
  await new Promise(done => server.close(done))
}
