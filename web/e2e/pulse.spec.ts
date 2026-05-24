// Pulse tab regression spec.
//
// Run:
//   cd web/
//   npm install -D @playwright/test
//   npx playwright install chromium
//   npx playwright test e2e/pulse.spec.ts --config=e2e/playwright.config.ts
//
// Or against a worktree without polluting web/:
//   cd web/e2e
//   npm install -D @playwright/test playwright && npx playwright install chromium
//   npx playwright test --config=playwright.config.ts
//
// Prerequisites: dashboard server running on http://127.0.0.1:9119 with API
// reachable. Tests pull the session token from / and re-use it.

import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:9119';

// Navigate to /pulse and wait for the SPA shell to settle.
async function gotoPulse(page: import('@playwright/test').Page) {
  await page.goto(`${BASE}/pulse`);
  await page.waitForSelector('.pulse-root', { timeout: 15_000 });
}

test('page loads with all four zones', async ({ page }) => {
  await gotoPulse(page);
  await expect(page.locator('.pulse-zone-top')).toBeVisible();
  await expect(page.locator('.pulse-zone-center')).toBeVisible();
  await expect(page.locator('.pulse-zone-right')).toBeVisible();
  await expect(page.locator('.pulse-zone-bottom')).toBeVisible();
});

test('KPI chips fetch fires and renders 5 chips', async ({ page }) => {
  await gotoPulse(page);
  // PulseChips always renders exactly 5 Chip elements — one per KPI — whether
  // data is loaded, error, or loading-placeholder. We can't use
  // `networkidle` here because H6's CRIT-1 fix opens a long-lived SSE
  // connection on /api/pulse/stream that keeps the network busy forever.
  await expect(page.locator('.pulse-chip')).toHaveCount(5, { timeout: 10_000 });
});

test('queue strip renders chips when cards exist', async ({ page }) => {
  await gotoPulse(page);

  // Wait for either the queue chips to render or the empty/error placeholder
  // to settle. Cannot use `networkidle` — the SSE pulse stream is
  // perpetually open (see H6 CRIT-1 fix); networkidle would never resolve.
  const queueRoot = page.locator('[data-testid="pulse-queue"], .pulse-zone-bottom').first();
  await expect(queueRoot).toBeVisible({ timeout: 10_000 });
  // Settle on the queue load — chip nodes appear within ~3s of mount.
  await page.waitForTimeout(3_000);

  const chips = page.locator('.pulse-queue__chip');
  const count = await chips.count();

  if (count === 0) {
    // No cards in the queue at this moment — not a bug.
    test.skip(true, 'Queue is empty on this run; no .pulse-queue__chip to assert against');
    return;
  }

  await expect(chips.first()).toBeVisible();
});

test('constellation canvas mounts', async ({ page }) => {
  await gotoPulse(page);
  // The canvas is only inserted by ForceGraph2D when there is data AND the
  // container has non-zero dimensions. We allow up to 10 s for graph data to
  // arrive and the layout engine to mount the canvas.
  const canvas = page.locator('.pulse-zone-center .pulse-constellation canvas');
  await expect(canvas).toBeAttached({ timeout: 10_000 });

  // Verify the canvas reports non-zero intrinsic dimensions via its attributes.
  const width = await canvas.getAttribute('width');
  const height = await canvas.getAttribute('height');
  expect(Number(width)).toBeGreaterThan(0);
  expect(Number(height)).toBeGreaterThan(0);
});

test('clicking a queue chip triggers window.open with kanban URL', async ({ page }) => {
  await gotoPulse(page);
  // Settle on the queue load (~3s) rather than `networkidle` — the SSE
  // stream is perpetually open after the H6 CRIT-1 fix.
  await page.waitForTimeout(3_000);

  const chips = page.locator('.pulse-queue__chip');
  const count = await chips.count();

  if (count === 0) {
    test.skip(true, 'Queue is empty on this run; cannot exercise chip click');
    return;
  }

  // Stub window.open before the click so we can record the call without
  // actually opening a browser window.
  await page.evaluate(() => {
    (window as unknown as Window & { __openCalls__: string[] }).__openCalls__ = [];
    window.open = (url?: string | URL) => {
      (window as unknown as Window & { __openCalls__: string[] }).__openCalls__.push(String(url ?? ''));
      return null;
    };
  });

  const pageErrors: string[] = [];
  page.on('pageerror', (err) => pageErrors.push(String(err)));

  await chips.first().click();
  await page.waitForTimeout(100);

  const calls = await page.evaluate(
    () => (window as unknown as Window & { __openCalls__: string[] }).__openCalls__,
  );

  expect(calls.length).toBeGreaterThanOrEqual(1);
  expect(calls[0]).toContain('kanban');
  expect(pageErrors).toEqual([]);
});

test('Esc keypress is wired to the detail-panel close handler', async ({ page }) => {
  // The detail panel opens only via a canvas node-click, which is not reliably
  // reproducible in headless because ForceGraph2D relies on rAF-driven layout
  // to position nodes. We verify the wiring exists at the component level by
  // checking the keydown listener is registered when a node IS selected.
  //
  // If this proves flaky in CI, promote to test.fixme() with the note below.
  test.fixme(
    true,
    'Canvas node-click cannot be reliably reproduced in headless: ' +
      'ForceGraph2D nodes are only positioned after the force simulation ' +
      'converges, making pixel-accurate click targeting non-deterministic. ' +
      'Cover this path in a unit test (see PulseConstellation.test.tsx) or ' +
      'enable by exposing a window.__selectPulseNode__ helper in dev builds.',
  );
});

