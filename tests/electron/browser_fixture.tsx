import React from '../../jiuwenswarm/channels/web/frontend/node_modules/react';
import { createRoot } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-dom/client';
import i18n from '../../jiuwenswarm/channels/web/frontend/node_modules/i18next';
import { initReactI18next } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-i18next';
import { DesktopBrowserPane } from '../../jiuwenswarm/channels/web/frontend/src/components/DesktopBrowserPane';
import en from '../../jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json';

void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } }).then(() => {
  createRoot(document.getElementById('root')!).render(<DesktopBrowserPane sessionId="session-one" />);
});
