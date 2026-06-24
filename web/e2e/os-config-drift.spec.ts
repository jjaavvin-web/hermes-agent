import { test, expect } from '@playwright/test';

test.describe('OS capstone config-drift truth gate', () => {
  test('renders config drift from combined wrapper result', async ({ page }) => {
    const apiResponses: unknown[] = [];
    page.on('response', async (response) => {
      if (response.url().includes('/api/dashboard/os') && response.status() === 200) {
        try { apiResponses.push(await response.json()); } catch { /* ignore */ }
      }
    });

    await page.goto('/os');
    await expect(page.getByRole('heading', { name: /systems|finding/i })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('button', { name: /config drift/i })).toContainText(/combined rc=0/i, { timeout: 30_000 });

    await page.getByRole('button', { name: /^Grid$/ }).click();
    await page.locator('#os-card-infra').getByRole('button').click();
    await expect(page.getByText('Config/authz/authority')).toBeVisible();
    await expect(page.locator('#os-card-infra').getByText(/config_drift=0 authz_drift=0 authority_drift=0/).first()).toBeVisible();
    await page.screenshot({ path: '/home/josep/.hermes/audits/20260624-worldclass-burn/capstone-truth/screenshots/os-config-drift-combined-rc.png', fullPage: true });

    const snapshot = apiResponses.at(-1) as any;
    expect(snapshot).toBeTruthy();
    expect(snapshot.infra.config_drift.combined_rc).toBe(0);
    expect(snapshot.infra.config_drift.config_drift).toBe(0);
    expect(snapshot.infra.config_drift.authz_drift).toBe(0);
    expect(snapshot.infra.config_drift.authority_drift).toBe(0);
  });
});
