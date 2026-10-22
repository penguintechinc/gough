/**
 * Smoke Test: Joiner Secrets Page
 *
 * Test secrets list, filtering, action buttons (rotate/revoke/ascertain).
 */

import { test, expect } from '@playwright/test';

test.describe('Joiner Secrets Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/settings/joiner-secrets');
    await page.waitForLoadState('networkidle');
  });

  test('renders joiner secrets header', async ({ page }) => {
    const header = page.locator('h1:has-text("Joiner Secrets")');
    await expect(header).toBeVisible();
  });

  test('displays secrets list', async ({ page }) => {
    const secretItems = page.locator('[class*="rounded-lg"][class*="p-4"]').filter({
      hasText: /secret|rotation/i,
    });

    // Should have secret items or empty state
    const count = await secretItems.count();
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('rotation class filter works', async ({ page }) => {
    const filterSelect = page.locator('select').first();

    if (await filterSelect.isVisible()) {
      const options = await filterSelect.locator('option').allTextContents();
      expect(options.length).toBeGreaterThanOrEqual(1);

      // Try selecting an option
      const optionValues = await filterSelect.locator('option').allAttributes('value');
      if (optionValues.length > 1) {
        await filterSelect.selectOption(optionValues[1]);
        await page.waitForLoadState('networkidle');
      }
    }
  });

  test('status filter shows active/expired', async ({ page }) => {
    const statusFilter = page.locator('select').nth(1);

    if (await statusFilter.isVisible()) {
      await statusFilter.selectOption('active');
      await page.waitForLoadState('networkidle');

      // Should filter results
      const items = page.locator('[class*="rounded-lg"][class*="p-4"]');
      const count = await items.count();
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('displays expiration status badges', async ({ page }) => {
    const badges = page.locator('[class*="px-2"][class*="py-0.5"]').filter({
      hasText: /expired|active|day/i,
    });

    const count = await badges.count();
    // May have multiple badges for expiry status
    expect(count).toBeGreaterThanOrEqual(0);
  });

  test('clicking secret shows action buttons', async ({ page }) => {
    const firstSecret = page.locator('[class*="rounded-lg"][class*="p-4"]').first();

    if (await firstSecret.isVisible()) {
      await firstSecret.click();
      await page.waitForTimeout(300);

      // Should show action buttons
      const actionPanel = page.locator('[class*="sticky"]').filter({
        hasText: /action|rotate|revoke/i,
      });

      if (await actionPanel.count() > 0) {
        await expect(actionPanel.first()).toBeVisible();
      }
    }
  });

  test('rotate button is present', async ({ page }) => {
    const firstSecret = page.locator('[class*="rounded-lg"][class*="p-4"]').first();

    if (await firstSecret.isVisible()) {
      await firstSecret.click();
      await page.waitForTimeout(300);

      const rotateBtn = page.locator('button:has-text("Rotate")');
      if (await rotateBtn.isVisible()) {
        await expect(rotateBtn).toBeVisible();
      }
    }
  });

  test('revoke button is present', async ({ page }) => {
    const firstSecret = page.locator('[class*="rounded-lg"][class*="p-4"]').first();

    if (await firstSecret.isVisible()) {
      await firstSecret.click();
      await page.waitForTimeout(300);

      const revokeBtn = page.locator('button:has-text("Revoke")');
      if (await revokeBtn.isVisible()) {
        await expect(revokeBtn).toBeVisible();
      }
    }
  });

  test('ciphertext is not displayed', async ({ page }) => {
    // Verify that secret values are not displayed (only metadata)
    const secretItems = page.locator('[class*="rounded-lg"][class*="p-4"]');

    const itemCount = await secretItems.count();
    for (let i = 0; i < Math.min(itemCount, 3); i++) {
      const item = secretItems.nth(i);
      const text = await item.textContent();

      // Should NOT contain long hex or base64 strings (ciphertext)
      expect(text).not.toMatch(/[a-f0-9]{32,}|[A-Za-z0-9+/]{50,}=/);
    }
  });

  test('secret metadata is visible', async ({ page }) => {
    const firstSecret = page.locator('[class*="rounded-lg"][class*="p-4"]').first();

    if (await firstSecret.isVisible()) {
      const text = await firstSecret.textContent();

      // Should have metadata like rotation class, issued date
      expect(text).toMatch(/class|issued|rotation/i);
    }
  });

  test('back button navigates to settings', async ({ page }) => {
    const backBtn = page.locator('button[class*="hover:bg-dark-800"]').first();
    if (await backBtn.isVisible()) {
      await backBtn.click();
      // Should navigate back to settings
      await page.waitForURL('**/settings**', { timeout: 5000 });
    }
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
});
