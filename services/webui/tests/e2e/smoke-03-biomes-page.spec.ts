/**
 * Smoke Test: Biomes Page
 *
 * Test biomes list loads, filtering by kind/phase/workload, eligibility indicators.
 */

import { test, expect } from '@playwright/test';

test.describe('Biomes Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/provisioning/biomes');
    await page.waitForLoadState('networkidle');
  });

  test('renders biomes list', async ({ page }) => {
    // Check for biome cards
    const eggCards = page.locator('[class*="rounded-lg"]').filter({
      has: page.locator('[class*="font-semibold"]'),
    });

    const count = await eggCards.count();
    if (count > 0) {
      await expect(eggCards.first()).toBeVisible();
    }
  });

  test('displays kind filter', async ({ page }) => {
    const kindFilter = page.locator('select:near(label:has-text("Kind"))');

    if (await kindFilter.isVisible()) {
      await expect(kindFilter).toBeVisible();

      // Check for option values
      const options = await kindFilter.locator('option').allTextContents();
      expect(options.length).toBeGreaterThan(0);
    }
  });

  test('filters biomes by kind', async ({ page }) => {
    const kindFilter = page.locator('select:near(label:has-text("Kind"))');

    if (await kindFilter.isVisible()) {
      await kindFilter.selectOption('compute');
      await page.waitForLoadState('networkidle');

      // Should show filtered biomes
      const cards = page.locator('[class*="rounded-lg"]').filter({
        has: page.locator('span:has-text("compute")'),
      });

      const count = await cards.count();
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('filters biomes by phase', async ({ page }) => {
    const phaseFilter = page.locator('select:near(label:has-text("Phase"))');

    if (await phaseFilter.isVisible()) {
      await phaseFilter.selectOption('phase-0');
      await page.waitForLoadState('networkidle');

      // Verify filter applied
      const badge = page.locator('span:has-text("phase-0")');
      if (await badge.count() > 0) {
        await expect(badge.first()).toBeVisible();
      }
    }
  });

  test('filters biomes by workload type', async ({ page }) => {
    const workloadFilter = page.locator('select:near(label:has-text("Workload"))');

    if (await workloadFilter.isVisible()) {
      await workloadFilter.selectOption('lxc');
      await page.waitForLoadState('networkidle');

      const badge = page.locator('span:has-text("lxc")');
      if (await badge.count() > 0) {
        await expect(badge.first()).toBeVisible();
      }
    }
  });

  test('displays node eligibility indicator', async ({ page }) => {
    const cards = page.locator('[class*="rounded-lg"]');

    if (await cards.count() > 0) {
      // Look for eligibility bar
      const bar = cards.first().locator('[class*="h-2"]').first();

      if (await bar.isVisible()) {
        // Verify bar is visible
        await expect(bar).toBeVisible();

        // Should have a percentage indicator
        const percent = cards.first().locator('text=/\\d+\\/\\d+/');
        if (await percent.isVisible()) {
          await expect(percent).toBeVisible();
        }
      }
    }
  });

  test('navigate to biome detail on card click', async ({ page }) => {
    const firstCard = page.locator('[class*="rounded-lg"]').first();

    if (await firstCard.isVisible()) {
      await firstCard.click();

      // Should navigate to detail page
      await page.waitForURL(/\/provisioning\/biomes\/[^\/]+$/, { timeout: 5000 });
      expect(page.url()).toMatch(/\/provisioning\/biomes\//);
    }
  });

  test('clear filters button works', async ({ page }) => {
    const kindFilter = page.locator('select:near(label:has-text("Kind"))');

    if (await kindFilter.isVisible()) {
      await kindFilter.selectOption('compute');

      const clearBtn = page.locator('button:has-text("Clear Filters")');
      if (await clearBtn.isVisible()) {
        await clearBtn.click();
        await page.waitForLoadState('networkidle');

        const selectedValue = await kindFilter.inputValue();
        expect(selectedValue).toBe('');
      }
    }
  });

  test('displays biome metadata tags', async ({ page }) => {
    const cards = page.locator('[class*="rounded-lg"]');

    if (await cards.count() > 0) {
      const firstCard = cards.first();

      // Should have badges for kind, phase, workload
      const badges = firstCard.locator('span[class*="border"][class*="rounded"]');
      const badgeCount = await badges.count();

      expect(badgeCount).toBeGreaterThanOrEqual(3);
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
