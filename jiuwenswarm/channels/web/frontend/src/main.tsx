import './i18n';
import ReactDOM from 'react-dom/client';
import { A2UIProvider } from '@a2ui/react';
import type { A2UIClientEventMessage } from '@a2ui/react';
import { injectStyles } from '@a2ui/react/styles';
import App from './App.tsx'
import { dispatchA2UIAction } from './features/a2ui/actionBridge';
import { installDesktopLocalFilesBridge } from './features/workspace/localFilePicker';
import './styles/foundation.css'
import './styles/themes/default/light.css'
import './index.css'
import './features/a2ui/a2ui.css'

// Durable desktop drop bridge — must exist before ChatPanel mounts / effect cleanup.
installDesktopLocalFilesBridge();

function flagA2UIIconFontAvailability() {
  if (typeof document === 'undefined' || !('fonts' in document)) {
    return
  }

  const fonts = document.fonts
  const hasMaterialSymbols =
    fonts.check('20px "Material Symbols Outlined"') ||
    fonts.check('20px "Google Symbols"')

  document.documentElement.classList.toggle(
    'a2ui-material-symbols-unavailable',
    !hasMaterialSymbols
  )
}

injectStyles();
flagA2UIIconFontAvailability()
void document.fonts?.ready.then(flagA2UIIconFontAvailability)

function handleA2UIAction(message: A2UIClientEventMessage) {
  void dispatchA2UIAction(message);
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <A2UIProvider onAction={handleA2UIAction}>
    <App />
  </A2UIProvider>,
)
