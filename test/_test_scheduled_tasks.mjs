// 调度任务模块重构 E2E（Playwright，双角色）：
// 管理员新建公共任务（无 Agent 选择/无模板/仅简单模式）→ 表格序号列、无 Agent 列、
// 策略固化 → 软删除/回收站/恢复 → 普通用户选择生成个人实例 → 运行与通知 →
// 权限（非管理员禁建/禁改公共任务）→ 彻底删除级联清理。
// 二期新增：任务类型（公共/个人）与可见性、属主显示规则、分组过滤（普通用户无"全部"）、
// 技能/MCP 不选=默认不启用（回显 []）、普通用户实例软删除/恢复/彻底删除、
// 同日同用户多任务共用一个会话（标题为 YYYY-MM-DD）、通知铃铛只留图标。
// 三期新增：任务单元格只留 标签(公共/个人)+名称+描述（描述≤20字、完整不截断、不显示任务内容）、
// 执行计划显示中文星期（每周五 15:30）而非 Cron 原文、星期语义修正后下次执行确实落在周五。
//
// 前置：后端 :8000 与前端 :5173 运行中，并已执行夹具：
//   conda run -n agentscope python test/_sched_fixtures.py setup
// 运行：node test/_test_scheduled_tasks.mjs
// 凭据：TP_ADMIN_ACCOUNT/TP_ADMIN_PASSWORD、TP_ACCOUNT/TP_PASSWORD（默认用夹具账号）
import { chromium } from 'playwright'

const BASE = process.env.TP_BASE || 'http://localhost:5173'
const API = process.env.TP_API || 'http://localhost:8000/api/v1'
const ADMIN_ACCOUNT = process.env.TP_ADMIN_ACCOUNT || '_sched_tmp_admin'
const ADMIN_PASSWORD = process.env.TP_ADMIN_PASSWORD || 'tmpAdmin123!'
const USER_ACCOUNT = process.env.TP_ACCOUNT || '_sched_tmp_user'
const USER_PASSWORD = process.env.TP_PASSWORD || 'tmpUser123!'
const TASK_NAME = 'UI冒烟-' + Date.now().toString(36)
// 名称之间互不包含，避免 :has-text() 选择器互相误匹配
const TASK_NAME_2 = 'UI冒烟B-' + Date.now().toString(36)
const PRIVATE_NAME = 'UI私有-' + Date.now().toString(36)
const PROMPT = '只回复一句：冒烟通过。不要调用任何工具。'
// 20 字描述：验证表格里完整展示、不被省略号截断（上限与后端 DESCRIPTION_MAX_LEN 一致）
const DESC_TEXT = '每周五下午自动汇总本周未读邮件并归档留存'
const DESC_TOO_LONG = '一二三四五六七八九十一二三四五六七八九十一'
// 当日共享会话标题（本地日期，与后端 now.strftime("%Y-%m-%d") 一致）
const TODAY = (() => {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
})()

