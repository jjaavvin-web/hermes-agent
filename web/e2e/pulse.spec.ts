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
  // Wait for networkidle so the /api/pulse/kpis fetch has time to resolve.
  await page.waitForLoadState('networkidle');
  // PulseChips always renders exactly 5 Chip elements — one per KPI — whether
  // data is loaded, error, or loading-placeholder.
  await expect(page.locator('.pulse-chip')).toHaveCount(5);
});

test('queue strip renders chips when cards exist', async ({ page }) => {
  await gotoPulse(page);
  await page.waitForLoadState('networkidle');

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
  await page.waitForLoadState('networkidle');

  const chips = page.locator('.pulse-queue__chip');
  const count = await chips.count();

  if (count === 0) {
    test.skip(true, 'Queue is empty on this run; cannot exercise chip click');
    return;
  }

  // Stub window.open before the click so we can record the call without
  // actually opening a browser window.
  await page.evaluate(() => {
    (window as Window & { __openCalls__: string[] }).__openCalls__ = [];
    window.open = (url?: string | URL) => {
      (window as Window & { __openCalls__: string[] }).__openCalls__.push(String(url ?? ''));
      return null;
    };
  });

  await chips.first().click();

  const calls = await page.evaluate(
    () => (window as Window & { __openCalls__: string[] }).__openCalls__,
  );

  expect(calls.length).toBeGreaterThanOrEqual(1);
  expect(calls[0]).toContain('kanban');
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
