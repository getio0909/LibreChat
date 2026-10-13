import type { BrowserContext } from '@playwright/test';

// 原有测试按英文控件名称定位，显式设置语言，避免依赖产品的默认语言。
export async function setEnglishLanguage(context: BrowserContext) {
  await context.addInitScript(() => {
    localStorage.setItem('lang', JSON.stringify('en'));
  });
}
