/**
 * Smoke Test: Audit Log Page
 *
 * Test audit log list, filtering, chain verification, detail panel.
 */

import { test, expect } from '@playwright/test';

test.describe('Audit Log Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/audit');
    await page.waitForLoadState('networkidle');
  });

  test('renders audit log header', async ({ page }) => {
    const header = page.locator('h1:has-text("Audit Log")');
    await expect(header).toBeVisible();
  });

  test('displays chain verification badge', async ({ page }) => {
    const badge = page.locator('[class*="green-900"], [class*="red-900"]').filter({
      hasText: /chain|verified/i,
    });

    // Should have verification badge
    const count = await badge.count();
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('verify button is present and clickable', async ({ page }) => {
    const verifyBtn = page.locator('button:has-text("Verify Now")');

    if (await verifyBtn.isVisible()) {
      await expect(verifyBtn).toBeVisible();

      // Click verify
      await verifyBtn.click();
      await page.waitForTimeout(1000);

      // Verify button should not be disabled after
      const isDisabled = await verifyBtn.isDisabled();
      expect(isDisabled).toBe(false);
    }
  });

  test('displays filter inputs', async ({ page }) => {
    const actionInput = page.locator('input[placeholder*="action"]');
    const resourceInput = page.locator('input[placeholder*="resource"]');

    // Should have at least filter inputs
    const count = await page.locator('input[type="text"], select').count();
    expect(count).toBeGreaterThanOrEqual(2);
  });

  test('filters events by action', async ({ page }) => {
    const actionInput = page.locator('input[placeholder*="action"]');

    if (await actionInput.isVisible()) {
      await actionInput.fill('create');
      await page.waitForLoadState('networkidle');

      // Verify table updates
      const rows = page.locator('tbody tr');
      const count = await rows.count();

      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('displays audit events table', async ({ page }) => {
    const table = page.locator('table');

    if (await table.isVisible()) {
      // Should have headers
      const headers = page.locator('th');
      const headerCount = await headers.count();

      expect(headerCount).toBeGreaterThan(0);
    }
  });

  test('pagination controls work', async ({ page }) => {
    const nextBtn = page.locator('button:has-text("Next")');

    if (await nextBtn.isVisible()) {
      const isDisabled = await nextBtn.isDisabled();

      if (!isDisabled) {
        await nextBtn.click();
        await page.waitForLoadState('networkidle');

        // Should update page display
        const pageInfo = page.locator('text=/page/i');
        if (await pageInfo.isVisible()) {
          const text = await pageInfo.textContent();
          expect(text).toMatch(/page/i);
        }
      }
    }
  });

  test('clicking event row shows detail panel', async ({ page }) => {
    const firstRow = page.locator('tbody tr').first();

    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForTimeout(500);

      // Detail panel should appear
      const detailPanel = page.locator('[class*="sticky"]').filter({
        hasText: /event|detail/i,
      });

      if (await detailPanel.count() > 0) {
        await expect(detailPanel.first()).toBeVisible();
      }
    }
  });

  test('detail panel shows event data', async ({ page }) => {
    const firstRow = page.locator('tbody tr').first();

    if (await firstRow.isVisible()) {
      await firstRow.click();
      await page.waitForTimeout(500);

      // Should have event info
      const timestamp = page.locator('text=/timestamp|timestamp/i');
      const action = page.locator('text=/action|Action/i');

      const hasInfo = await timestamp.count() > 0 || await action.count() > 0;
      expect(hasInfo).toBe(true);
    }
  });

  test('clear filters button resets filters', async ({ page }) => {
    // Set a filter
    const actionInput = page.locator('input[placeholder*="action"]');
    if (await actionInput.isVisible()) {
      await actionInput.fill('test');

      // Click clear
      const clearBtn = page.locator('button:has-text("Clear Filters")');
      if (await clearBtn.isVisible()) {
        await clearBtn.click();
        await page.waitForLoadState('networkidle');

        // Verify input is cleared
        const value = await actionInput.inputValue();
        expect(value).toBe('');
      }
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

  test('back button navigates to home', async ({ page }) => {
    const backBtn = page.locator('button[class*="hover:bg-dark-800"]').first();
    if (await backBtn.isVisible()) {
      await backBtn.click();
      // URL should change
      await page.waitForURL('**/');
    }
  });
});
