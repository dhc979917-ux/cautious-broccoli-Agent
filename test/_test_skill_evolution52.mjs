import assert from 'node:assert/strict'
import { readFile, mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
const identity = JSON.parse(await readFile('/tmp/evolution52-identity.json', 'utf8'))
const base = process.env.EVOLUTION52_BASE || 'http://evolution52-qa-frontend'
assert.match(base, /(?:evolution52-qa-frontend|10\.9\.7\.52:8092)/)
const browser = await chromium.launch({ headless: true, executablePath: '/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell', args: ['--no-sandbox'] })
const errors = []
await mkdir('/tmp/evolution52-artifacts', { recursive: true })
try {
  for (const [width, height] of [[1366, 900], [390, 844]]) {
    const context = await browser.newContext({ viewport: { width, height } })
    await context.addInitScript(identity => localStorage.setItem('auth-storage', JSON.stringify({ state: { user: identity.user, token: identity.token, theme: 'light', selectedSkills: [], selectedMcps: null }, version: 6 })), identity)
    const page = await context.newPage()
    page.on('pageerror', e => errors.push(e.message))
    await page.goto(base + '/skills', { waitUntil: 'networkidle' })
    await page.getByText('我的技能', { exact: true }).click()
    await page.getByText('个人技能进化', { exact: true }).waitFor()
    await page.getByRole('switch', { name: '个人技能自动进化' }).waitFor()
    assert.equal(await page.getByRole('switch', { name: '个人技能自动进化' }).getAttribute('aria-checked'), 'true')
    await page.getByRole('button', { name: '查看变更', exact: true }).first().click()
    await page.getByText('技能进化记录', { exact: true }).waitFor()
    await page.getByText('验证证据', { exact: true }).waitFor()
    await page.screenshot({ path: '/tmp/evolution52-artifacts/review-' + width + '.png', fullPage: true })
    await page.locator('.ant-drawer-close').last().click()
    await page.getByRole('switch', { name: '个人技能自动进化' }).click()
    await page.waitForTimeout(600)
    assert.equal(await page.getByRole('switch', { name: '个人技能自动进化' }).getAttribute('aria-checked'), 'false')
    await page.getByRole('switch', { name: '个人技能自动进化' }).click()
    await page.locator('.ant-modal-confirm-title').filter({ hasText: '开启个人技能自动进化？' }).waitFor()
    await page.getByRole('button', { name: '开启自动进化', exact: true }).click()
    await page.waitForTimeout(600)
    assert.equal(await page.getByRole('switch', { name: '个人技能自动进化' }).getAttribute('aria-checked'), 'true')
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2), false)
    await page.goto(base + '/chat/' + identity.session_id + '?panel=skill', { waitUntil: 'networkidle' })
    await page.getByText('个人技能进化', { exact: true }).waitFor()
    await page.getByText('本会话已关联个人进化版', { exact: true }).waitFor()
    await page.screenshot({ path: '/tmp/evolution52-artifacts/chat-' + width + '.png', fullPage: true })
    await context.close()
  }
  assert.deepEqual(errors, [])
  console.log('PASS desktop/mobile review, enable confirmation, preference persistence, chat routing UI; no page errors')
} finally { await browser.close() }
