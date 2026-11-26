/**
 * 应用级宿主组装：创建唯一 WebUI Runtime，并通过 Provider 下发给槽位宿主。
 * 生命周期（start/dispose）集中在这里；业务页面只负责在槽位处渲染 WebUIHost。
 */

import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient } from '../../../services/webClient';
import { WebHttpClient } from '../../../services/webHttpClient';
import { createWebUIAppRuntime } from '../assembly/createWebUIAppRuntime.ts';
import { componentCatalog } from '../catalog/componentCatalog.ts';
import { createComponentRendererRegistry } from '../renderer/adapters/componentRegistry.ts';
import { WebUIAppearanceHost, resetTabBrand } from './WebUIAppearanceHost.tsx';
import { WebUIBackgroundHost } from './WebUIBackgroundHost.tsx';
import { WebUIEntryHost } from './WebUIEntryHost.tsx';
import { STATIC_NEUTRAL_ID, WebUISplashGate } from './WebUISplashGate.tsx';
import { fetchStartupHint, resolveLaunch } from '../splash/launch.ts';
import { SPLASH_PLAY_EVENT } from '../splash/controller.ts';
import { WebUIProvider } from './WebUIProvider.tsx';
import { browserClientCapabilities } from '../client/browserClient.ts';
import { WebUIUploadContext } from '../renderer/adapters/webuiFileImport/uploadContext.ts';
import { createUploadService, putWithFetch } from '../transfers/uploads.ts';
import { DOWNLOAD_EVENT, readDownloadOffer, startDownload } from '../transfers/downloads.ts';

export interface WebUIAppRootProps {
  children: ReactNode;
}

export function WebUIAppRoot({ children }: WebUIAppRootProps) {
  useLayoutEffect(() => {
    if (!(webClient instanceof WebHttpClient)) return;
    // Without a Gateway connection there is no gate and no appearance: show the host brand.
    document.getElementById(STATIC_NEUTRAL_ID)?.remove();
    resetTabBrand(document);
  }, []);
  if (webClient instanceof WebHttpClient) return <>{children}</>;
  return <WebSocketWebUIAppRoot client={webClient}>{children}</WebSocketWebUIAppRoot>;
}

function WebSocketWebUIAppRoot({ children, client }: WebUIAppRootProps & {
  client: Exclude<typeof webClient, WebHttpClient>;
}) {
  const { t } = useTranslation();
  const error = useSyncExternalStore(browserClientCapabilities.subscribeErrors, browserClientCapabilities.getError);
  const app = useMemo(
    () =>
      createWebUIAppRuntime({
        client,
        clientCapabilities: browserClientCapabilities,
        catalog: componentCatalog,
        registry: createComponentRendererRegistry(),
      }),
    [client],
  );

  useEffect(() => {
    app.start();
    const releaseConnection = client.retainConnection();
    const onStorage = (event: StorageEvent) => browserClientCapabilities.storageChanged(event);
    const onVisible = () => { if (document.visibilityState === 'visible') browserClientCapabilities.refresh(); };
    const onFocus = () => browserClientCapabilities.refresh();
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisible);
      app.dispose();
      releaseConnection();
    };
  }, [app, client]);

  // Imports and exports go through the host (Gateway S_10); adapters never reach the client directly.
  const uploads = useMemo(() => createUploadService({
    runtime: app.runtime,
    request: (method, params) => client.request(method, params),
    put: putWithFetch,
  }), [app, client]);
  useEffect(() => client.on(DOWNLOAD_EVENT, (event) => {
    const offer = readDownloadOffer(event.payload);
    if (!offer) return;
    const desktop = window.jiuwenDesktop?.downloadFile;
    void startDownload(offer, { desktopDownload: desktop, document }).catch(() => undefined);
  }), [client]);

  const launch = useMemo(() => resolveLaunch(window.jiuwenDesktop?.launchId), []);
  const requestProviders = useMemo(() => () => client.request('webui.splash.providers', {}), [client]);
  const subscribePreview = useMemo(() => (listener: (payload: unknown) => void) =>
    client.on(SPLASH_PLAY_EVENT, (event) => listener(event.payload)), [client]);

  return (
    <WebUIProvider runtime={app.runtime} registry={app.registry}>
      <WebUIUploadContext.Provider value={uploads}>
      <WebUISplashGate launch={launch} hint={fetchStartupHint} requestProviders={requestProviders}
        subscribePreview={subscribePreview}>
        <WebUIBackgroundHost />
        <WebUIAppearanceHost />
        <WebUIEntryHost notice={error ? <div role="alert" className="webui-entry-notice" data-testid="webui-client-error">
          {t(error === 'CONFIGURATION_ERROR' ? 'webui.clientConfigurationError' : 'webui.clientOperationError')}
        </div> : undefined}>
          {children}
        </WebUIEntryHost>
      </WebUISplashGate>
      </WebUIUploadContext.Provider>
    </WebUIProvider>
  );
}
