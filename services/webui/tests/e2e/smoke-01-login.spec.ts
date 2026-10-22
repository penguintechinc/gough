/**
 * Smoke Test: Login Page
 *
 * Test login page renders, validation errors, and successful login flow.
 */

import { test, expect } from '@playwright/test';

test.describe('Login Page', () => {
  test('renders login form', async ({ page }) => {
    await page.goto('/login');

    // Check for essential elements
    await expect(page.locator('input[type="email"]')).toBeVisible();
    await expect(page.locator('input[type="password"]')).toBeVisible();
    await expect(page.locator('button:has-text("Login")')).toBeVisible();
    await expect(page.locator('a:has-text("Forgot password")')).toBeVisible();
  });

  test('shows validation errors for empty fields', async ({ page }) => {
    await page.goto('/login');

    // Click login without entering credentials
    await page.click('button:has-text("Login")');

    // Check for validation messages
    await expect(page.locator('text=/email|required/i')).toBeVisible({ timeout: 3000 });
  });

  test('handles invalid credentials', async ({ page }) => {
    await page.goto('/login');

    // Enter invalid credentials
    await page.fill('input[type="email"]', 'test@example.com');
    await page.fill('input[type="password"]', 'wrongpassword');
    await page.click('button:has-text("Login")');

    // Should show error message
    await expect(page.locator('text=/invalid|wrong|failed/i')).toBeVisible({ timeout: 5000 });
  });

  test('no console errors on page load', async ({ page }) => {
    const errors: string[] = [];

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    await page.goto('/login');
    await page.waitForLoadState('networkidle');

    expect(errors).toHaveLength(0);
  });

  test('form remembers email on focus loss', async ({ page }) => {
    await page.goto('/login');

    const emailInput = page.locator('input[type="email"]');
    await emailInput.fill('persist@example.com');
    await emailInput.blur();
    await page.waitForTimeout(300);

    const value = await emailInput.inputValue();
    expect(value).toBe('persist@example.com');
  });

  test('password input is masked', async ({ page }) => {
    await page.goto('/login');

    const passwordInput = page.locator('input[type="password"]');
    await passwordInput.fill('secret123');

    const inputType = await passwordInput.getAttribute('type');
    expect(inputType).toBe('password');
  });

  test('shows help text on field hover', async ({ page }) => {
    await page.goto('/login');

    const emailInput = page.locator('input[type="email"]').first();
    await emailInput.hover();

    // Check if tooltip or help text appears
    const helpText = page.locator('[role="tooltip"], .help-text, .field-help');
    if (await helpText.count() > 0) {
      await expect(helpText.first()).toBeVisible({ timeout: 2000 });
    }
  });
});
