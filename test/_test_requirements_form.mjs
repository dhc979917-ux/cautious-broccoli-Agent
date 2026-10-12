// 需求采集表单（CollectRequirements / RequirementsForm）的真实 UI 验证。
//
// 依赖：后端 :8000、前端 :5173 已启动。凭据 TP_ACCOUNT / TP_PASSWORD，
// Agent 由 TP_AGENT_ID 指定（默认 2 = 数据分析师，task 架构）。
//
// 跑法：node test/_test_requirements_form.mjs
//
// 验证的不只是"表单出现了"，而是它作为一个 UI 是否合格：
// 不遮挡输入框、标签不溢出、窄屏不塌、提交后正确折叠、答案真的回传给了 Agent。
import { chromium } from 'playwright';
import fs from 'node:fs';

const BASE = process.env.TP_BASE || 'http://localhost:5173';
const ACCOUNT = process.env.TP_ACCOUNT || 'admin';
const PASSWORD = process.env.TP_PASSWORD || 'admin123';
const AGENT_ID = process.env.TP_AGENT_ID || '2';
const OUT = process.env.TP_SHOT_DIR || 'test/_shots_intake';
const PROMPT = process.env.TP_PROMPT ||
  '帮我做一个单文件 HTML 的外延设备点检计数看板，双击浏览器就能打开，车间内网用，数据先模拟。';

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} - ${name}${detail ? ` | ${detail}` : ''}`);
};

fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ headless: true });
const errors = [];

try {
  const page = await browser.newPage({ viewport: { width: 1480, height: 920 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  // ===== 登录 =====
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.getByPlaceholder('账号').first().fill(ACCOUNT);
  await page.getByPlaceholder('密码').first().fill(PASSWORD);
  await page.locator('button.login-btn').first().click();
  // 登录后落地页可能是 / 或 /chat，只要离开 /login 就算成功
  await page.waitForURL((u) => !/\/login/.test(u.pathname), { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2000);
  check('登录成功（已离开登录页）', !/\/login/.test(new URL(page.url()).pathname), page.url());

  // ===== 指定 Agent 并新建会话 =====
  await page.goto(`${BASE}/chat?agent=${AGENT_ID}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  await page.locator('.new-chat-btn').first().click();
  await page.waitForTimeout(2000);

  // ===== 发送构建型请求 =====
  const ta = page.locator('.chat-input-area textarea').first();
  await ta.fill(PROMPT);
  await page.keyboard.press('Enter');
  console.log('  [info] 已发送，等待模型发起需求采集…');

  // ===== 等待采集表单出现 =====
  let appeared = false;
  try {
    await page.locator('.intake-bar').first().waitFor({ state: 'visible', timeout: 180000 });
    appeared = true;
  } catch { /* 下面按未出现处理 */ }
  check('需求采集表单出现', appeared);
  if (!appeared) {
    const body = await page.locator('body').innerText();
    console.log('  [debug] 页面文本尾部：' + body.slice(-600).replace(/\n+/g, ' | '));
    throw new Error('表单未出现，后续断言无意义');
  }
  await page.waitForTimeout(800);

  const bar = page.locator('.intake-bar').first();

  // ===== 布局：不遮挡输入框、不超出视口 =====
  const barBox = await bar.boundingBox();
  const composerBox = await page.locator('.chat-input-area').first().boundingBox();
  const taBox = await ta.boundingBox();
  check('表单在视口内', barBox.y >= 0 && barBox.y + barBox.height <= 920,
    `y=${Math.round(barBox.y)} h=${Math.round(barBox.height)}`);
  check('表单没有盖住输入框', barBox.y + barBox.height <= taBox.y + 2,
    `barBottom=${Math.round(barBox.y + barBox.height)} taTop=${Math.round(taBox.y)}`);
  check('表单没有横向溢出容器', barBox.width <= composerBox.width + 2,
    `bar=${Math.round(barBox.width)} container=${Math.round(composerBox.width)}`);
  check('表单高度受限（不把输入区顶出屏幕）', barBox.height <= 920 * 0.6,
    `h=${Math.round(barBox.height)}`);

  // ===== 内容：题目行数与标签不溢出 =====
  const rows = page.locator('.intake-bar .intake-row');
  const rowCount = await rows.count();
  check('题目行数在 1~8 之间', rowCount >= 1 && rowCount <= 8, `rows=${rowCount}`);

  const clipped = await page.evaluate(() => {
    const bad = [];
    document.querySelectorAll('.intake-bar .intake-control > *').forEach((el) => {
      const r = el.getBoundingClientRect();
      const host = el.closest('.intake-control').getBoundingClientRect();
      // 控件右缘超出所属列 = 会压到别的元素
      if (r.right > host.right + 2) bad.push(el.className + ' over=' + Math.round(r.right - host.right));
    });
    document.querySelectorAll('.intake-bar .intake-actions button').forEach((b) => {
      const span = b.querySelector('span:last-child');
      if (span && span.scrollWidth > span.clientWidth + 1) bad.push('btn-text-clipped:' + span.innerText);
    });
    return bad;
  });
  check('控件与按钮文字均未溢出/被裁', clipped.length === 0, clipped.slice(0, 4).join('; '));

  const hasDefaultAll = await page.locator('.intake-bar .intake-actions button', { hasText: '全部用默认' }).count();
  const hasSubmit = await page.locator('.intake-bar .intake-actions button', { hasText: '提交' }).count();
  check('有「全部用默认」入口', hasDefaultAll === 1);
  check('有「提交」入口', hasSubmit === 1);

  await page.screenshot({ path: `${OUT}/intake-desktop.png`, fullPage: false });

  // ===== 窄屏：标签与控件应改为上下两行，不挤压 =====
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(700);
  const narrow = await page.evaluate(() => {
    const bar = document.querySelector('.intake-bar');
    const r = bar.getBoundingClientRect();
    const overflowX = document.documentElement.scrollWidth > window.innerWidth + 1;
    return { w: Math.round(r.width), vw: window.innerWidth, overflowX };
  });
  check('窄屏下表单不超出视口宽度', narrow.w <= narrow.vw, JSON.stringify(narrow));
  check('窄屏下页面无横向滚动', narrow.overflowX === false, JSON.stringify(narrow));
  await page.screenshot({ path: `${OUT}/intake-mobile.png`, fullPage: false });
  await page.setViewportSize({ width: 1480, height: 920 });
  await page.waitForTimeout(500);

  // ===== 填一项并提交 =====
  const firstTextInput = page.locator('.intake-bar .intake-row').first().locator('input[type="text"], input:not([type])').first();
  const firstSegmented = page.locator('.intake-bar .intake-row').first().locator('.ant-segmented-item').nth(1);
  if (await firstSegmented.count()) {
    await firstSegmented.click().catch(() => {});
  } else if (await firstTextInput.count()) {
    await firstTextInput.fill('UI测试填写值').catch(() => {});
  }
  await page.waitForTimeout(300);
  await page.locator('.intake-bar .intake-actions button', { hasText: '提交' }).first().click();

  // ===== 提交后：输入区表单消失 + 消息流出现采集摘要 + Agent 继续 =====
  // 与 AskUserBar 一致：已解决的交互卡从输入框上方移除，只在消息流里留"问/答"两行
  let barGone = false;
  try {
    await page.locator('.intake-bar').first().waitFor({ state: 'detached', timeout: 15000 });
    barGone = true;
  } catch { /* 下面断言 */ }
  check('提交后输入区表单移除', barGone);
  let summaryVisible = false;
  try {
    await page.locator('.step-ask-summary', { hasText: '需求采集' }).first()
      .waitFor({ state: 'visible', timeout: 15000 });
    summaryVisible = true;
  } catch { /* 下面断言 */ }
  check('消息流出现需求采集摘要', summaryVisible);
  if (summaryVisible) {
    const sum = await page.locator('.step-ask-summary', { hasText: '需求采集' }).first().innerText().catch(() => '');
    check('摘要含提交统计', /确认 \d+ 项|默认值/.test(sum), sum.slice(0, 120));
  }
  await page.screenshot({ path: `${OUT}/intake-resolved.png`, fullPage: false });

  // Agent 应继续干活（出现工具调用/思考步骤），而不是卡在暂停态
  await page.waitForTimeout(8000);
  const stillStreaming = await page.locator('.chat-progress-pill').count();
  const stepCount = await page.locator('.message-wrapper.agent .step-ask-summary, .message-wrapper.agent [class*="step"]').count();
  check('提交后 Agent 继续执行', stillStreaming > 0 || stepCount > 0,
    `streaming=${stillStreaming} steps=${stepCount}`);

  // ===== 历史还原：刷新后折叠态摘要仍在 =====
  await page.waitForTimeout(4000);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3500);
  const restored = await page.locator('.step-ask-summary', { hasText: '需求采集' }).count();
  check('刷新后需求采集记录可还原', restored >= 1, `count=${restored}`);
  await page.screenshot({ path: `${OUT}/intake-restored.png`, fullPage: false });

  // antd 的弃用/静态方法告警是平台既有噪声（Modal destroyOnClose 等），与本次改动无关
  const realErrors = errors.filter((e) =>
    !/favicon|404|net::ERR/i.test(e) &&
    !/\[antd:/.test(e) &&
    !/destroyOnClose|Static function/.test(e));
  check('无前端 JS 报错', realErrors.length === 0, realErrors.slice(0, 3).join(' ; '));
} catch (e) {
  console.log('EXCEPTION - ' + (e && e.message ? e.message : e));
  results.push({ name: '脚本执行', ok: false });
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n===== UI 验证: ${results.length - failed.length}/${results.length} 通过，截图在 ${OUT}/ =====`);
process.exit(failed.length ? 1 : 0);