const results = []
const check = (name, cond) => {
  results.push([name, !!cond])
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const errDetail = (e) => e?.response?.data?.detail || e?.message || ''

// antd 的图标 Tooltip 会盖住 Popconfirm 的确认按钮：先把鼠标移开再点（等价真实用户行为）
async function confirmPopconfirm(page) {
  await page.waitForSelector('.ant-popconfirm-buttons', { timeout: 8000 })
  await page.mouse.move(4, 4)
  await sleep(300)
  await page.locator('.ant-popconfirm-buttons button.ant-btn-primary').click()
}

// ---------------- API 助手 ----------------
async function apiLogin(account, password) {
  const res = await fetch(`${API}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
  if (!res.ok) throw new Error(`登录失败 ${account}: ${res.status} ${await res.text()}`)
  const data = await res.json()
  return data.access_token
}
function apiHeaders(token) {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
}
async function apiListJobs(token, scope) {
  const qs = scope ? `?scope=${scope}` : ''
  const res = await fetch(`${API}/scheduled-jobs/${qs}`, { headers: apiHeaders(token) })
  if (!res.ok) throw new Error(`列表失败: ${res.status}`)
  return res.json()
}
async function apiGet(token, path) {
  const res = await fetch(`${API}${path}`, { headers: apiHeaders(token) })
  if (!res.ok) throw new Error(`GET ${path} 失败: ${res.status}`)
  return res.json()
}
async function apiCreateJob(token, payload) {
  const res = await fetch(`${API}/scheduled-jobs/`, {
    method: 'POST', headers: apiHeaders(token), body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(`建任务失败: ${res.status} ${await res.text()}`)
  return res.json()
}
async function apiHardDelete(token, jobId) {
  return fetch(`${API}/scheduled-jobs/${jobId}/hard`, {
    method: 'DELETE', headers: apiHeaders(token),
  })
}
// 手动执行的等待逻辑：每 5s 查执行历史首行是否到达终态
async function waitRunTerminal(token, jobId, timeoutMs = 300000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    const r = await apiGet(token, `/scheduled-jobs/${jobId}/runs?limit=5`).catch(() => null)
    const first = r?.items?.[0]
    if (first && first.status !== 'running') return first
    await sleep(5000)
  }
  return null
}

let browser
try {
  browser = await chromium.launch()
} catch {
  browser = await chromium.launch({ headless: false })
}

// ============ 0. API 层准备与权限冒烟 ============
let adminToken, userToken
try {
  adminToken = await apiLogin(ADMIN_ACCOUNT, ADMIN_PASSWORD)
  userToken = await apiLogin(USER_ACCOUNT, USER_PASSWORD)
  check('夹具账号登录成功（管理员+普通用户）', true)
} catch (e) {
  check('夹具账号登录成功（管理员+普通用户）: ' + errDetail(e), false)
  console.error(e)
  await browser.close()
  process.exit(1)
}

// 非管理员不带 parent_id 直接建任务 → 403
{
  const res = await fetch(`${API}/scheduled-jobs/`, {
    method: 'POST', headers: apiHeaders(userToken),
    body: JSON.stringify({ name: 'x', prompt: 'x', trigger_type: 'cron', cron_expr: '0 8 * * *' }),
  })
  check('非管理员直接新建任务被拒（403）', res.status === 403)
}

// 描述超过 20 字 → 后端校验拒绝
{
  const res = await fetch(`${API}/scheduled-jobs/`, {
    method: 'POST', headers: apiHeaders(adminToken),
    body: JSON.stringify({
      name: '描述超长校验', prompt: 'x', description: DESC_TOO_LONG,
      trigger_type: 'cron', cron_expr: '0 8 * * *',
    }),
  })
  check('描述超过 20 字被后端拒绝（422）', res.status === 422)
}

// ============ 1. 管理员 UI ============
const adminPage = await browser.newPage({ viewport: { width: 1440, height: 900 } })
adminPage.setDefaultTimeout(30000)

let publicJobId = null
let privateJobId = null
try {
  // 登录
  await adminPage.goto(`${BASE}/login`)
  await adminPage.getByPlaceholder('账号').first().fill(ADMIN_ACCOUNT)
  await adminPage.getByPlaceholder('密码').first().fill(ADMIN_PASSWORD)
  await adminPage.locator('button.login-btn').first().click()
  await adminPage.waitForSelector('.sidebar-nav-item', { timeout: 20000 })
  check('管理员登录成功', true)

  // 侧边栏入口位置
  const labels = await adminPage.locator('.sidebar-nav-item .sidebar-nav-label').allTextContents()
  const idxMon = labels.findIndex((t) => t.includes('监控'))
  const idxSched = labels.findIndex((t) => t.includes('调度任务'))
  const idxProfile = labels.findIndex((t) => t.includes('个人信息'))
  check('调度任务入口位于监控与个人信息之间', idxMon >= 0 && idxProfile >= 0 && idxMon < idxSched && idxSched < idxProfile)

  // 通知铃铛在用户名右侧（DOM 中铃铛位于用户块之后）
  const bellOrder = await adminPage.evaluate(() => {
    const bottom = document.querySelector('.sidebar-bottom')
    const user = bottom?.querySelector('.sidebar-user')
    const bell = bottom?.querySelector('.notif-bell')
    if (!user || !bell) return 'missing'
    return (user.compareDocumentPosition(bell) & Node.DOCUMENT_POSITION_FOLLOWING) ? 'after' : 'before'
  })
  check('通知铃铛位于用户名右侧', bellOrder === 'after')

  // 进入页面
  await adminPage.locator('.sidebar-nav-item', { hasText: '调度任务' }).click()
  await adminPage.waitForSelector('.sched-page', { timeout: 15000 })
  check('页面标题渲染', (await adminPage.locator('.sched-title').textContent()).includes('调度任务'))
  check('统计卡片 4 张', (await adminPage.locator('.sched-card').count()) === 4)
  check('工具栏含回收站分组（管理员）', (await adminPage.locator('.ant-segmented-item-label').allTextContents()).some((t) => t.includes('回收站')))

  // 新建公共任务：每周五 15:30 + 20 字描述（覆盖星期语义与描述展示）
  await adminPage.getByRole('button', { name: '新建任务' }).first().click()
  await adminPage.waitForSelector('.ant-drawer-open', { timeout: 10000 })
  const drawerText = await adminPage.locator('.ant-drawer-open').textContent()
  check('新建表单无 Agent 选择', !/agent/i.test(drawerText))
  check('新建表单无模板/Cron 模式', (await adminPage.locator('.sched-template').count()) === 0 && !/cron/i.test(drawerText))

  await adminPage.locator('.ant-drawer-open input[type="text"]').first().fill(TASK_NAME)
  await adminPage.locator('.ant-drawer-open .sched-field', { hasText: '描述' }).first().locator('input').fill(DESC_TEXT)
  check('描述输入框限制 20 字',
    (await adminPage.locator('.ant-drawer-open .sched-field', { hasText: '描述' }).first()
      .locator('input').inputValue()).length === 20)
  await adminPage.locator('.ant-drawer-open textarea').fill(PROMPT)

  // 执行时间：每周 → 15:30 → 只勾周五（cron 应为 30 15 * * 5）
  await adminPage.locator('.ant-drawer-open .sched-simple .ant-select-selector').first().click()
  await adminPage.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="每周"]').click()
  const timeInput = adminPage.locator('.ant-drawer-open .sched-simple .ant-picker input').first()
  await timeInput.click()
  await timeInput.fill('15:30')
  await adminPage.keyboard.press('Enter')
  for (const d of ['周一', '周二', '周三', '周四']) {
    await adminPage.locator(`.ant-drawer-open .ant-checkbox-wrapper:has-text("${d}")`).click()
  }
  await adminPage.waitForSelector('.sched-preview:has-text("每周五 15:30")', { timeout: 10000 })
  check('触发预览显示中文星期（不再是原始 cron）',
    (await adminPage.locator('.sched-preview').first().textContent()).includes('每周五 15:30'))

  await adminPage.locator('.ant-drawer-footer button.ant-btn-primary').click()
  await adminPage.waitForSelector(`.ant-table-row:has-text("${TASK_NAME}")`, { timeout: 15000 })
  check('公共任务创建成功并出现在表格', true)

  // 任务单元格：只有 标签 + 名称 + 描述，不显示任务内容，且描述完整不截断
  const taskRow = adminPage.locator(`.ant-table-row:has-text("${TASK_NAME}")`).first()
  const rowText = (await taskRow.textContent()) || ''
  check('任务单元格不显示任务内容',
    !rowText.includes(PROMPT) && (await taskRow.locator('.sched-cell-prompt').count()) === 0)
  const descBox = await taskRow.locator('.sched-cell-desc').first().evaluate((el) => {
    const cs = getComputedStyle(el)
    return {
      text: el.textContent || '', noOverflow: el.scrollWidth <= el.clientWidth + 1,
      textOverflow: cs.textOverflow, whiteSpace: cs.whiteSpace,
    }
  })
  check('任务单元格完整展示 20 字描述（无省略截断）',
    descBox.text === DESC_TEXT && descBox.noOverflow
    && descBox.textOverflow !== 'ellipsis' && descBox.whiteSpace !== 'nowrap')

  // 执行计划列：中文星期 + 保留"下次 xx-xx xx:xx"
  const schedText = (await taskRow.locator('td').nth(2).textContent()) || ''
  check('执行计划显示“每周五 15:30”而非 Cron 原文',
    schedText.includes('每周五 15:30') && !schedText.includes('Cron:'))
  check('执行计划仍显示下次执行时间', /下次\s*\d{2}-\d{2}\s*\d{2}:\d{2}/.test(schedText))

  // 表格结构：序号列、无 Agent 列
  const headers = await adminPage.locator('.ant-table-thead th').allTextContents()
  check('表头含序号列', headers.some((h) => h.includes('序号')))
  check('表头无 Agent 列', !headers.some((h) => h.toLowerCase().includes('agent')))
  const firstCell = await adminPage.locator(`.ant-table-row:has-text("${TASK_NAME}") td`).first().textContent()
  check('首列为自增序号', /^\s*\d+\s*$/.test(firstCell || ''))

  // 固化策略校验（API 读回）
  const jobs = await apiListJobs(adminToken)
  const job = jobs.find((j) => j.name === TASK_NAME)
  publicJobId = job?.id
  check('API 能读回新建公共任务', !!job)
  if (job) {
    check('公共任务 is_public 且绑定任务助手', job.is_public === true && !!job.agent_id)
    check('策略固化：并行+不重试+补跑+每次通知',
      job.skip_if_running === false && job.max_retries === 0
      && job.misfire_policy === 'run_once' && job.notify_on === 'always')
    check('通知渠道含站内与邮件', Array.isArray(job.notify_channels) && job.notify_channels.includes('inapp') && job.notify_channels.includes('email'))
    check('未选技能/MCP 时回显空数组（默认全部不可用）',
      Array.isArray(job.selected_skills) && job.selected_skills.length === 0
      && Array.isArray(job.selected_mcps) && job.selected_mcps.length === 0)
    check('API schedule_desc 为中文星期描述', job.schedule_desc === '每周五 15:30')
    check('cron_expr 按 crontab 语义存为 30 15 * * 5', job.cron_expr === '30 15 * * 5')
    // 星期语义回归：APScheduler 3.x 把 0 当周一，转换后下次执行必须真的落在周五
    check('下次执行确实落在周五', new Date(job.next_run_at).getDay() === 5)
    check('描述按 20 字原样回显', job.description === DESC_TEXT)
  }

  // 二期：管理员可选"任务类型"，建一个个人任务（普通用户不可见、不可选）
  await adminPage.getByRole('button', { name: '新建任务' }).first().click()
  await adminPage.waitForSelector('.ant-drawer-open', { timeout: 10000 })
  const drawerText2 = (await adminPage.locator('.ant-drawer-open').textContent()) || ''
  check('新建抽屉提供“任务类型”选项', drawerText2.includes('任务类型')
    && drawerText2.includes('公共任务') && drawerText2.includes('个人任务'))
  await adminPage.locator('.ant-drawer-open .ant-segmented-item', { hasText: '个人任务' }).first().click()
  // 展开"执行策略"折叠面板后再校验技能/MCP 的默认文案
  await adminPage.locator('.ant-drawer-open .ant-collapse-header').first().click()
  await sleep(600)
  const advText = (await adminPage.locator('.ant-drawer-open').textContent()) || ''
  check('技能/MCP 文案为“默认不启用，选择后生效”', advText.includes('默认不启用'))
  await adminPage.locator('.ant-drawer-open input[type="text"]').first().fill(PRIVATE_NAME)
  await adminPage.locator('.ant-drawer-open textarea').fill(PROMPT)
  await adminPage.waitForSelector('.sched-preview', { timeout: 10000 })
  await adminPage.locator('.ant-drawer-footer button.ant-btn-primary').click()
  await adminPage.waitForSelector(`.ant-table-row:has-text("${PRIVATE_NAME}")`, { timeout: 15000 })
  check('管理员个人任务创建成功并出现在表格', true)
  privateJobId = (await apiListJobs(adminToken)).find((j) => j.name === PRIVATE_NAME)?.id || null
  const privateJob = privateJobId ? await apiGet(adminToken, `/scheduled-jobs/${privateJobId}`) : null
  check('个人任务 is_public=false 且无父任务',
    !!privateJob && privateJob.is_public === false && privateJob.parent_id === null)

  // 第二个公共任务：验证"同日同用户多任务共用一个会话"
  const pub2 = await apiCreateJob(adminToken, {
    name: TASK_NAME_2, prompt: PROMPT, trigger_type: 'cron', cron_expr: '0 9 * * *',
    selected_skills: [], selected_mcps: [],
  })
  check('第二个公共任务创建成功', !!pub2?.id && pub2.is_public === true)
} catch (e) {
  console.error('管理员阶段异常:', e)
  check('管理员阶段无异常', false)
}

// ============ 2. 普通用户 UI：分组/铃铛/选择公共任务 ============
const userCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const userPage = await userCtx.newPage()
userPage.setDefaultTimeout(30000)

let inst1Id = null
let inst2Id = null
try {
  await userPage.goto(`${BASE}/login`)
  await userPage.getByPlaceholder('账号').first().fill(USER_ACCOUNT)
  await userPage.getByPlaceholder('密码').first().fill(USER_PASSWORD)
  await userPage.locator('button.login-btn').first().click()
  await userPage.waitForSelector('.sidebar-nav-item', { timeout: 20000 })
  check('普通用户登录成功', true)

  // 二期：左下角通知只留铃铛图标（点击行为不变）
  const bellText = (await userPage.locator('.notif-bell').first().textContent()) || ''
  check('通知铃铛内不存在“通知”文本',
    !bellText.includes('通知') && (await userPage.locator('.notif-bell-label').count()) === 0)
  check('通知铃铛仍渲染图标与未读角标',
    (await userPage.locator('.notif-bell .notif-bell-icon').count()) === 1
    && (await userPage.locator('.notif-bell .ant-badge').count()) === 1)

  await userPage.locator('.sidebar-nav-item', { hasText: '调度任务' }).click()
  await userPage.waitForSelector('.sched-page', { timeout: 15000 })
  check('普通用户无新建任务按钮', (await userPage.getByRole('button', { name: '新建任务' }).count()) === 0)

  // 二期：分组过滤——普通用户只有 公共任务/我的任务/回收站，没有"全部"
  const segLabels = (await userPage.locator('.ant-segmented-item-label').allTextContents()).map((t) => t.trim())
  check('普通用户看不到“全部”分组', !segLabels.some((t) => t === '全部'))
  check('普通用户有 公共任务/我的任务/回收站 分组',
    segLabels.some((t) => t.includes('公共任务')) && segLabels.some((t) => t.includes('我的任务'))
    && segLabels.some((t) => t.includes('回收站')))
  check('普通用户默认分组为公共任务',
    ((await userPage.locator('.ant-segmented-item-selected').first().textContent()) || '').includes('公共任务'))

  // 二期：管理员个人任务对普通用户不可见、不可选
  {
    const userJobs = await apiListJobs(userToken)
    check('普通用户列表看不到管理员个人任务', !userJobs.some((j) => j.name === PRIVATE_NAME))
    if (privateJobId) {
      const res = await fetch(`${API}/scheduled-jobs/`, {
        method: 'POST', headers: apiHeaders(userToken),
        body: JSON.stringify({ parent_id: privateJobId, trigger_type: 'cron', cron_expr: '0 8 * * *' }),
      })
      check('普通用户不能选择管理员个人任务（404）', res.status === 404)
      const getRes = await fetch(`${API}/scheduled-jobs/${privateJobId}`, { headers: apiHeaders(userToken) })
      check('普通用户读不到管理员个人任务详情（404）', getRes.status === 404)
    }
  }

  // 公共任务行有"选择"入口，且不显示属主
  const pubRow = userPage.locator(`.ant-table-row:has-text("${TASK_NAME}")`).first()
  await pubRow.waitFor({ timeout: 15000 })
  check('普通用户可见公共任务', true)
  check('公共任务行提供选择入口', (await pubRow.getByText('选择').count()) > 0)
  check('公共任务行不显示属主', !((await pubRow.textContent()) || '').includes('属主'))

  await pubRow.getByText('选择').first().click()
  await userPage.waitForSelector('.ant-modal', { timeout: 10000 })
  await userPage.waitForSelector('.ant-modal .sched-preview', { timeout: 10000 }).catch(() => {})
  await userPage.locator('.ant-modal-footer button.ant-btn-primary').click()
  await sleep(1500)

  // 第二个公共任务也选一份实例，用于验证同日共享会话
  const pubRow2 = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}")`).first()
  await pubRow2.waitFor({ timeout: 15000 })
  await pubRow2.getByText('选择').first().click()
  await userPage.waitForSelector('.ant-modal', { timeout: 10000 })
  await userPage.waitForSelector('.ant-modal .sched-preview', { timeout: 10000 }).catch(() => {})
  await userPage.locator('.ant-modal-footer button.ant-btn-primary').click()
  await sleep(1500)
  {
    const myJobs = await apiListJobs(userToken)
    inst1Id = myJobs.find((j) => j.name === TASK_NAME && !j.is_public)?.id || null
    inst2Id = myJobs.find((j) => j.name === TASK_NAME_2 && !j.is_public)?.id || null
    check('选择后生成两个个人实例', !!inst1Id && !!inst2Id)
    const inst1 = myJobs.find((j) => j.id === inst1Id)
    check('个人实例 is_public=false 且带属主用户名',
      !!inst1 && inst1.is_public === false && inst1.owner_name === USER_ACCOUNT)
  }

  // 切到"我的任务"过滤，应只剩个人实例
  await userPage.locator('.ant-segmented-item-label', { hasText: '我的任务' }).click()
  await sleep(1000)
  const myRows = userPage.locator('.ant-table-row')
  const myCount = await myRows.count()
  let hasMine = false, mineShowsOwner = false, mineHasPublic = false, mineShowsWoDe = false
  for (let i = 0; i < myCount; i++) {
    const txt = (await myRows.nth(i).textContent()) || ''
    if (txt.includes(TASK_NAME) && txt.includes('个人')) hasMine = true
    if (txt.includes(TASK_NAME) && txt.includes(`属主：${USER_ACCOUNT}`)) mineShowsOwner = true
    if (txt.includes('公共')) mineHasPublic = true
    if (txt.includes('我的')) mineShowsWoDe = true
  }
  check('我的任务过滤下存在个人实例', hasMine)
  check('实例标签统一为“个人”（不再按视角显示“我的”）', hasMine && !mineShowsWoDe)
  check('个人实例行显示属主用户名', mineShowsOwner)
  check('我的任务分组不含公共任务', !mineHasPublic)

  // 权限：普通用户改/删公共任务被拒
  if (publicJobId) {
    const putRes = await fetch(`${API}/scheduled-jobs/${publicJobId}`, {
      method: 'PUT', headers: apiHeaders(userToken),
      body: JSON.stringify({ description: 'hack' }),
    })
    const delRes = await fetch(`${API}/scheduled-jobs/${publicJobId}`, {
      method: 'DELETE', headers: apiHeaders(userToken),
    })
    check('普通用户编辑公共任务被拒（403/404）', putRes.status === 403 || putRes.status === 404)
    check('普通用户删除公共任务被拒（403/404）', delRes.status === 403 || delRes.status === 404)
  }
} catch (e) {
  console.error('普通用户阶段异常:', e)
  check('普通用户阶段无异常', false)
}

// ============ 2b. 同日共享会话：同一用户两个任务依次手动执行 ============
let sharedSessionId = null
try {
  await userPage.locator('.ant-segmented-item-label', { hasText: '我的任务' }).click()
  await sleep(800)
  const instRow = userPage.locator(`.ant-table-row:has-text("${TASK_NAME}"):has(.ant-tag:has-text("个人"))`).first()
  const instRow2 = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}"):has(.ant-tag:has-text("个人"))`).first()
  await instRow.waitFor({ timeout: 15000 })
  await instRow2.waitFor({ timeout: 15000 })

  // 串行执行：第一个到终态后再触发第二个，避免命中"当日会话正忙"的回退分支
  await instRow.locator('td:last-child button').nth(0).click() // 立即运行
  const run1 = await waitRunTerminal(userToken, inst1Id)
  check('第一个个人实例手动运行到达终态', !!run1)
  check('第一个个人实例手动运行成功', run1?.status === 'success')

  await instRow2.locator('td:last-child button').nth(0).click() // 立即运行
  const run2 = await waitRunTerminal(userToken, inst2Id)
  check('第二个个人实例手动运行到达终态', !!run2)
  check('第二个个人实例手动运行成功', run2?.status === 'success')

  sharedSessionId = run1?.session_id || null
  check('同日同用户两个任务共用同一个会话',
    !!run1 && !!run2 && !!run1.session_id && run1.session_id === run2.session_id)
  if (sharedSessionId) {
    const sessions = await apiGet(userToken, '/sessions/')
    const sess = sessions.find((s) => s.id === sharedSessionId)
    check(`共享会话标题为当天日期 ${TODAY}`, sess?.title === TODAY)
    const msgs = await apiGet(userToken, `/sessions/${sharedSessionId}/messages?limit=50`)
    const list = Array.isArray(msgs) ? msgs : (msgs.messages || [])
    check('共享会话内追加了两条任务指令消息',
      list.filter((m) => m.role === 'user').length >= 2)
  }

  // UI：执行历史 → 查看会话 指向同一个会话
  await instRow.locator('td:last-child button').nth(2).click() // 执行历史
  await userPage.waitForSelector('.ant-drawer-open .sched-runs', { timeout: 10000 })
  const viewBtn = userPage.locator('.ant-drawer-open .sched-run-row').first().getByText('查看会话')
  check('执行历史提供“查看会话”入口', (await viewBtn.count()) > 0)
  await viewBtn.first().click()
  await userPage.waitForURL(/\/chat\/\d+/, { timeout: 15000 })
  const urlSid = Number((userPage.url().match(/\/chat\/(\d+)/) || [])[1])
  check('“查看会话”跳转到当天共享会话', urlSid === sharedSessionId)
  // 硬导航回列表页：清掉跨页遗留的抽屉遮罩，避免后续点击被拦截
  await userPage.goto(`${BASE}/scheduled-tasks`)
  await userPage.waitForSelector('.sched-page', { timeout: 20000 })
  await sleep(1500)

  // 通知铃铛：结果通知（站内），点击弹层行为不变
  await userPage.locator('.notif-bell').click()
  await userPage.waitForSelector('.notif-panel', { timeout: 8000 })
  await userPage.waitForSelector('.notif-item', { timeout: 12000 }).catch(() => {})
  check('运行结果产生站内通知', (await userPage.locator('.notif-item').count()) > 0)
  await userPage.keyboard.press('Escape')
  await sleep(500)
} catch (e) {
  console.error('同日共享会话阶段异常:', e)
  check('同日共享会话阶段无异常', false)
}

