// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';

it('says why when its presentation is not open, with a retry, rather than a blank pane', async () => {
  document.body.innerHTML = '<div id="app"></div>';
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: () => undefined }));
  vi.stubGlobal('Application', { Presentations: { Count: 0 } });
  history.replaceState(null, '', '/taskpane.wps-presentation.html?doc=closed-deck');
  await import('./taskpane.wps-presentation');
  await vi.waitFor(() => expect(document.querySelector('.tray-item')?.textContent).toContain('演示文稿已经关闭'));
  expect(document.querySelector('.tray-action')?.textContent).toBe('重试');
});
