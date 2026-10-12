// Capture browser console/page errors on the login page and screenshot the brand logo.
import { chromium } from 'playwright'

const BASE = process.argv[2] || 'http://localhost:5173'
const browser = await chromium.launch({ channel: 'chromium' })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

const problems = []
page.on('console', (m) => {
  if (m.type() === 'error' || m.type() === 'warning') problems.push(`[console.${m.type()}] ${m.text()}`)
})
page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`))
page.on('requestfailed', (r) => problems.push(`[requestfailed] ${r.url()} ${r.failure()?.errorText}`))
page.on('response', (r) => {
  if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.url()}`)
})

await page.goto(BASE, { waitUntil: 'networkidle' }).catch((e) => problems.push(`[goto] ${e.message}`))
await page.waitForTimeout(2500)
await page.screenshot({ path: 'test/_diag_logo_login.png' })

const logo = await page.evaluate(() => {
  const img = document.querySelector('.login-brand-logo img')
  if (!img) return 'login logo img NOT found'
  return `login logo img: ${img.naturalWidth}x${img.naturalHeight} complete=${img.complete}`
})
const favicon = await page.evaluate(() => document.querySelector('link[rel="icon"]')?.href)

console.log('LOGO:', logo)
console.log('FAVICON:', favicon)
console.log('PROBLEMS:', problems.length)
problems.slice(0, 40).forEach((p) => console.log('  ' + p))
await browser.close()
