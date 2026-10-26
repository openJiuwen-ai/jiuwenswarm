import React from '../../jiuwenswarm/channels/web/frontend/node_modules/react';
import { createRoot } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-dom/client';
import i18n from '../../jiuwenswarm/channels/web/frontend/node_modules/i18next';
import { initReactI18next } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-i18next';
import { DesktopBrowserPane } from '../../jiuwenswarm/channels/web/frontend/src/components/DesktopBrowserPane';
import {
  applyBrowserDisplaySettings,
  useDesktopBrowserHeadless,
} from '../../jiuwenswarm/channels/web/frontend/src/features/desktopBrowserDisplay';
import en from '../../jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json';

function BrowserFixture() {
  return useDesktopBrowserHeadless()
    ? <div data-testid="browser-test-headless">Background browser task</div>
    : <DesktopBrowserPane sessionId="session-one" />;
}

(window as Window & { browserTestDisplay?: typeof applyBrowserDisplaySettings }).browserTestDisplay = applyBrowserDisplaySettings;

void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } }).then(async () => {
  await applyBrowserDisplaySettings({ headless: false });
  createRoot(document.getElementById('root')!).render(<BrowserFixture />);
});
