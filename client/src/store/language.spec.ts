import { snapshot_UNSTABLE } from 'recoil';
import language from './language';

describe('signed-in language preference', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('defaults the settings language to Simplified Chinese', () => {
    const snapshot = snapshot_UNSTABLE();

    expect(snapshot.getLoadable(language.lang).valueOrThrow()).toBe('zh-Hans');
  });

  it('preserves an explicit English preference', () => {
    localStorage.setItem('lang', JSON.stringify('en'));
    const snapshot = snapshot_UNSTABLE();

    expect(snapshot.getLoadable(language.lang).valueOrThrow()).toBe('en');
    expect(localStorage.getItem('lang')).toBe('"en"');
  });
});
