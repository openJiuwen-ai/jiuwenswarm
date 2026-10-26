import './i18n';
import ReactDOM from 'react-dom/client';
import { A2UIProvider } from '@a2ui/react';
import type { A2UIClientEventMessage } from '@a2ui/react';
import { injectStyles } from '@a2ui/react/styles';
import App from './App.tsx'
import { EnterpriseEntry } from './EnterpriseEntry.tsx'
import { dispatchA2UIAction } from './features/a2ui/actionBridge';
import { installDesktopLocalFilesBridge } from './features/workspace/localFilePicker';
import './styles/foundation.css'
import './styles/themes/default/light.css'
import './index.css'
import './features/a2ui/a2ui.css'
import { getApiPrefix, resolveApiUrl } from './utils/env';
/**
* 安装全局 fetch 拦截器：配置了 VITE_API_PREFIX 时，自动为指向同源的相对路径请求
* 拼接接口前缀，覆盖 string / URL / Request 三种传参形式，跨源请求不受影响。
*/
function installApiPrefixFetchInterceptor(): void {
  const prefix = getApiPrefix();
  if (!prefix || typeof window === 'undefined') return;

  const originalFetch = window.fetch;
  window.fetch = function (input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    if (typeof input === 'string') {
      return originalFetch(resolveApiUrl(input), init);
    }
    if (input instanceof URL) {
      const pathname = input.pathname;
      const resolvedPath = resolveApiUrl(pathname);
      if (resolvedPath !== pathname) {
        input.pathname = resolvedPath;
      }
      return originalFetch(input, init);
    }
    if (input instanceof Request) {
      // 仅拦截同源请求，避免修改发往其他站点的 Request 对象。
      const url = new URL(input.url);
      if (url.origin === window.location.origin) {
        const resolvedPath = resolveApiUrl(url.pathname);
        if (resolvedPath !== url.pathname) {
          url.pathname = resolvedPath;
          // Request 的 url 只读，需克隆后替换地址再转发。
          const cloned = new Request(url.toString(), input);
          return originalFetch(cloned, init);
        }
      }
      return originalFetch(input, init);
    }
    return originalFetch(input, init);
  };
}

installApiPrefixFetchInterceptor();

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
  <EnterpriseEntry>
    <A2UIProvider onAction={handleA2UIAction}>
      <App />
    </A2UIProvider>
  </EnterpriseEntry>,
)
