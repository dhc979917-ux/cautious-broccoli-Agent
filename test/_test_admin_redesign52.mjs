import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdir, readFile } from 'node:fs/promises'
import { resolve, extname, sep, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || './node_modules/playwright/index.mjs')

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = resolve(process.env.HARNESS_UI_DIST || resolve(root, 'frontend/dist'))
const artifacts = resolve(process.env.HARNESS_ARTIFACTS || resolve(root, 'artifacts/admin-redesign52'))
await mkdir(artifacts, { recursive: true })
const server = createServer(async (request, response) => {
  const path = resolve(dist, decodeURIComponent(new URL(request.url, 'http://localhost').pathname).slice(1))
  assert(path.startsWith(dist + sep) || path === dist)
  const mime = { '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml' }
  try { response.setHeader('Content-Type', mime[extname(path)] || 'text/html'); response.end(await readFile(path)) }
  catch { response.setHeader('Content-Type', 'text/html'); response.end(await readFile(resolve(dist, 'index.html'))) }
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const admin = { id: 1, username: '测试管理员', account: 'synthetic-admin', role: 'admin', permissions: [], agent_permissions: [], must_change_password: false }
const person = { id: 3, username: '测试人员', account: 'synthetic-person', email: 'qa@example.invalid', phone: '13800000000', role: 'viewer', is_active: true,
  department_id: 2, department_name: '测试部门', job_title: '工程师', external_id: null, source: 'local', employment_status: 'active', revision: 0,
  created_at: '2026-10-08T01:00:00Z', last_login: '2026-10-08T02:00:00Z', agent_ids: [7], skill_grants: { skill: [9], package: [] }, roles: [], usage: { sessions: 2, messages: 4 }, can_edit: true }
const department = { id: 2, name: '测试部门', code: 'qa52_department', is_active: true, parent_id: null, revision: 0 }
const model = { model_id: 'synthetic-model', display_name: '企业通用模型', enabled: true, in_gateway: true, authorized_scopes: [], revision: 0 }
const agent = { id: 7, name: '测试助手', description: '测试助手说明', agent_type: 'dialog', model_config_name: 'synthetic-model', is_active: true, created_at: '2026-10-08T00:00:00Z' }
const skill = { id: 9, name: 'synthetic-skill', display_name: '测试技能', description: '测试技能说明', required_role: 'viewer', is_active: 1, created_at: '2026-10-08T00:00:00Z', has_access: true }
const snapshot = { name: agent.name, description: agent.description, agent_type: 'dialog', system_prompt: 'Keep all original resource bindings.', model_config_name: 'synthetic-model',
  config: { mcp_servers: [4], knowledge_bases: [6], builtin_skills: ['synthetic-skill'], _skill_versions: { '9': 'locked-skill-id' } }, tool_grants: [{ server_id: 4, tool_name: 'read_record', schema_digest: 'fixed-schema' }], memory_policy: 'candidate_review' }
const history = { published_version_id: 'published-id', revision: 0, versioning_enabled: true, versions: [{ id: 'published-id', agent_id: 7, number: 1, state: 'published', snapshot, revision: 0, test_status: 'passed' }], history: [] }
const gateway = { models: ['synthetic-model', 'other-model'], policies: [] }
const browser = await chromium.launch({ headless: true })
const errors = []
async function prepare(page, user = admin, theme = 'light') {
  const calls = []
  const localHistory = structuredClone(history)
  page.setDefaultTimeout(12000)
  await page.addInitScript(({ user, theme }) => localStorage.setItem('auth-storage', JSON.stringify({ state: { token: 'synthetic-only', user, theme, motionEffect: false }, version: 6 })), { user, theme })
  page.on('pageerror', (cause) => errors.push(cause.message))
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.slice('/api/v1'.length)
    const body = request.postDataJSON()
    calls.push({ path, method: request.method(), query: url.search, body })
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
    if (path === '/auth/me') return json(user)
    if (path === '/admin/access') return json({ can_view_people: user.role === 'admin', can_manage: user.role === 'admin' })
    if (path === '/admin/departments') return json([department])
    if (path === '/admin/role-templates') return json([])
    if (path === '/admin/people') return json({ items: [person], total: 1, page: 1, page_size: 20 })
    if (path === '/admin/people/3') return json(person)
    if (path === '/users/') return json([person])
    if (path === '/users/3' && request.method() === 'PUT') return json({ ...person, ...body })
    if (path === '/auth/register') return json({ user: person })
    if (path === '/users/skill-grant-options') return json({ skills: [{ id: 9, label: '测试技能' }], packages: [] })
    if (path === '/agents/all' || path === '/agents/') return json([agent])
    if (path === '/skills/' || path === '/skills/all') return json([skill])
    if (path === '/skills/packages/all' || path.includes('/skills/upload-jobs')) return json([])
    if (path === '/admin/agents/versioning' || path === '/admin/skills/versioning') return json({ enabled: true })
    if (path === '/mcp/' || path === '/mcp/servers' || path === '/mcp/servers/') return json([{ id: 4, name: '测试服务', type: 'stdio', enabled: true, env: {}, args: [] }])
    if (path === '/knowledge/bases') return json([{ id: 6, name: '测试共享库', scope: 'global' }])
    if (path === '/models/') return json({ models: gateway.models, default: gateway.models[0] })
    if (path === '/models/policies') return json(gateway)
    if (path === '/admin/models/catalog/metadata') return json({ revision: 1 })
    if (path === '/admin/models/catalog') return json({ items: [model], total: 1, stale: false, fetched_at: '2026-10-08T03:00:00Z', gateway_reachable: true })
    if (path === '/admin/agents/7/versions' && request.method() === 'GET') return json(localHistory)
    if (path === '/admin/agents/7/versions' && request.method() === 'POST') {
      const candidate = { ...localHistory.versions[0], id: 'draft-id', number: 2, state: 'draft', test_status: 'untested' }
      localHistory.versions.unshift(candidate); return json(candidate)
    }
    if (path === '/admin/agents/7/versions/draft-id' && request.method() === 'PUT') {
      const candidate = localHistory.versions[0]
      candidate.snapshot = { ...candidate.snapshot, ...body.changes }; candidate.revision++
      return json(candidate)
    }
    if (path === '/admin/agents/7/versions/draft-id/test') {
      localHistory.versions[0].test_status = 'passed'; return json({ passed: true })
    }
    if (path === '/admin/agents/7/versions/draft-id/publish') {
      localHistory.published_version_id = 'draft-id'; localHistory.versions[0].state = 'published'; return json({ published: true })
    }
    if (path === '/admin/mcp/servers/4/catalog') return json([{ name: 'read_record', schema_digest: 'fixed-schema', available: true, risk: 'low' }])
    if (path === '/admin/operations/dashboard') return json({ timezone: 'Asia/Shanghai', resources: { users: 1, agents: 1, skills: 1 }, pending: { memory_review: 2 },
      usage: { agent_replies: 12, model_calls: 18, tool_calls: 3, active_users: 1, tokens: { input: 3200, output: 600, complete_samples: 18, missing_samples: 0 } }, alerts: [], recent: [], updated_at: '2026-10-08T03:00:00Z', samples: {} })
    if (path === '/admin/operations/policies' || path === '/admin/operations/cleanup') return json([])
    if (path === '/admin/operations/pending') return json({ items: [], note: '' })
    if (path === '/admin/audit/events' || path === '/admin/operations/traces') return json({ items: [], total: 0, samples: {} })
    if (path === '/admin/memories/users') return json({ items: [{ id: 3, account: person.account }] })
    if (path === '/admin/memories/entries') return json({ items: [], total: 0 })
    if (path === '/admin/memories/tasks' || path === '/admin/memories/methods') return json([])
    if (path.startsWith('/notifications')) return json({ items: [], unread: 0 })
    return json({})
  })
  return calls
}
try {
  for (const [name, viewport, theme] of [['desktop', { width: 1440, height: 1000 }, 'light'], ['mobile', { width: 390, height: 844 }, 'light'], ['dark', { width: 1440, height: 1000 }, 'dark']]) {
    const page = await browser.newPage({ viewport })
    const calls = await prepare(page, admin, theme)
    await page.goto(base + '/admin', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '管理后台', exact: true }).waitFor()
    assert.deepEqual(await page.locator('.sidebar-nav-label').allTextContents(), ['首页', '开始问答', 'Skill广场', '文件中心', '知识库', 'MCP工具', '监控与运维', '调度任务', '个人信息'])
    assert.equal(await page.locator('.overview-module').count(), 9)
    assert(!((await page.locator('body').innerText()).includes('权限与安全')))
    assert.equal(await page.locator('.overview-module').filter({ hasText: '人员信息' }).count(), 1)
    await page.screenshot({ path: resolve(artifacts, `home-${name}.png`), fullPage: true })
    await page.goto(base + '/admin/people', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '人员信息', exact: true }).waitFor()
    await page.getByRole('button', { name: /测试人员/ }).click()
    await page.getByRole('tab', { name: '账号与登录' }).click()
    await page.waitForTimeout(300)
    await page.getByRole('button', { name: '编辑账号资料' }).click()
    await page.getByLabel('姓名', { exact: true }).fill('测试人员更新')
    await page.locator('.ant-modal').getByRole('button', { name: /^确\s*认$/ }).click()
    await page.waitForTimeout(300)
    assert(calls.some((item) => item.path === '/users/3' && item.body?.username === '测试人员更新'))
    await page.keyboard.press('Escape')
    await page.locator('.ant-modal').waitFor({ state: 'hidden' })
    await page.locator('.ant-drawer-open').waitFor({ state: 'hidden' })
    await page.screenshot({ path: resolve(artifacts, `people-${name}.png`), fullPage: true })
    await page.goto(base + '/admin/models', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '企业模型', exact: true }).waitFor()
    const text = await page.locator('.workspace-content').innerText()
    for (const removed of ['用途', '能力', '价格', '测试', '元数据']) assert(!text.includes(removed), removed)
    await page.getByRole('button', { name: '管理授权' }).click()
    await page.getByRole('button', { name: '允许使用', exact: true }).click()
    await page.waitForTimeout(300)
    assert(calls.some((item) => item.path === '/models/policies' && item.method === 'PUT' && item.body.scope === 'role'))
    await page.locator('.ant-select[aria-label="授权对象类型"]').click()
    await page.getByTitle('人员', { exact: true }).click()
    await page.locator('.ant-select[aria-label="选择授权对象"]').click()
    await page.getByTitle('测试人员 · synthetic-person', { exact: true }).click()
    await page.getByRole('button', { name: '允许使用', exact: true }).click()
    await page.waitForTimeout(300)
    assert(calls.some((item) => item.path === '/models/policies' && item.method === 'PUT' && item.body.scope === 'user' && item.body.principal === '3'))
    await page.waitForFunction(() => !document.querySelector('.ant-message-notice'))
    await page.locator('.ant-drawer-close').click()
    await page.locator('.ant-drawer-open').waitFor({ state: 'hidden' })
    await page.getByRole('switch').click()
    await page.getByRole('dialog').getByRole('button', { name: /^停\s*用$/ }).click()
    await page.waitForTimeout(300)
    assert(calls.some((item) => item.path === '/admin/models/catalog/metadata' && Object.keys(item.body).sort().join(',') === 'enabled,model_id,revision'))
    await page.screenshot({ path: resolve(artifacts, `models-${name}.png`), fullPage: true })
    for (const path of ['/admin/agents', '/admin/skills', '/admin/mcp', '/admin/memories', '/admin/operations', '/admin/audit', '/admin/traces']) {
      await page.goto(base + path, { waitUntil: 'networkidle' })
      assert(!(await page.locator('body').innerText()).includes('应用遇到错误'), path)
      if (['/admin/agents', '/admin/skills'].includes(path)) assert.equal(await page.getByRole('button', { name: '版本', exact: true }).count(), 0)
      await page.screenshot({ path: resolve(artifacts, `${path.split('/').at(-1)}-${name}.png`), fullPage: true })
      assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)), `${path} ${name} overflow`)
    }
    await page.goto(base + '/admin/users', { waitUntil: 'networkidle' })
    assert(page.url().endsWith('/admin/people'))
    await page.goto(base + '/admin/skills/9', { waitUntil: 'networkidle' })
    assert(page.url().endsWith('/admin/skills'))
    await page.goto(base + '/admin/agents/7', { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '测试助手', exact: true }).waitFor()
    await page.getByLabel('说明', { exact: true }).fill('只修改说明，保留依赖')
    await page.getByRole('button', { name: '保存修改', exact: true }).click()
    await page.waitForTimeout(400)
    const saved = calls.find((item) => item.path === '/admin/agents/7/versions/draft-id' && item.method === 'PUT')
    assert(saved)
    assert.deepEqual(saved.body.changes.config.mcp_servers, [4])
    assert.deepEqual(saved.body.changes.config.knowledge_bases, [6])
    assert.deepEqual(saved.body.changes.config.builtin_skills, ['synthetic-skill'])
    assert.deepEqual(saved.body.changes.tool_grants, snapshot.tool_grants)
    assert.deepEqual(saved.body.changes.config._skill_versions, { '9': 'locked-skill-id' })
    assert.equal(saved.body.changes.memory_policy, 'candidate_review')
    await page.getByRole('button', { name: '检查配置', exact: true }).click()
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some((item) => item.textContent === '应用配置' && !item.disabled))
    await page.getByRole('button', { name: '应用配置', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: '应用配置', exact: true }).click()
    await page.waitForTimeout(300)
    assert(calls.some((item) => item.path.endsWith('/draft-id/publish') && item.method === 'POST'))
    await page.getByRole('tab', { name: '资源绑定', exact: true }).click()
    await page.getByLabel('附加 Skill', { exact: true }).click()
    assert.equal(await page.locator('.ant-select-dropdown .ant-select-item-option').filter({ hasText: '测试技能' }).evaluate((item) => item.classList.contains('ant-select-item-option-disabled')), false)
    await page.keyboard.press('Escape')
    await page.getByRole('tab', { name: '基本配置', exact: true }).click()
    await page.evaluate(() => scrollTo(0, 0))
    await page.screenshot({ path: resolve(artifacts, `agent-${name}.png`), fullPage: true })
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2)
    assert(!overflow, `${name} page overflow`)
    await page.close()
    console.log('PASS', name, 'all modules, merged personnel, simplified models, no version entry, preserved Agent dependencies')
  }
  const page = await browser.newPage()
  await prepare(page, { ...admin, id: 8, role: 'viewer' })
  await page.goto(base + '/admin', { waitUntil: 'networkidle' })
  await page.getByText('无权访问管理后台', { exact: true }).waitFor()
  assert.equal(await page.locator('.workspace-navigation a').count(), 1)
  assert.deepEqual(errors, [])
  console.log('PASS unauthorized state and no browser runtime errors')
} catch (cause) {
  for (const context of browser.contexts()) for (const page of context.pages()) {
    await page.screenshot({ path: resolve(artifacts, 'failure.png'), fullPage: true })
    console.error((await page.locator('body').innerText()).slice(-5000))
  }
  throw cause
} finally { await browser.close(); server.close() }