// ============ 2c. 普通用户回收站：软删除 → 恢复 → 彻底删除 ============
try {
  await userPage.locator('.ant-segmented-item-label', { hasText: '我的任务' }).click()
  await sleep(800)
  const row2 = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}"):has(.ant-tag:has-text("个人"))`).first()
  await row2.waitFor({ timeout: 15000 })
  await row2.locator('td:last-child button').last().click() // 移入回收站
  const popText = (await userPage.locator('.ant-popconfirm').first().textContent()) || ''
  check('实例删除确认文案为“移入回收站”', popText.includes('回收站'))
  await confirmPopconfirm(userPage)
  await sleep(1500)
  check('软删除后实例移出我的任务',
    (await userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}")`).count()) === 0)

  await userPage.locator('.ant-segmented-item-label', { hasText: '回收站' }).click()
  await sleep(1200)
  const trashRow = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}")`).first()
  check('普通用户回收站可见自己的实例', (await trashRow.count()) > 0)
  const trashList = await apiListJobs(userToken, 'deleted')
  check('普通用户回收站接口只返回本人任务',
    trashList.length > 0 && trashList.every((j) => j.user_id === trashList[0].user_id))

  await trashRow.locator('td:last-child button').nth(0).click() // 恢复
  await sleep(1500)
  await userPage.locator('.ant-segmented-item-label', { hasText: '我的任务' }).click()
  await sleep(1000)
  check('恢复后实例回到我的任务',
    (await userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}")`).count()) > 0)

  // 再次软删 → 回收站彻底删除
  const row2b = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}"):has(.ant-tag:has-text("个人"))`).first()
  await row2b.locator('td:last-child button').last().click()
  await confirmPopconfirm(userPage)
  await sleep(1500)
  await userPage.locator('.ant-segmented-item-label', { hasText: '回收站' }).click()
  await sleep(1200)
  const trashRow2 = userPage.locator(`.ant-table-row:has-text("${TASK_NAME_2}")`).first()
  await trashRow2.locator('td:last-child button').nth(1).click() // 彻底删除
  await confirmPopconfirm(userPage)
  await sleep(1500)
  const stillDeleted = await apiListJobs(userToken, 'deleted')
  check('普通用户可彻底删除自己的实例', !stillDeleted.some((j) => j.name === TASK_NAME_2))
} catch (e) {
  console.error('普通用户回收站阶段异常:', e)
  check('普通用户回收站阶段无异常', false)
}

