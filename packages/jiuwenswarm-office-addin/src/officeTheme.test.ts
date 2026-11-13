// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { followOfficeTheme, themeForBackground } from './officeTheme';

describe('the theme for an Office background', () => {
  it('is dark on the dark and black Office themes and light on the white and colorful ones', () => {
    expect(themeForBackground('#262626')).toBe('dark');
    expect(themeForBackground('#000000')).toBe('dark');
    expect(themeForBackground('#FFFFFF')).toBe('light');
    expect(themeForBackground('#E6E6E6')).toBe('light');
  });

  it('leaves the choice to the system without a color it can read', () => {
    expect(themeForBackground(undefined)).toBeUndefined();
    expect(themeForBackground('white')).toBeUndefined();
  });
});

describe('following the Office theme', () => {
  it('applies the theme when the host refuses the theme-change event, as PowerPoint on Mac does', () => {
    vi.stubGlobal('Office', { EventType: { OfficeThemeChanged: 'officeThemeChanged' } });
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: () => undefined }));
    const office = {
      officeTheme: { bodyBackgroundColor: '#262626' },
      document: { addHandlerAsync: () => { throw new Error('Function addHandlerAsync has invalid parameters.'); } },
    } as unknown as typeof Office.context;

    followOfficeTheme(office);
    expect(document.documentElement.dataset.theme).toBe('dark');
    vi.unstubAllGlobals();
  });
});
