/**
 * 应用级宿主组装：创建唯一 WebUI Runtime，并通过 Provider 下发给槽位宿主。
 * 生命周期（start/dispose）集中在这里；业务页面只负责在槽位处渲染 WebUIHost。
 */

import { useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient } from '../../../services/webClient';
import { createWebUIAppRuntime } from '../assembly/createWebUIAppRuntime.ts';
import { componentCatalog } from '../catalog/componentCatalog.ts';
import { createComponentRendererRegistry } from '../renderer/adapters/componentRegistry.ts';
import { WebUIEntryHost } from './WebUIEntryHost.tsx';
import { WebUIProvider } from './WebUIProvider.tsx';
import { browserClientCapabilities } from '../client/browserClient.ts';

export interface WebUIAppRootProps {
  children: ReactNode;
}

export function WebUIAppRoot({ children }: WebUIAppRootProps) {
  const { t } = useTranslation();
  const error = useSyncExternalStore(browserClientCapabilities.subscribeErrors, browserClientCapabilities.getError);
  const app = useMemo(
    () =>
      createWebUIAppRuntime({
        client: webClient,
        clientCapabilities: browserClientCapabilities,
        catalog: componentCatalog,
        registry: createComponentRendererRegistry(),
      }),
    [],
  );

  useEffect(() => {
    app.start();
    const releaseConnection = webClient.retainConnection();
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
  }, [app]);

  return (
    <WebUIProvider runtime={app.runtime} registry={app.registry}>
      <WebUIEntryHost notice={error ? <div role="alert" className="webui-entry-notice" data-testid="webui-client-error">
        {t(error === 'CONFIGURATION_ERROR' ? 'webui.clientConfigurationError' : 'webui.clientOperationError')}
      </div> : undefined}>
        {children}
      </WebUIEntryHost>
    </WebUIProvider>
  );
}
