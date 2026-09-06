import { chromium } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

const baseUrl = process.env.BASE_URL ?? 'http://127.0.0.1:9129';
const screenshotPath = process.env.SCREENSHOT_PATH ?? '/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/cost-verify.png';
const apiPath = process.env.API_JSON_PATH ?? '/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/cost-api-verify.json';

const root = await fetch(`${baseUrl}/`, { headers: { accept: 'text/html' } });
if (!root.ok) throw new Error(`root fetch failed: ${root.status}`);
const html = await root.text();
const match = html.match(/window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"/);
if (!match) throw new Error('dashboard session token not found in root HTML');
const token = match[1];

const api = await fetch(`${baseUrl}/api/dashboard/cost`, {
  headers: { 'X-Hermes-Session-Token': token },
});
if (!api.ok) throw new Error(`cost API failed: ${api.status}`);
const json = await api.json();
await writeFile(apiPath, JSON.stringify(json, null, 2), { encoding: 'utf-8' });

if (!Array.isArray(json.dailySeries) || json.dailySeries.length <= 0) {
  throw new Error(`dailySeries missing/empty: ${JSON.stringify(json.dailySeries)}`);
}
const cl = json.cacheLatency7d;
for (const key of ['cacheHitRatio', 'avgLatencyMs', 'p95LatencyMs']) {
  if (!cl || typeof cl[key] !== 'number' || Number.isNaN(cl[key])) {
    throw new Error(`cacheLatency7d.${key} missing/non-numeric`);
  }
}
for (const key of ['today', 'last7d', 'meteredLeak', 'meteredLeakCount', 'meteredLeakCostUsd']) {
  if (!(key in json)) throw new Error(`legacy field missing: ${key}`);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  extraHTTPHeaders: { 'X-Hermes-Session-Token': token },
  viewport: { width: 1440, height: 1000 },
});
const page = await context.newPage();
await page.goto(`${baseUrl}/cost?token=${encodeURIComponent(token)}`, { waitUntil: 'networkidle' });
const spark = page.locator('svg[data-testid="cost-daily-spark"]');
await spark.waitFor({ state: 'visible', timeout: 10_000 });
const rectCount = await spark.locator('rect').count();
if (rectCount <= 0) throw new Error(`spark rect count not >0: ${rectCount}`);
await page.locator('[data-testid="cost-cache-chip"]').waitFor({ state: 'visible', timeout: 10_000 });
await page.locator('[data-testid="cost-latency-chip"]').waitFor({ state: 'visible', timeout: 10_000 });
await page.screenshot({ path: screenshotPath, fullPage: true });
await browser.close();

console.log(JSON.stringify({
  ok: true,
  baseUrl,
  dailySeriesLength: json.dailySeries.length,
  rectCount,
  screenshotPath,
  apiPath,
  cacheLatency7d: cl,
  legacyPresent: ['today', 'last7d', 'meteredLeak', 'meteredLeakCount', 'meteredLeakCostUsd'].every((k) => k in json),
}, null, 2));