// ============ 3. 管理员：软删除/恢复/彻底删除级联 ============
// 公共任务行精确定位（同名个人实例存在时避免误点）
const PUB_ROW = `.ant-table-row:has-text("${TASK_NAME}"):has(.ant-tag:has-text("公共"))`
try {
  await sleep(1000) // 等待表格轮询稳定

  // 二期：管理员"全部"分组可见他人实例；公共任务无属主、个人实例显示属主
  await adminPage.getByRole('button', { name: '刷新' }).first().click()
  await sleep(1500)
  await adminPage.locator('.ant-segmented-item-label', { hasText: '全部' }).click()
  await sleep(1000)
  const adminSeg = (await adminPage.locator('.ant-segmented-item-label').allTextContents()).map((t) => t.trim())
  check('管理员分组含“全部”', adminSeg.some((t) => t === '全部'))
  const pubRowTxt = (await adminPage.locator(PUB_ROW).first().textContent().catch(() => '')) || ''
  const instRowTxt = (await adminPage
    .locator(`.ant-table-row:has-text("${TASK_NAME}"):has(.ant-tag:has-text("个人"))`).first()
    .textContent().catch(() => '')) || ''
  check('管理员“全部”分组能看到他人个人实例', instRowTxt.length > 0)
  check('公共任务行无属主字样', pubRowTxt.length > 0 && !pubRowTxt.includes('属主'))
  check('个人实例行有属主用户名', instRowTxt.includes(`属主：${USER_ACCOUNT}`))

  // 软删除公共任务
  const row = adminPage.locator(PUB_ROW).first()
  await row.locator('td:last-child button').last().click() // 删除（软）
  await confirmPopconfirm(adminPage)
  await sleep(1200)
  check('软删除后公共任务移出常规列表', (await adminPage.locator(PUB_ROW).count()) === 0)

  // 软删除不影响已有个人实例（普通用户仍可见）
  const instStill = await apiListJobs(userToken)
  check('软删除公共任务不影响用户个人实例', instStill.some((j) => j.name === TASK_NAME && !j.is_public))

  // 回收站可见 + 恢复
  await adminPage.locator('.ant-segmented-item-label', { hasText: '回收站' }).click()
  await sleep(800)
  const trashRow = adminPage.locator(`.ant-table-row:has-text("${TASK_NAME}")`).first()
  check('回收站可见已软删除任务', (await trashRow.count()) > 0)
  await trashRow.locator('td:last-child button').nth(0).click() // 恢复
  await sleep(1200)
  await adminPage.locator('.ant-segmented-item-label', { hasText: '全部' }).click()
  await sleep(800)
  check('恢复后公共任务回到常规列表', (await adminPage.locator(PUB_ROW).count()) > 0)

  // 彻底删除（级联个人实例）：先软删 → 回收站 → 彻底删除
  const row2 = adminPage.locator(PUB_ROW).first()
  await row2.locator('td:last-child button').last().click()
  await confirmPopconfirm(adminPage)
  await sleep(1200)
  await adminPage.locator('.ant-segmented-item-label', { hasText: '回收站' }).click()
  await sleep(800)
  const trashRow2 = adminPage.locator(`.ant-table-row:has-text("${TASK_NAME}")`).first()
  await trashRow2.locator('td:last-child button').nth(1).click() // 彻底删除
  await confirmPopconfirm(adminPage)
  await sleep(1200)

  const after = await apiListJobs(adminToken)
  check('彻底删除后公共任务消失', !after.some((j) => j.name === TASK_NAME))
  const afterUser = await apiListJobs(userToken)
  check('彻底删除级联清理用户个人实例', !afterUser.some((j) => j.name === TASK_NAME))

  // 清理二期新增的临时任务（管理员个人任务 + 第二个公共任务）
  if (privateJobId) await apiHardDelete(adminToken, privateJobId)
  const left = await apiListJobs(adminToken)
  const pub2 = left.find((j) => j.name === TASK_NAME_2)
  if (pub2) await apiHardDelete(adminToken, pub2.id)
  const final = await apiListJobs(adminToken)
  check('二期临时任务已清理', !final.some((j) => j.name === TASK_NAME_2 || j.name === PRIVATE_NAME))
} catch (e) {
  console.error('删除/回收站阶段异常:', e)
  check('删除/回收站阶段无异常', false)
}

await browser.close()

const failed = results.filter(([, ok]) => !ok).map(([n]) => n)
console.log('='.repeat(50))
console.log(`结果: ${results.length - failed.length}/${results.length} 通过`)
if (failed.length) {
  console.log('失败项:', failed)
  process.exit(1)
}
