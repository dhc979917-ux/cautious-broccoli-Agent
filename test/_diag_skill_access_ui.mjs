// UI 冒烟：管理员编辑弹窗的 Skill 授权树 + Admin 技能弹窗的广场隐藏开关。
// 前置：后端 :8000、前端 :5173、python test/_skill_ui_fixture.py（生成临时管理员）。
import { chromium } from 'playwright'
import { readFileSync } from 'fs'

const BASE = process.argv[2] || 'http://localhost:5173'
const cred = JSON.parse(readFileSync(new URL('./_skill_ui_user.json', import.meta.url)))

const browser = await chromium.launch({ channel: 'chromium' })
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })

const problems = []
page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`))
page.on('console', (m) => {
  if (m.type() !== 'error') return
  // 既有全局 antd 弃用警告（与本功能无关），不计入失败
  if (m.text().includes('destroyOnClose') || m.text().includes('Static function can not consume context')) return
  problems.push(`[console.error] ${m.text()}`)
})

await page.goto(BASE + '/login', { waitUntil: 'networkidle' })
await page.fill('input[type="text"], input#account', cred.account)
await page.fill('input[type="password"]', cred.password)
await page.click('button[type="submit"]')
await page.waitForTimeout(3500)

// 人员编辑弹窗：Skill 授权树
await page.goto(BASE + '/users', { waitUntil: 'networkidle' })
await page.waitForTimeout(1800)
await page.locator('tbody tr').first().locator('button')
  .filter({ has: page.locator('.anticon-edit') }).first().click()
await page.waitForTimeout(900)
const grantLabel = await page.locator('.ant-form-item-label', { hasText: 'Skill 授权' }).count()
const treeChecks = await page.locator('.users-edit-group .ant-tree-checkbox').count()
console.log('USERS  Skill授权标签:', grantLabel > 0 ? 'FOUND' : 'MISSING')
console.log('USERS  授权树节点数:', treeChecks)
await page.screenshot({ path: 'test/_diag_skill_users_edit.png' })

// Admin 技能弹窗：广场隐藏开关（新建与编辑共用同一表单）
await page.goto(BASE + '/admin', { waitUntil: 'networkidle' })
await page.waitForTimeout(2200)
await page.getByRole('button', { name: /新建 Skill/ }).first().click()
await page.waitForTimeout(900)
const hiddenLabel = await page.locator('.ant-form-item-label', { hasText: '在技能广场隐藏' }).count()
const switchCount = await page.locator('.ant-modal .ant-switch').count()
console.log('ADMIN  广场隐藏标签:', hiddenLabel > 0 ? 'FOUND' : 'MISSING')
console.log('ADMIN  开关数量:', switchCount)
await page.screenshot({ path: 'test/_diag_skill_admin_modal.png' })

await browser.close()
console.log('PROBLEMS:', problems.length)
problems.slice(0, 10).forEach((p) => console.log('  ' + p))
const ok = grantLabel > 0 && treeChecks > 0 && hiddenLabel > 0 && problems.length === 0
console.log('SUMMARY:', ok ? 'PASS' : 'FAIL')
process.exit(ok ? 0 : 1)
