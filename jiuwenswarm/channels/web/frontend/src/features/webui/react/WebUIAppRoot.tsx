/**
 * 应用级宿主组装：创建唯一 WebUI Runtime，并通过 Provider 下发给槽位宿主。
 * 生命周期（start/dispose）集中在这里；业务页面只负责在槽位处渲染 WebUIHost。
 */

import { useEffect, useMemo, type ReactNode } from 'react';
import { webClient } from '../../../services/webClient';
import { createWebUIAppRuntime } from '../assembly/createWebUIAppRuntime.ts';
import { componentCatalog } from '../catalog/componentCatalog.ts';
import { createComponentRendererRegistry } from '../renderer/adapters/componentRegistry.ts';
import { WebUIEntryHost } from './WebUIEntryHost.tsx';
import { WebUIProvider } from './WebUIProvider.tsx';

export interface WebUIAppRootProps {
  children: ReactNode;
}

export function WebUIAppRoot({ children }: WebUIAppRootProps) {
  const app = useMemo(
    () =>
      createWebUIAppRuntime({
        client: webClient,
        catalog: componentCatalog,
        registry: createComponentRendererRegistry(),
      }),
    [],
  );

  useEffect(() => {
    app.start();
    return () => app.dispose();
  }, [app]);

  return (
    <WebUIProvider runtime={app.runtime} registry={app.registry}>
      <WebUIEntryHost>{children}</WebUIEntryHost>
    </WebUIProvider>
  );
}
