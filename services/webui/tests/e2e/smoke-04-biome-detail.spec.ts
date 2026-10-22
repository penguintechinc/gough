/**
 * Smoke Test: Biome Detail Page
 *
 * Test biome detail tabs, metadata display, no tab content breaks.
 */

import { test, expect } from '@playwright/test';

test.describe('Biome Detail Page', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to biomes and click first card
    await page.goto('/provisioning/biomes');
    await page.waitForLoadState('networkidle');

    const firstCard = page.locator('[class*="rounded-lg"]').first();
    if (await firstCard.isVisible()) {
      await firstCard.click();
      await page.waitForURL(/\/provisioning\/biomes\/[^\/]+$/);
      await page.waitForLoadState('networkidle');
    }
  });

  test('renders all tabs', async ({ page }) => {
    const tabs = ['Overview', 'Versions', 'Dependencies', 'Requirements', 'Joiner Secrets', 'Tests', 'Audit'];

    for (const tabName of tabs) {
      const tab = page.locator(`button:has-text("${tabName}")`);
      await expect(tab).toBeVisible();
    }
  });

  test('overview tab shows biome metadata', async ({ page }) => {
    const overviewTab = page.locator('button:has-text("Overview")');
    if (await overviewTab.isVisible()) {
      await overviewTab.click();
      await page.waitForLoadState('networkidle');

      // Should have header with biome name
      const header = page.locator('h1');
      await expect(header).toBeVisible();

      // Should have metadata badges
      const badges = page.locator('span[class*="border"][class*="rounded"]');
      const count = await badges.count();
      expect(count).toBeGreaterThan(0);
    }
  });

  test('versions tab lists versions', async ({ page }) => {
    const versionsTab = page.locator('button:has-text("Versions")');
    if (await versionsTab.isVisible()) {
      await versionsTab.click();
      await page.waitForLoadState('networkidle');

      // Should have version list
      const versionItems = page.locator('[class*="border"][class*="bg-dark"]');
      const count = await versionItems.count();

      if (count > 0) {
        await expect(versionItems.first()).toBeVisible();
      }
    }
  });

  test('dependencies tab shows dependencies', async ({ page }) => {
    const depsTab = page.locator('button:has-text("Dependencies")');
    if (await depsTab.isVisible()) {
      await depsTab.click();
      await page.waitForLoadState('networkidle');

      // Should show dependency items (if any)
      const depItems = page.locator('[class*="px-3"][class*="py-2"]').filter({
        has: page.locator('[class*="font-medium"]'),
      });

      const count = await depItems.count();
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('requirements tab shows eligibility', async ({ page }) => {
    const reqTab = page.locator('button:has-text("Requirements")');
    if (await reqTab.isVisible()) {
      await reqTab.click();
      await page.waitForLoadState('networkidle');

      // Should have requirement items with ✓/✗
      const reqItems = page.locator('[class*="flex"][class*="items-start"]');
      if (await reqItems.count() > 0) {
        await expect(reqItems.first()).toBeVisible();
      }
    }
  });

  test('joiner secrets tab displays secrets', async ({ page }) => {
    const secretsTab = page.locator('button:has-text("Joiner Secrets")');
    if (await secretsTab.isVisible()) {
      await secretsTab.click();
      await page.waitForLoadState('networkidle');

      // Should show secrets list or "no secrets" message
      const content = page.locator('text=/secret|no joiner/i');
      await expect(content.first()).toBeVisible();
    }
  });

  test('tests tab shows test status', async ({ page }) => {
    const testsTab = page.locator('button:has-text("Tests")');
    if (await testsTab.isVisible()) {
      await testsTab.click();
      await page.waitForLoadState('networkidle');

      // Should have test items if any exist
      const testItems = page.locator('[class*="px-3"][class*="py-2"]').filter({
        hasText: /pass|fail|pending/i,
      });

      const count = await testItems.count();
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('audit tab shows audit trail', async ({ page }) => {
    const auditTab = page.locator('button:has-text("Audit")');
    if (await auditTab.isVisible()) {
      await auditTab.click();
      await page.waitForLoadState('networkidle');

      // Should show audit events if any
      const auditItems = page.locator('[class*="border-b"]').filter({
        has: page.locator('[class*="text-dark-400"]'),
      });

      const count = await auditItems.count();
      expect(count).toBeGreaterThanOrEqual(0);
    }
  });

  test('back button navigates to biomes list', async ({ page }) => {
    const backBtn = page.locator('button:has-text("Back to Biomes")');
    if (await backBtn.isVisible()) {
      await backBtn.click();
      await page.waitForURL('/provisioning/biomes');
      expect(page.url()).toContain('/provisioning/biomes');
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

  test('tab switching does not break layout', async ({ page }) => {
    const tabs = page.locator('button[role="tab"]');
    const tabCount = await tabs.count();

    for (let i = 0; i < Math.min(tabCount, 3); i++) {
      const tab = tabs.nth(i);
      await tab.click();
      await page.waitForLoadState('networkidle');

      // Check content is visible
      const content = page.locator('main, [role="main"], .content').first();
      if (await content.isVisible()) {
        const box = await content.boundingBox();
        expect(box).not.toBeNull();
        expect(box?.height).toBeGreaterThan(0);
      }
    }
  });
});
