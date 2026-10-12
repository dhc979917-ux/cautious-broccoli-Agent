// Screenshot the sidebar brand logo (expanded + collapsed) after login.
// Requires: backend on :8000, frontend on :5173, and test/_ui_logout_user.json
// (created by `python test/_ui_logout_fixture.py setup`).
import { chromium } from 'playwright'
import { readFileSync } from 'fs'

const BASE = process.argv[2] || 'http://localhost:5173'
const cred = JSON.parse(readFileSync(new URL('./_ui_logout_user.json', import.meta.url)))

const browser = await chromium.launch({ channel: 'chromium' })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await page.goto(BASE + '/login', { waitUntil: 'networkidle' })
await page.fill('input[type="text"], input#account', cred.account).catch(() => {})
await page.fill('input[type="password"]', cred.password)
await page.click('button[type="submit"]')
await page.waitForTimeout(3500)
await page.screenshot({ path: 'test/_diag_sidebar_expanded.png' })
await page.click('.sidebar-logo')
await page.waitForTimeout(800)
await page.screenshot({ path: 'test/_diag_sidebar_collapsed.png' })
console.log('url after login:', page.url())
await browser.close()
