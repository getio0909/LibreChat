import type { TranslationResource } from './i18n';
import {
  __resetLocaleForTests,
  __setLocaleLoaderForTests,
  changeLanguageSafely,
  ensureLocale,
  initializeI18n,
  detectInitialLanguage,
  normalizeLocale,
} from './i18n';
import Chinese from './zh-Hans/translation.json';
import English from './en/translation.json';
import Spanish from './es/translation.json';
import French from './fr/translation.json';
import { TranslationKeys } from '~/hooks';
import i18n from './i18n';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });

  return { promise, resolve, reject };
}

describe('i18next translation tests', () => {
  // Ensure i18next is initialized before any tests run
  beforeAll(async () => {
    await initializeI18n();
  });

  afterEach(async () => {
    await changeLanguageSafely('en');
  });

  it('should return the correct translation for a valid key in English', async () => {
    await changeLanguageSafely('en');
    expect(i18n.t('com_ui_examples')).toBe(English.com_ui_examples);
  });

  it('should return the correct translation for a valid key in French', async () => {
    await changeLanguageSafely('fr');
    expect(i18n.t('com_ui_examples')).toBe(French.com_ui_examples);
  });

  it('should return the correct translation for a valid key in Spanish', async () => {
    await changeLanguageSafely('es');
    expect(i18n.t('com_ui_examples')).toBe(Spanish.com_ui_examples);
  });

  it('should fallback to English for an invalid language code', async () => {
    // When an invalid language is provided, i18next should fallback to English
    await changeLanguageSafely('invalid-code');
    expect(i18n.t('com_ui_examples')).toBe(English.com_ui_examples);
  });

  it('should return the key itself for an invalid key', async () => {
    await changeLanguageSafely('en');
    expect(i18n.t('invalid-key' as TranslationKeys)).toBe('invalid-key'); // Returns the key itself
  });

  it('should correctly format placeholders in the translation', async () => {
    await changeLanguageSafely('en');
    expect(i18n.t('com_endpoint_default_with_num', { 0: 'John' })).toBe('default: John');

    await changeLanguageSafely('fr');
    expect(i18n.t('com_endpoint_default_with_num', { 0: 'Marie' })).toBe('par défaut : Marie');
  });

  it('should normalize language selector values to locale files', () => {
    expect(normalizeLocale('en-US')).toBe('en');
    expect(normalizeLocale('de-DE')).toBe('de');
    expect(normalizeLocale('fr-FR')).toBe('fr');
    expect(normalizeLocale('ar-EG')).toBe('ar');
    expect(normalizeLocale('he-IL')).toBe('he');
    expect(normalizeLocale('nl-NL')).toBe('nl');
    expect(normalizeLocale('pl-PL')).toBe('pl');
    expect(normalizeLocale('uk-UA')).toBe('uk');
    expect(normalizeLocale('zh-Hans')).toBe('zh-Hans');
    expect(normalizeLocale('zh-Hant')).toBe('zh-Hant');
    expect(normalizeLocale('pt-BR')).toBe('pt-BR');
    expect(normalizeLocale('pt-PT')).toBe('pt-PT');
  });

  it('should reuse an in-flight locale load', async () => {
    __resetLocaleForTests('sv');
    const pendingLocale = deferred<{ default: TranslationResource }>();
    const loadLocale = jest.fn(() => pendingLocale.promise);
    const restoreLoader = __setLocaleLoaderForTests('sv', loadLocale);

    const firstLoad = ensureLocale('sv-SE');
    const secondLoad = ensureLocale('sv-SE');

    expect(loadLocale).toHaveBeenCalledTimes(1);

    pendingLocale.resolve({ default: { com_ui_examples: 'svenska exempel' } });

    await expect(Promise.all([firstLoad, secondLoad])).resolves.toEqual(['sv', 'sv']);
    expect(i18n.getResource('sv', 'translation', 'com_ui_examples')).toBe('svenska exempel');

    restoreLoader();
    __resetLocaleForTests('sv');
  });

  it('should retry a locale load after a transient failure', async () => {
    __resetLocaleForTests('ka');
    const consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    let callCount = 0;
    const restoreLoader = __setLocaleLoaderForTests('ka', async () => {
      callCount += 1;
      if (callCount === 1) {
        throw new Error('temporary chunk failure');
      }

      return { default: { com_ui_examples: 'ქართული მაგალითები' } };
    });

    await expect(ensureLocale('ka-GE')).resolves.toBe('en');
    await expect(ensureLocale('ka-GE')).resolves.toBe('ka');

    expect(callCount).toBe(2);
    expect(i18n.getResource('ka', 'translation', 'com_ui_examples')).toBe('ქართული მაგალითები');

    restoreLoader();
    __resetLocaleForTests('ka');
    consoleErrorSpy.mockRestore();
  });

  it('should only apply the newest rapid language switch', async () => {
    __resetLocaleForTests('sv');
    __resetLocaleForTests('ka');
    __resetLocaleForTests('sl');

    const svLocale = deferred<{ default: TranslationResource }>();
    const kaLocale = deferred<{ default: TranslationResource }>();
    const slLocale = deferred<{ default: TranslationResource }>();
    const restoreSv = __setLocaleLoaderForTests('sv', () => svLocale.promise);
    const restoreKa = __setLocaleLoaderForTests('ka', () => kaLocale.promise);
    const restoreSl = __setLocaleLoaderForTests('sl', () => slLocale.promise);

    const firstSwitch = changeLanguageSafely('sv-SE');
    const secondSwitch = changeLanguageSafely('ka-GE');
    const latestSwitch = changeLanguageSafely('sl');

    svLocale.resolve({ default: { com_ui_examples: 'svenska exempel' } });
    await firstSwitch;
    expect(i18n.language).not.toBe('sv');

    kaLocale.resolve({ default: { com_ui_examples: 'ქართული მაგალითები' } });
    await secondSwitch;
    expect(i18n.language).not.toBe('ka');

    slLocale.resolve({ default: { com_ui_examples: 'slovenski primeri' } });
    await expect(latestSwitch).resolves.toBe('sl');
    expect(i18n.language).toBe('sl');
    expect(document.documentElement.lang).toBe('sl');

    restoreSv();
    restoreKa();
    restoreSl();
    __resetLocaleForTests('sv');
    __resetLocaleForTests('ka');
    __resetLocaleForTests('sl');
  });

  it('should restore the newest language if an older change finishes late', async () => {
    __resetLocaleForTests('sv');
    __resetLocaleForTests('sl');

    const svLocale = deferred<{ default: TranslationResource }>();
    const slLocale = deferred<{ default: TranslationResource }>();
    const restoreSv = __setLocaleLoaderForTests('sv', () => svLocale.promise);
    const restoreSl = __setLocaleLoaderForTests('sl', () => slLocale.promise);

    const firstSwitch = changeLanguageSafely('sv-SE');
    const latestSwitch = changeLanguageSafely('sl');

    slLocale.resolve({ default: { com_ui_examples: 'slovenski primeri' } });
    await expect(latestSwitch).resolves.toBe('sl');
    expect(i18n.language).toBe('sl');

    svLocale.resolve({ default: { com_ui_examples: 'svenska exempel' } });
    await firstSwitch;
    expect(i18n.language).toBe('sl');
    expect(document.documentElement.lang).toBe('sl');

    restoreSv();
    restoreSl();
    __resetLocaleForTests('sv');
    __resetLocaleForTests('sl');
  });
});

