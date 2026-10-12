import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, mkdir } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || '../../test/node_modules/playwright/index.mjs')
const dist = resolve(process.env.HARNESS_UI_DIST || '._memory52/frontend/dist')
const artifacts = resolve(process.env.HARNESS_ARTIFACTS || '._memory52/artifacts')
await mkdir(artifacts, { recursive: true })
const server = createServer(async (request, response) => {
  const path = resolve(dist, decodeURIComponent(new URL(request.url, 'http://localhost').pathname).slice(1))
  assert(path === dist || path.startsWith(dist + sep))
  try { response.setHeader('Content-Type', { '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml' }[extname(path)] || 'text/html'); response.end(await readFile(path)) }
  catch { response.setHeader('Content-Type', 'text/html'); response.end(await readFile(resolve(dist, 'index.html'))) }
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const admin = { id: 1, username: '记忆管理员', account: 'qa-memory', role: 'admin', permissions: [], agent_permissions: [], must_change_password: false }
const markdown = '# 测试记忆标题\n\n**偏好中文回复**\n\n- 关注交付时间\n- 关注来源依据\n\n| 项目 | 内容 |\n| --- | --- |\n| 表达 | 简洁清晰 |\n\n<script>window.memoryUnsafe=true</script>\n![外部图片](https://example.invalid/tracker.png)'
const browser = await chromium.launch({ headless: true })
const errors = []
try {
  for (const [name, viewport, theme] of [['desktop', { width: 1440, height: 1000 }, 'light'], ['mobile', { width: 390, height: 844 }, 'light'], ['dark', { width: 1440, height: 1000 }, 'dark']]) {
    const context = await browser.newContext({ viewport })
    const page = await context.newPage()
    page.setDefaultTimeout(12000)
    page.on('pageerror', (cause) => errors.push(cause.message))
    await page.addInitScript(({ admin, theme }) => localStorage.setItem('auth-storage', JSON.stringify({ state: { token: 'synthetic', user: admin, theme, motionEffect: false }, version: 6 })), { admin, theme })
    const calls = []
    let failure = false
    let requiresReason = false
    const task = { id: 'task-a', user_id: 2, source_type: 'manual', method_code: 'summary', status: 'review', revision: 0, error_code: null, created_at: '2026-10-08T03:00:00Z', sources: [{ id: 'entry-a', content: markdown, path: 'daily/2026-10-08/fact.md', source_type: 'auto_session' }], items: [{ id: 'item-a', content: markdown, status: 'pending', revision: 0, source_ids: ['entry-a'], evidence: [{ source_id: 'entry-a', quote: '**偏好中文回复**' }], conflict_group: null }] }
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request(); const url = new URL(request.url()); const path = url.pathname.slice(7); const body = request.postDataJSON()
      calls.push({ path, method: request.method(), body, query: url.search })
      const json = (result, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(result) })
      if (path === '/auth/me') return json(admin)
      if (path === '/notifications/') return json({ items: [], unread: 0 })
      if (path === '/admin/access') return json({ can_view_people: true, can_manage: true })
      if (path === '/admin/memories/users') return json({ items: [{ id: 2, username: '测试人员', account: 'qa-person' }] })
      if (path === '/admin/memories/access') return json({ create: true, review: true, publish: true })
      if (path === '/admin/memories/entries') return json({ items: [{ id: 'entry-a', path: 'daily/2026-10-08/fact.md', source_type: 'auto_session', status: 'published', revision: 0, created_at: task.created_at }], total: 1 })
      if (path === '/admin/memories/entries/entry-a') return json({ ...task.sources[0], sha256: 'synthetic' })
      if (path === '/admin/memories/methods') return json([{ code: 'summary', title: '重点摘要', revision: 1 }, { code: 'dedupe', title: '合并去重', revision: 1 }])
      if (path === '/admin/memories/tasks' && request.method() === 'POST') return json(task)
      if (path === '/admin/memories/tasks') return json({ items: [task], total: 1 })
      if (path === '/admin/memories/tasks/task-a') return json({ ...task, review_access: { can_review: true, can_publish: true, requires_reason: requiresReason, reason_recorded: false } })
      if (path.endsWith('/items/item-a')) {
        if (failure) return json({ detail: '测试：审核暂未完成，请重试。' }, 409)
        task.items[0].status = body.decision; task.items[0].revision++; task.revision++; return json(task.items[0])
      }
      if (path.endsWith('/publish')) { task.status = 'completed'; return json(task) }
      return json([])
    })
    await page.goto(base + '/admin/memories?user_id=2', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '用户记忆', exact: true }).waitFor().catch(async (cause) => { console.log('UI diagnostics', await page.locator('body').innerText(), errors, calls.map((call) => call.path)); await page.screenshot({ path: resolve(artifacts, 'failure.png') }); throw cause })
    assert.equal(await page.getByRole('tab', { name: /记忆内容/ }).count(), 1)
    assert((await page.locator('.memory-page').innerText()).includes('会话记忆'))
    assert(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2))
    await page.screenshot({ path: resolve(artifacts, `memory-${name}.png`), fullPage: true })
    await page.getByRole('button', { name: '查看', exact: true }).click()
    await page.locator('.memory-markdown h1').getByText('测试记忆标题').waitFor()
    assert.equal(await page.locator('.memory-markdown strong').filter({ hasText: '偏好中文回复' }).count(), 1)
    assert.equal(await page.locator('.memory-markdown table').count(), 1)
    assert.equal(await page.locator('.memory-markdown img').count(), 0)
    assert.equal(await page.evaluate(() => window.memoryUnsafe), undefined)
    await page.locator('.ant-drawer-close').click()
    await page.locator('.ant-drawer-open').waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: /萃取记忆$/ }).click()
    await page.getByText('请勾选需要整理的记忆，再点击“萃取记忆”。', { exact: true }).waitFor()
    await page.locator('.ant-table-tbody .ant-table-row input[type=checkbox]').check()
    await page.getByRole('button', { name: /萃取记忆$/ }).click()
    await page.getByRole('dialog').getByText('重点摘要', { exact: true }).waitFor()
    await page.getByRole('button', { name: '开始萃取', exact: true }).click()
    await page.locator('.ant-modal').waitFor({ state: 'hidden' })
    await page.locator('.memory-candidate h1').waitFor()
    const create = calls.find((call) => call.method === 'POST' && call.path === '/admin/memories/tasks')
    assert.equal(create.body.method_code, 'summary'); assert.equal(create.body.idempotency_key.length, 32)
    await page.screenshot({ path: resolve(artifacts, `review-${name}.png`), fullPage: true })
    if (name === 'desktop') {
      failure = true
      await page.getByRole('button', { name: /^接\s*受$/ }).click()
      await page.locator('.memory-overlay .ant-alert').getByText('测试：审核暂未完成，请重试。', { exact: true }).waitFor()
      failure = false; requiresReason = true
      await page.locator('.ant-drawer-open').getByRole('button', { name: /^刷\s*新$/ }).click()
      await page.getByRole('button', { name: /^接\s*受$/ }).click()
      await page.getByLabel('管理员自审说明（至少10字）').fill('已核对来源，此为合成测试的管理员审批说明')
      await page.getByRole('button', { name: '确认审核', exact: true }).click()
    } else await page.getByRole('button', { name: /^接\s*受$/ }).click()
    await page.getByText('已接受', { exact: true }).waitFor()
    for (const review of calls.filter((call) => call.method === 'PUT')) assert(!Object.hasOwn(review.body, 'content'), 'ordinary acceptance must not resubmit source')
    await page.getByRole('button', { name: '确认发布', exact: true }).click()
    await page.locator('.memory-task-meta').getByText('已完成', { exact: true }).waitFor()
    await context.close()
    console.log('PASS', name, 'Chinese UI, extraction HTTP compatibility, rendered Markdown, acceptance, publish')
  }
  assert.deepEqual(errors, [])
} finally { await browser.close(); server.close() }
