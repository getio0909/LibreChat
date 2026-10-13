import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { load } from 'js-yaml';
import { expect, test } from '@playwright/test';
import Chinese from '../../../client/src/locales/zh-Hans/translation.json';
import { getE2EUser } from '../../setup/user';

// 使用全新英文浏览器，不继承原有英文测试账号的语言偏好。
test.use({ storageState: { cookies: [], origins: [] }, locale: 'en-US' });

test('test deployment declares the brand as its runtime title', async () => {
  const text = await readFile(path.resolve(__dirname, '../../../deploy/compose.yml'), 'utf8');
  const config = load(text) as { services: { api: { environment: { APP_TITLE: string } } } };
  expect(config.services.api.environment.APP_TITLE).toBe('庄严宝刹 AI 设计助手');
});

test('fresh login and signed-in settings default to Chinese and preserve explicit English', async ({
  page,
}, testInfo) => {
  test.setTimeout(60000);
  await page.addInitScript(() => {
    localStorage.setItem('color-theme', 'dark');
  });
  await page.goto('/login');
  const configResponse = await page.request.get('/api/config');
  expect(configResponse.ok()).toBeTruthy();
  const config = (await configResponse.json()) as { appTitle: string };

  await expect(page).toHaveTitle(config.appTitle);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-Hans');
  await expect(page.getByRole('heading', { name: Chinese.com_auth_welcome_back })).toBeVisible();
  await expect(page.getByLabel(Chinese.com_auth_password, { exact: true })).toBeVisible();
  await expect(page.getByTestId('login-button')).toHaveText(Chinese.com_auth_continue);
  await page.screenshot({ path: testInfo.outputPath('login-chinese.png') });

  const user = getE2EUser();
  await page.locator('#email').fill(user.email);
  await page.locator('#password').fill(user.password);
  await page.getByTestId('login-button').click();
  await expect(page).toHaveURL(/\/c\/new/);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-Hans');
  await page.getByTestId('nav-user').click();
  await page.getByRole('menuitem', { name: Chinese.com_nav_settings }).click();

  const language = page.getByRole('combobox', { name: Chinese.com_nav_language });
  await expect(language).toHaveText(Chinese.com_nav_lang_chinese);
  await page.screenshot({ path: testInfo.outputPath('settings-chinese.png') });
  await language.click();
  await page.getByRole('option', { name: Chinese.com_nav_lang_english, exact: true }).click();
  // 语言选择器保存 en-US；重新载入时再由 i18n 规范化为 en。
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US');
  await expect(page.locator('#language-selector-label')).toHaveText('Language');
  expect(await page.evaluate(() => localStorage.getItem('lang'))).toBe('"en-US"');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await page.getByTestId('nav-user').click();
  await page.getByRole('menuitem', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Language English', exact: true })).toHaveText(
    'English',
  );
});
