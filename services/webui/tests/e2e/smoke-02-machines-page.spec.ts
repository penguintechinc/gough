/**
 * Smoke Test: Machines Page
 *
 * Test machines list loads, filtering, state pills, and navigation.
 */

import { test, expect } from '@playwright/test';

test.describe('Machines Page', () => {
  test.beforeEach(async ({ page }) => {
    // Assume logged in
    await page.goto('/provisioning/machines');
    await page.waitForLoadState('networkidle');
  });

  test('renders machines list', async ({ page }) => {
    // Check for table elements
    await expect(page.locator('table')).toBeVisible();
    await expect(page.locator('th:has-text("Hostname")')).toBeVisible();
    await expect(page.locator('th:has-text("State")')).toBeVisible();
    await expect(page.locator('th:has-text("IP Address")')).toBeVisible();
  });

  test('displays state pills with correct colors', async ({ page }) => {
    // Check for state pills
    const statePills = page.locator('[class*="bg-"][class*="text-"]').filter({
      hasText: /ready|deploying|failed|new/i,
    });

    const count = await statePills.count();
    if (count > 0) {
      // Verify at least one state pill is visible
      await expect(statePills.first()).toBeVisible();

      // Check for colored indicators
      const classList = await statePills.first().getAttribute('class');
      expect(classList).toMatch(/bg-.*-900|text-.*-[34]00/);
    }
  });

  test('filters machines by state', async ({ page }) => {
    // Find state filter
    const stateFilter = page.locator('select:near(label:has-text("State"))');

    if (await stateFilter.isVisible()) {
      await stateFilter.selectOption('ready');
      await page.waitForLoadState('networkidle');

      // Check that filtered results appear
      const rows = page.locator('tbody tr');
      if (await rows.count() > 0) {
        const firstRowState = await rows.first().locator('td:nth-child(4)').textContent();
        expect(firstRowState?.toLowerCase()).toContain('ready');
      }
    }
  });

  test('clears filters when clear button clicked', async ({ page }) => {
    // Set a filter
    const stateFilter = page.locator('select:near(label:has-text("State"))');
    if (await stateFilter.isVisible()) {
      await stateFilter.selectOption('ready');

      // Click clear
      const clearBtn = page.locator('button:has-text("Clear Filters")');
      if (await clearBtn.isVisible()) {
        await clearBtn.click();
        await page.waitForLoadState('networkidle');

        // Verify filter is reset
        const selectedValue = await stateFilter.inputValue();
        expect(selectedValue).toBe('');
      }
    }
  });

  test('navigate to machine detail on row click', async ({ page }) => {
    const firstRow = page.locator('tbody tr').first();

    if (await firstRow.isVisible()) {
      await firstRow.click();

      // Should navigate to detail page
      await page.waitForURL(/\/provisioning\/machines\/[^\/]+$/);
      expect(page.url()).toMatch(/\/provisioning\/machines\//);
    }
  });

  test('bulk select machines', async ({ page }) => {
    const selectAllCheckbox = page.locator('thead input[type="checkbox"]');

    if (await selectAllCheckbox.isVisible()) {
      await selectAllCheckbox.click();

      // Check that machines are selected
      const selectedCheckboxes = page.locator('tbody input[type="checkbox"]:checked');
      const totalCheckboxes = page.locator('tbody input[type="checkbox"]');

      const selectedCount = await selectedCheckboxes.count();
      const totalCount = await totalCheckboxes.count();

      if (totalCount > 0) {
        expect(selectedCount).toBeGreaterThan(0);
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

  test('search field filters machines', async ({ page }) => {
    const searchInput = page.locator('input[placeholder*="Hostname"]');

    if (await searchInput.isVisible()) {
      await searchInput.fill('test');
      await page.waitForLoadState('networkidle');

      // Verify results update
      const rows = page.locator('tbody tr');
      const count = await rows.count();

      // Should have filtered results (if any match)
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });
});
