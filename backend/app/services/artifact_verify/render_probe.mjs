// Tier 1/2 无头渲染探针：由 render_check.py 以子进程方式调用。
//
// 协议：stdin 收一个 JSON 配置，stdout 输出一个 JSON 结论（永远输出，
// 出错也输出 {ok:false, reason:...}），退出码仅供日志用。
//
// 只回传文本结论，不回传 DOM/HTML 原文——这是"验证要便宜"的关键：
// 一次调用的结果控制在几百 token 内，模型才敢用、才用得起。
// 截图（Tier 2）落盘为文件，由调用方决定是否附加给模型。

import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const MAX_CONSOLE = 12
const MAX_OVERFLOW = 8

function readStdin() {
  return new Promise((resolve) => {
    let buf = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (c) => { buf += c })
    process.stdin.on('end', () => resolve(buf))
    process.stdin.on('error', () => resolve(buf))
  })
}

async function loadChromium(entry) {
  // entry 可以是 playwright 包目录的绝对路径，也可以是裸模块名
  const spec = entry && (entry.startsWith('/') || /^[A-Za-z]:[\\/]/.test(entry))
    ? pathToFileURL(path.join(entry, 'index.js')).href
    : (entry || 'playwright')
  const mod = await import(spec)
  const pw = mod.default && mod.default.chromium ? mod.default : mod
  if (!pw || !pw.chromium) throw new Error('playwright 模块里没有 chromium 导出')
  return pw.chromium
}

function short(s, n = 180) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim()
  return s.length > n ? s.slice(0, n) + '…' : s
}

