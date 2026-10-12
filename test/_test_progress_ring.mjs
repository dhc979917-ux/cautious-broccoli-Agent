import { chromium } from 'playwright'

const BASE = 'http://localhost:5173'

async function testProgressRing() {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()

  // Navigate to login page
  await page.goto(`${BASE}/login`)
  await page.waitForLoadState('networkidle')

  // Login as admin
  await page.fill('input[placeholder*="用户名"], input[id="username"]', 'admin')
  await page.fill('input[placeholder*="密码"], input[type="password"]', 'admin123')
  await page.click('button[type="submit"]')
  await page.waitForURL('**/chat**', { timeout: 10000 }).catch(() => {})

  // Navigate to chat
  await page.goto(`${BASE}/chat`)
  await page.waitForLoadState('networkidle')

  // Check if ProgressRing component renders when streaming
  // We can't easily trigger streaming, but we can check the CSS class exists
  const ringExists = await page.locator('.chat-progress-ring').count()
  console.log('chat-progress-ring elements found:', ringExists)

  // If streaming is active, check if the ring has transform applied (JS-driven)
  if (ringExists > 0) {
    // Give it a moment for rAF to apply transform
    await page.waitForTimeout(200)
    const transform = await page.locator('.chat-progress-ring').first().evaluate(
      (el) => el.style.transform || getComputedStyle(el).transform
    )
    console.log('ring transform:', transform)
    if (transform && transform !== 'none') {
      console.log('PASS: ProgressRing has transform applied (JS-driven rotation)')
    } else {
      console.log('WARN: ProgressRing has no transform yet (streaming may not be active)')
    }
  }

  // Check reduced-motion CSS override exists
  const css = await page.evaluate(() => {
    const sheets = document.styleSheets
    for (const sheet of sheets) {
      try {
        for (const rule of sheet.cssRules) {
          if (rule.cssText && rule.cssText.includes('chat-progress-ring') && rule.cssText.includes('animation-duration')) {
            return rule.cssText
          }
        }
      } catch (e) { /* cross-origin */ }
    }
    return null
  })
  console.log('CSS rule with chat-progress-ring animation:', css ? 'found' : 'not found')

  await browser.close()
  console.log('Test complete')
}

testProgressRing().catch(e => { console.error(e); process.exit(1) })
