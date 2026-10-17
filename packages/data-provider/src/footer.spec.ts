import { hasConfiguredFooter } from './footer';

describe('hasConfiguredFooter', () => {
  it('is true for a footer the deployment configured itself', () => {
    expect(hasConfiguredFooter({ customFooter: 'Operator policy' })).toBe(true);
    expect(hasConfiguredFooter({ customFooter: '[Docs](https://example.com)' })).toBe(true);
  });

  it('uses the interface footer when no top-level footer is configured', () => {
    expect(hasConfiguredFooter({ interface: { customFooter: 'Operator policy' } })).toBe(true);
    expect(hasConfiguredFooter({ interface: { customFooter: '   ' } })).toBe(false);
  });

  it('preserves an explicitly blank top-level footer over the interface footer', () => {
    expect(
      hasConfiguredFooter({ customFooter: '', interface: { customFooter: 'Operator policy' } }),
    ).toBe(false);
  });

  it('is false for a deployment that configured none of it', () => {
    expect(hasConfiguredFooter()).toBe(false);
    expect(hasConfiguredFooter(null)).toBe(false);
    expect(hasConfiguredFooter({})).toBe(false);
    /** An operator who set the footer to nothing suppressed the welcome
     *  screen's disclaimer; a conversation renders nothing for it, so it must
     *  not reserve the band a bar would need. */
    expect(hasConfiguredFooter({ customFooter: '' })).toBe(false);
    /** The bar trims each part, so whitespace is the same nothing. */
    expect(hasConfiguredFooter({ customFooter: '   ' })).toBe(false);
  });
});
