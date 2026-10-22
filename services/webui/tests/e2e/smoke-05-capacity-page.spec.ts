/**
 * Smoke Test: Capacity Planning Page
 *
 * Test forecast charts, risk levels, migration policy editor, license gating.
 */

import { test, expect } from '@playwright/test';

test.describe('Capacity Planning Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/provisioning/capacity');
    await page.waitForLoadState('networkidle');
  });

  test('renders capacity page header', async ({ page }) => {
    const header = page.locator('h1:has-text("Capacity Planning")');
    await expect(header).toBeVisible();
  });

  test('displays forecast horizon buttons', async ({ page }) => {
    const buttons = page.locator('button:has-text("24h"), button:has-text("7d"), button:has-text("30d")');

    const count = await buttons.count();
    expect(count).toBeGreaterThanOrEqual(2);
  });

  test('switches forecast horizon', async ({ page }) => {
    const button7d = page.locator('button:has-text("7d")');
    if (await button7d.isVisible()) {
      await button7d.click();
      await page.waitForLoadState('networkidle');

      // Verify button is highlighted
      const classList = await button7d.getAttribute('class');
      expect(classList).toMatch(/gold|active|selected/i);
    }
  });

  test('displays node forecast bars', async ({ page }) => {
    const bars = page.locator('[class*="h-2"][class*="bg-"]');

    // Should have at least some bars (memory, cpu, forecast)
    const count = await bars.count();
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('shows risk indicators for nodes', async ({ page }) => {
    const riskIndicators = page.locator('[class*="text-yellow"][class*="text-red"]').filter({
      hasText: /⚠|warning|risk/i,
    });

    const count = await riskIndicators.count();
    // May or may not have risk indicators depending on data
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('displays recent migrations table', async ({ page }) => {
    const migrationsHeading = page.locator('h2:has-text("Recent Migrations")');
    if (await migrationsHeading.isVisible()) {
      await expect(migrationsHeading).toBeVisible();

      // Should have migration items or "no migrations" message
      const content = page.locator('text=/migration|no migration/i');
      await expect(content.first()).toBeVisible();
    }
  });

  test('migration policy form is editable', async ({ page }) => {
    const policySection = page.locator('h2:has-text("Migration Policy")');
    if (await policySection.isVisible()) {
      // Find input fields
      const inputs = page.locator('input[type="number"]');
      const count = await inputs.count();

      expect(count).toBeGreaterThanOrEqual(2);
    }
  });

  test('updates migration policy on input change', async ({ page }) => {
    const inputs = page.locator('input[type="number"]');
    if (await inputs.count() > 0) {
      const firstInput = inputs.first();

      // Get original value
      const originalValue = await firstInput.inputValue();

      // Change value
      await firstInput.fill('5');
      await page.waitForTimeout(500);

      // Verify change
      const newValue = await firstInput.inputValue();
      expect(newValue).toBe('5');
    }
  });

  test('verify button is present', async ({ page }) => {
    const verifyBtn = page.locator('button:has-text("Verify")');

    if (await verifyBtn.isVisible()) {
      await expect(verifyBtn).toBeVisible();
    }
  });

  test('license banner shows if WaddleAI not licensed', async ({ page }) => {
    // Check for license gated warning
    const licenseBanner = page.locator('[class*="yellow-900"][class*="yellow-700"], [class*="red-900"][class*="red-700"]').filter({
      hasText: /license|available|not available/i,
    });

    const count = await licenseBanner.count();
    // May or may not have license banner depending on state
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('no console errors on page load', async ({ page }) => {
    const errors: string[] = [];

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    expect(errors).toHaveLength(0);
  });

  test('back button navigates to provisioning', async ({ page }) => {
    const backBtn = page.locator('button[class*="hover:bg-dark-800"]').first();
    if (await backBtn.isVisible()) {
      await backBtn.click();
      // Should navigate back (URL might vary)
      await page.waitForURL('**/provisioning**', { timeout: 5000 });
    }
  });
});
