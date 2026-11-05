import React, { useState } from '../../jiuwenswarm/channels/web/frontend/node_modules/react';
import { createRoot } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-dom/client';
import i18n from '../../jiuwenswarm/channels/web/frontend/node_modules/i18next';
import { initReactI18next } from '../../jiuwenswarm/channels/web/frontend/node_modules/react-i18next';
import { DesktopBrowserPane } from '../../jiuwenswarm/channels/web/frontend/src/components/DesktopBrowserPane';
import {
  applyBrowserDisplaySettings,
  useDesktopBrowserHeadless,
} from '../../jiuwenswarm/channels/web/frontend/src/features/desktopBrowserDisplay';
import {
  closeDesktopBrowserTab,
  useDesktopBrowserAutoOpen,
  useDesktopBrowserTabFlags,
} from '../../jiuwenswarm/channels/web/frontend/src/features/desktopBrowserFile';
import { useChatStore } from '../../jiuwenswarm/channels/web/frontend/src/stores/chatStore';
import en from '../../jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json';

function BrowserFixture() {
  const [expanded, setExpanded] = useState(false);
  const flags = useDesktopBrowserTabFlags('session-one');
  const headless = useDesktopBrowserHeadless();
  useDesktopBrowserAutoOpen('session-one', () => setExpanded(true));
  const visible = !headless && expanded && flags.requested && !flags.closed;
  return (
    <div data-testid="browser-test-ready" style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0 }}>
      <button data-testid="browser-test-close" onClick={() => {
        closeDesktopBrowserTab();
        setExpanded(false);
      }}>Close browser</button>
      {headless && <div data-testid="browser-test-headless">Background browser task</div>}
      {visible && <DesktopBrowserPane sessionId="session-one" />}
    </div>
  );
}

(window as Window & { browserTestDisplay?: typeof applyBrowserDisplaySettings }).browserTestDisplay = applyBrowserDisplaySettings;

void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } }).then(async () => {
  useChatStore.setState({ activeSessionId: 'session-one' });
  await applyBrowserDisplaySettings({ headless: false });
  createRoot(document.getElementById('root')!).render(<BrowserFixture />);
});