describe('Chinese-first interface defaults', () => {
  beforeEach(() => {
    localStorage.clear();
    document.cookie = 'lang=; Max-Age=0; path=/';
    jest.spyOn(navigator, 'language', 'get').mockReturnValue('en-US');
  });

  afterEach(async () => {
    localStorage.clear();
    document.cookie = 'lang=; Max-Age=0; path=/';
    jest.restoreAllMocks();
    await changeLanguageSafely('en');
  });

  it('loads Chinese login and settings strings before the first render on an English browser', async () => {
    expect(detectInitialLanguage()).toBe('zh-Hans');
    await initializeI18n();

    expect(i18n.language).toBe('zh-Hans');
    expect(document.documentElement.lang).toBe('zh-Hans');
    expect(i18n.t('com_auth_welcome_back')).toBe(Chinese.com_auth_welcome_back);
    expect(i18n.t('com_auth_password')).toBe(Chinese.com_auth_password);
    expect(i18n.t('com_auth_continue')).toBe(Chinese.com_auth_continue);
    expect(i18n.t('com_nav_settings')).toBe(Chinese.com_nav_settings);
    expect(i18n.t('com_nav_language')).toBe(Chinese.com_nav_language);
  });

  it.each(['en', '"en"'])('preserves a stored English preference: %s', async (value) => {
    localStorage.setItem('lang', value);
    await initializeI18n();

    expect(i18n.language).toBe('en');
    expect(localStorage.getItem('lang')).toBe(value);
  });

  it('preserves a language selected through the cookie', async () => {
    document.cookie = 'lang=fr; path=/';
    await initializeI18n();

    expect(i18n.language).toBe('fr');
  });

  it('still follows the browser when the user explicitly selects automatic language', async () => {
    localStorage.setItem('lang', JSON.stringify('auto'));
    await initializeI18n();

    expect(i18n.language).toBe('en');
  });
});
