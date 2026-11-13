/**
 * The pane follows the host's theme: dark on a dark Office theme, light on a
 * light one. Without an Office theme (the web host may have none) it follows
 * the system's color scheme instead.
 */
export type Theme = 'light' | 'dark';

/** The theme for a background color such as ``#262626``: dark when its relative luminance is under one half. */
export function themeForBackground(color: string | undefined): Theme | undefined {
  const hex = /^#?([0-9a-f]{6})$/i.exec(color?.trim() ?? '')?.[1];
  if (!hex) return undefined;
  const [r, g, b] = [0, 2, 4].map(at => Number.parseInt(hex.slice(at, at + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5 ? 'dark' : 'light';
}

/** Set the page's theme (``data-theme``, which the stylesheet reads): ``theme``, or else the system's. */
function applyTheme(theme: Theme | undefined, root: HTMLElement = document.documentElement): void {
  root.dataset.theme = theme ?? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

/** Apply the Office theme to the page now and whenever the user or the system changes it. */
export function followOfficeTheme(office: typeof Office.context): void {
  const apply = (): void => applyTheme(themeForBackground(office.officeTheme?.bodyBackgroundColor));
  apply();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', apply);
  // PowerPoint on Mac refuses the event and throws; the theme then stays as the pane opened with it.
  try {
    office.document?.addHandlerAsync?.(Office.EventType.OfficeThemeChanged, apply, () => undefined);
  } catch {
    // No theme-change event on this host.
  }
}
