#!/usr/bin/env node
const { chromium } = require('../node_modules/playwright');
const fs = require('fs');

const baseUrl = process.env.LEARNING_DASHBOARD_URL || 'http://127.0.0.1:9132';
const outDir = process.env.LEARNING_AUDIT_DIR || '/home/josep/.hermes/audits/20260623-worldclass-top10/deliverables';
fs.mkdirSync(outDir, { recursive: true });

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1400 } });
  const consoleErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleErrors.push(msg.text());
    }
  });
  await page.goto(`${baseUrl}/learning?smoke=${Date.now()}`, { waitUntil: 'networkidle', timeout: 30_000 });
  await page.waitForTimeout(1000);
  const auth401 = consoleErrors.filter((text) => text.includes('401') || text.includes('Unauthorized')).length;
  const fatalConsoleErrors = consoleErrors.filter((text) => !(text.includes('401') || text.includes('Unauthorized')));
  const api = await page.evaluate(async () => {
    const token = window.__HERMES_SESSION_TOKEN__;
    const res = await fetch('/api/dashboard/learning', { headers: { 'X-Hermes-Session-Token': token } });
    return { status: res.status, body: await res.json() };
  });
  assert(api.status === 200, `learning API status ${api.status}`);
  const body = api.body;
  assert(body.recall_eval?.recall_at_k === 0.6667, `expected blind recall 0.6667, got ${body.recall_eval?.recall_at_k}`);
  assert(body.recall_eval?.holdout_file?.endsWith('holdout_wave2.jsonl'), 'blind holdout provenance missing');
  assert(body.recall_eval?.self_seeded_latest?.recall_at_k !== body.recall_eval?.recall_at_k, 'self-seeded contrast must differ from blind headline');
  assert(Number.isInteger(body.recall_activity?.recent_24h), 'recent_24h missing');
  assert(body.promotion?.status === 'measured', 'promotion not measured');
  assert(body.verify?.critic_status === 'PASS', 'verify critic did not PASS');
  assert(body.mvms_lessons?.lessons_total === 522, `expected MVMS lessons 522, got ${body.mvms_lessons?.lessons_total}`);

  const text = await page.locator('body').innerText();
  assert(text.includes('LEARNING STATUS · HONEST/LIVE'), 'honest/live status not rendered');
  assert(text.includes('blind RECALL@10\n0.6667') || text.includes('BLIND HELD-OUT RECALL@10'), 'blind recall tile not rendered');
  assert(text.includes(String(body.recall_activity.recent_24h)), 'recall activity count not rendered');
  assert(text.includes('LEARNING-LOOP-PROMOTE.TIMER'), 'promotion tile not rendered');
  assert(text.includes('LEARNING-VERIFY.TIMER VERDICT'), 'verify tile not rendered');
  assert(text.includes('lessons_total\n522'), 'MVMS lessons count not rendered');
  assert(fatalConsoleErrors.length === 0, `console errors: ${fatalConsoleErrors.join('; ')}`);

  const screenshot = `${outDir}/learning-playwright-smoke.png`;
  await page.screenshot({ path: screenshot, fullPage: true });
  const result = {
    ok: true,
    baseUrl,
    screenshot,
    auth_401_console_noise: auth401,
    recall_at_k: body.recall_eval.recall_at_k,
    holdout_file: body.recall_eval.holdout_file,
    self_seeded_contrast: body.recall_eval.self_seeded_latest,
    recall_activity_recent_24h: body.recall_activity.recent_24h,
    recall_activity_total: body.recall_activity.total_events,
    promotion_processed: body.promotion.latest?.processed,
    verify_critic_status: body.verify.critic_status,
    mvms_lessons_total: body.mvms_lessons.lessons_total,
  };
  fs.writeFileSync(`${outDir}/learning-playwright-smoke.json`, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