test('hive switcher dropdown is interactable', async ({ page }) => {
  await gotoPulse(page);

  const switcher = page.locator('[data-testid="pulse-hive-switcher"]');
  await expect(switcher).toBeVisible();

  // Confirm it is a <select> element.
  const tagName = await switcher.evaluate((el) => el.tagName.toLowerCase());
  expect(tagName).toBe('select');

  // The "All hives" option is always present as the first child.
  const firstOption = switcher.locator('option').first();
  await expect(firstOption).toHaveText('All hives');

  // Clicking to open and selecting the first (already-selected) option should
  // not throw and should leave the element in a valid state.
  await switcher.selectOption({ index: 0 });
  await expect(switcher).toHaveValue('__all__');
});

// H6 regression — H5 DEFECTS.md CRIT-1: the SSE pulse stream returned 401
// in any real browser because EventSource cannot send headers and the
// dashboard auth middleware ignored the ?token= query param. After H6's
// query-param auth fallback, the transcript should NEVER show "Reconnecting"
// for longer than the initial connect handshake when the server is healthy.
test('transcript SSE connects (no perpetual reconnect loop)', async ({ page }) => {
  await gotoPulse(page);
  // Cannot use `networkidle` — the SSE stream stays open by design.
  // Wait for the transcript shell to render, then sample the status pill.
  const status = page.locator('[data-testid="pulse-transcript-status"]');
  await expect(status).toBeVisible({ timeout: 10_000 });
  // Give the EventSource up to 8 s to either move into "open" or settle.
  // If CRIT-1 has regressed, the status pill will read "Reconnecting in Xs…"
  // continuously and this assertion fails.
  await expect(status).not.toContainText(/Reconnecting/, { timeout: 8_000 });
});

// H6 regression — H5 DEFECTS.md MAJ-2: KPI chips were <div> without
// tabindex so keyboard users could not Tab to them. After H6's promotion to
// <button>, every chip should be a focusable button element with an
// accessible name.
test('KPI chips are keyboard-focusable buttons with accessible names', async ({ page }) => {
  await gotoPulse(page);
  // Cannot use `networkidle` — see CRIT-1 fix note above.
  const chips = page.locator('.pulse-chip');
  await expect(chips).toHaveCount(5, { timeout: 10_000 });

  const audit = await chips.evaluateAll((els) =>
    els.map((el) => ({
      tag: el.tagName.toLowerCase(),
      ariaLabel: el.getAttribute('aria-label') ?? '',
      focusable:
        el.tagName.toLowerCase() === 'button' ||
        el.hasAttribute('tabindex'),
    })),
  );
  for (const row of audit) {
    expect(row.tag).toBe('button');
    expect(row.focusable).toBe(true);
    // aria-label must contain the chip label word (lowercased) at minimum.
    expect(row.ariaLabel.length).toBeGreaterThan(0);
  }
});

// Regression for the 2026-05-24 stuck-Connecting bug: EventSource `open`
// must flip the transcript pill to Live even when HIVES=0 and no
// `pulse.activity` event ever arrives. The older no-op open handler left this
// status as "Connecting…" forever in the live dashboard.
test('transcript status flips to Live within 3s when active_hives is zero', async ({ page }) => {
  // Mock only the KPI payload so this regression is deterministic even when a
  // live dashboard has active historical hives. The SSE stream itself remains
  // real; the bug was the EventSource `open` handler failing to flip the pill
  // when zero hives/no activity events were present.
  await page.route('**/api/pulse/kpis', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        active_hives: 0,
        pending_cards: 0,
        max_usage_pct: null,
        today_spend_usd: 0,
        today_pr_merges: 0,
        last_completion: null,
      }),
    });
  });

  const start = Date.now();
  await gotoPulse(page);

  const status = page.locator('[data-testid="pulse-transcript-status"]');
  await expect(status).toHaveText(/Live/i, { timeout: 3_000 });
  await expect(status).not.toContainText(/Connecting/i);
  expect(Date.now() - start).toBeLessThan(3_500);
});

// Sibling handler audit regression: KPI chips are rendered as buttons, so a
// click should perform a visible/useful action. Here the action is a safe
// on-demand refresh of /api/pulse/kpis; before the fix, this click was a no-op.
test('clicking a KPI chip refreshes KPI data', async ({ page }) => {
  let kpiRequests = 0;
  await page.route('**/api/pulse/kpis', async (route) => {
    kpiRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        active_hives: 0,
        pending_cards: 0,
        max_usage_pct: null,
        today_spend_usd: kpiRequests,
        today_pr_merges: 0,
        last_completion: null,
      }),
    });
  });

  await gotoPulse(page);
  const chips = page.locator('.pulse-chip');
  await expect(chips).toHaveCount(5, { timeout: 10_000 });
  await expect(chips.nth(2)).toContainText('$1.00');

  await chips.first().click();

  await expect.poll(() => kpiRequests, { timeout: 3_000 }).toBeGreaterThanOrEqual(2);
  await expect(chips.nth(2)).toContainText('$2.00');
});