async function main() {
  const raw = await readStdin()
  let cfg
  try {
    cfg = JSON.parse(raw || '{}')
  } catch (e) {
    console.log(JSON.stringify({ ok: false, reason: 'config_parse_error: ' + e.message }))
    return 1
  }

  const target = cfg.path
  if (!target || !fs.existsSync(target)) {
    console.log(JSON.stringify({ ok: false, reason: 'artifact_not_found' }))
    return 1
  }

  let chromium
  try {
    chromium = await loadChromium(cfg.playwrightEntry)
  } catch (e) {
    console.log(JSON.stringify({ ok: false, reason: 'playwright_unavailable: ' + short(e.message, 120) }))
    return 2
  }

  const consoleErrors = []
  const consoleWarnings = []
  const pageErrors = []
  const requestFailed = []
  const externalRequests = []

  let browser
  try {
    browser = await chromium.launch({
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    })
    const context = await browser.newContext({
      viewport: { width: cfg.width || 1440, height: cfg.height || 900 },
      deviceScaleFactor: 1,
    })
    const page = await context.newPage()
    page.setDefaultTimeout(cfg.navTimeout || 15000)

    page.on('console', (msg) => {
      const t = msg.type()
      const text = short(msg.text())
      if (t === 'error') consoleErrors.push(text)
      else if (t === 'warning') consoleWarnings.push(text)
    })
    page.on('pageerror', (err) => pageErrors.push(short(err && err.message ? err.message : err)))
    page.on('requestfailed', (req) => {
      requestFailed.push(short(req.url(), 140))
    })
    page.on('request', (req) => {
      const u = req.url()
      if (u.startsWith('http://') || u.startsWith('https://')) externalRequests.push(short(u, 140))
    })

    await page.goto(pathToFileURL(path.resolve(target)).href, {
      waitUntil: cfg.waitUntil || 'networkidle',
      timeout: cfg.navTimeout || 15000,
    }).catch(async (e) => {
      // networkidle 在有长轮询/动画的页面上可能永不触发，退化到 load 再试一次
      pageErrors.push('goto(' + (cfg.waitUntil || 'networkidle') + ') 失败: ' + short(e.message, 120))
      await page.goto(pathToFileURL(path.resolve(target)).href, {
        waitUntil: 'load', timeout: cfg.navTimeout || 15000,
      }).catch((e2) => pageErrors.push('goto(load) 失败: ' + short(e2.message, 120)))
    })

    await page.waitForTimeout(cfg.settleMs || 1200)

    const probe = await page.evaluate(() => {
      const out = {
        title: document.title || '',
        textLen: (document.body && document.body.innerText || '').trim().length,
        canvas: [],
        zeroSize: [],
        overflow: [],
        interactive: 0,
        nodes: document.getElementsByTagName('*').length,
      }
      document.querySelectorAll('canvas').forEach((c) => {
        out.canvas.push({ w: c.width, h: c.height, cw: c.clientWidth, ch: c.clientHeight })
      })
      document.querySelectorAll('svg').forEach((s) => {
        const r = s.getBoundingClientRect()
        if (r.width > 0 && r.height > 0) out.canvas.push({ svg: true, w: Math.round(r.width), h: Math.round(r.height) })
      })
      // 可见但尺寸塌陷的元素（图表没画出来 / 容器没高度的典型症状）
      document.querySelectorAll('[id]').forEach((el) => {
        const r = el.getBoundingClientRect()
        const cs = getComputedStyle(el)
        if (cs.display === 'none' || cs.visibility === 'hidden') return
        if (el.children.length === 0 && !el.textContent.trim()) return
        if (r.width > 40 && (r.height === 0 || r.width === 0)) {
          out.zeroSize.push({ id: el.id, w: Math.round(r.width), h: Math.round(r.height) })
        }
      })
      // 横向溢出 = 文字/内容超出父容器，会互相遮挡
      document.querySelectorAll('body *').forEach((el) => {
        const cs = getComputedStyle(el)
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') return
        if (el.scrollWidth - el.clientWidth > 12 && el.clientWidth > 40) {
          if (cs.overflowX === 'visible') {
            out.overflow.push({
              tag: el.tagName.toLowerCase(),
              id: el.id || '',
              cls: (el.className && String(el.className).slice(0, 40)) || '',
              over: el.scrollWidth - el.clientWidth,
            })
          }
        }
      })
      out.interactive = document.querySelectorAll('button, a[href], input, select, textarea, [role="button"]').length
      out.zeroSize = out.zeroSize.slice(0, 8)
      out.overflow = out.overflow.slice(0, 8)
      return out
    })

    // 交互探测：点前若干个按钮，看是否触发新的报错（不点提交/危险类）
    let clickProbed = 0
    const errsBefore = consoleErrors.length + pageErrors.length
    if (cfg.clickProbe !== false) {
      const handles = await page.$$('button, [role="button"], .chip, .tab, [data-verify-click]')
      for (const h of handles.slice(0, cfg.maxClicks || 4)) {
        try {
          const label = (await h.innerText().catch(() => '')) || ''
          if (/提交|删除|发送|下载|导出|submit|delete|send/i.test(label)) continue
          const visible = await h.isVisible().catch(() => false)
          if (!visible) continue
          await h.click({ timeout: 2000 }).catch(() => {})
          clickProbed++
          await page.waitForTimeout(cfg.clickSettleMs || 350)
        } catch {
          /* 单个元素点不动不影响整体结论 */
        }
      }
    }
    const clickErrors = (consoleErrors.length + pageErrors.length) - errsBefore

    let screenshot = null
    if (cfg.screenshotPath) {
      try {
        await page.screenshot({
          path: cfg.screenshotPath,
          fullPage: false,
          timeout: cfg.shotTimeout || 12000,
        })
        screenshot = cfg.screenshotPath
      } catch (e) {
        consoleWarnings.push('截图失败: ' + short(e.message, 100))
      }
    }

    const uniq = (a) => Array.from(new Set(a))
    const zeroCanvas = probe.canvas.filter((c) => !c.svg && (c.cw === 0 || c.ch === 0 || c.w === 0 || c.h === 0))

    const result = {
      ok: pageErrors.length === 0 && consoleErrors.length === 0 && zeroCanvas.length === 0,
      tier: cfg.screenshotPath ? 2 : 1,
      title: probe.title,
      text_len: probe.textLen,
      dom_nodes: probe.nodes,
      interactive_elements: probe.interactive,
      canvas_total: probe.canvas.length,
      canvas_zero_size: zeroCanvas.length,
      zero_size_ids: probe.zeroSize.map((z) => '#' + z.id + ' ' + z.w + 'x' + z.h),
      page_errors: uniq(pageErrors).slice(0, MAX_CONSOLE),
      console_errors: uniq(consoleErrors).slice(0, MAX_CONSOLE),
      console_warnings: uniq(consoleWarnings).slice(0, 6),
      request_failed: uniq(requestFailed).slice(0, 6),
      external_requests: uniq(externalRequests).slice(0, 6),
      overflow: probe.overflow.map((o) => (o.id ? '#' + o.id : o.tag + (o.cls ? '.' + o.cls : '')) + ' 溢出' + o.over + 'px').slice(0, MAX_OVERFLOW),
      click_probed: clickProbed,
      click_new_errors: clickErrors,
      layout_risk: probe.overflow.length > 0 || zeroCanvas.length > 0 || probe.zeroSize.length > 0,
      console_error_count: consoleErrors.length,
      page_error_count: pageErrors.length,
    }
    if (screenshot) result.screenshot = screenshot
    console.log(JSON.stringify(result))
    return 0
  } catch (e) {
    console.log(JSON.stringify({ ok: false, reason: 'render_error: ' + short(e && e.message ? e.message : e, 200) }))
    return 1
  } finally {
    if (browser) await browser.close().catch(() => {})
  }
}

main().then((code) => { process.exit(code || 0) }).catch((e) => {
  console.log(JSON.stringify({ ok: false, reason: 'fatal: ' + short(e && e.message ? e.message : e, 200) }))
  process.exit(1)
})
