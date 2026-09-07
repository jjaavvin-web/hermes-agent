import { test, expect } from '@playwright/test';

test.describe('OS tab live infra gates', () => {
  test('renders real security/evals/DR values and grid details', async ({ page }) => {
    const apiResponses: unknown[] = [];
    page.on('response', async (response) => {
      if (response.url().includes('/api/dashboard/os') && response.status() === 200) {
        try { apiResponses.push(await response.json()); } catch { /* ignore */ }
      }
    });

    await page.goto('/os');
    await page.screenshot({ path: '/home/josep/.hermes/audits/20260623-worldclass-top10/deliverables/os-tab-playwright-before-grid.png', fullPage: true });
    await expect(page.getByRole('heading', { name: /systems|finding/i })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('button', { name: /evals/i })).toContainText(/67%|worst/i, { timeout: 30_000 });
    await expect(page.getByRole('button', { name: /DR/i })).toContainText(/2\/4|untested/i, { timeout: 30_000 });
    await expect(page.getByRole('button', { name: /security/i })).toContainText(/0 breaches/i, { timeout: 30_000 });

    await page.getByRole('button', { name: /^Grid$/ }).click();
    await page.locator('#os-card-infra').getByRole('button').click();
    await page.screenshot({ path: '/home/josep/.hermes/audits/20260623-worldclass-top10/deliverables/os-tab-playwright-infra-expanded.png', fullPage: true });
    await expect(page.getByText('Per-holdout evals')).toBeVisible();
    await expect(page.locator('span').filter({ hasText: /^holdout_wave2\.jsonl$/ })).toBeVisible();
    await expect(page.getByText('DR stores')).toBeVisible();
    await expect(page.getByRole('cell', { name: 'honcho' })).toBeVisible();
    await expect(page.getByRole('cell', { name: 'app_state' })).toBeVisible();

    const snapshot = apiResponses.at(-1) as any;
    expect(snapshot).toBeTruthy();
    expect(snapshot.infra.security.breach_count).toBe(0);
    expect(snapshot.infra.evals.worst_holdout).toBe('holdout_wave2.jsonl');
    expect(snapshot.infra.evals.recall_at_k).toBeLessThan(0.7);
    expect(snapshot.infra.dr.rows.map((row: any) => row.store)).toEqual(['mvms', 'honcho', 'app_state', 'state_db']);
    expect(snapshot.infra.dr.status).not.toBe('green');
    expect(snapshot.sections.find((section: any) => section.id === 'infra').status).not.toBe('green');
  });
});
