import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../src/index.css';
import '../../src/styles/foundation.css';
import '../../src/styles/themes/default/light.css';
import i18n from '../../src/i18n';
import { createRendererRegistry, type WebUIAdapterProps } from '../../src/features/webui/renderer/types';
import { webClient } from '../../src/services/webClient';
import { gatewayFetch } from '../../src/services/gatewayFetch';
import { SessionSidebar } from '../../src/components/SessionSidebar';
import { componentCatalog } from '../../src/features/webui/catalog/componentCatalog';
import { createComponentRendererRegistry } from '../../src/features/webui/renderer/adapters/componentRegistry';
import { createWebUIAppRuntime } from '../../src/features/webui/assembly/createWebUIAppRuntime';
import { WebUIEntryHost } from '../../src/features/webui/react/WebUIEntryHost';
import { WebUIProvider } from '../../src/features/webui/react/WebUIProvider';
import { browserClientCapabilities } from '../../src/features/webui/client/browserClient';
import { useChatStore } from '../../src/stores/chatStore';
import { useSessionStore } from '../../src/stores/sessionStore';
import { WebUIAppRoot } from '../../src/features/webui/react/WebUIAppRoot';
import { useOptionalWebUIAppContext } from '../../src/features/webui/react/WebUIProvider';
import promo from '../../src/assets/bee-static.png?url';
import background from '../../src/assets/rsi/rsi-background2.svg?url';

// This is a protocol fixture, with no identity service or login Handler.
document.documentElement.dataset.theme = 'default';
document.documentElement.dataset.colorMode = 'light';
const baseRegistry = createComponentRendererRegistry();
const Avatar = baseRegistry.get('Avatar')!;
const registry = createRendererRegistry({
  ...Object.fromEntries(baseRegistry.list().map((name) => [name, baseRegistry.get(name)!])),
  Avatar: (props: WebUIAdapterProps) => {
    if (props.node.props.alt === 'fixture-render-error') throw new Error('Fixture render failure');
    return <Avatar {...props} />;
  },
});
const lifecycle = new URLSearchParams(location.search).has('lifecycle');
const app = lifecycle ? null : createWebUIAppRuntime({ client: webClient, clientCapabilities: browserClientCapabilities,
  catalog: componentCatalog, registry });
app?.start();
window.addEventListener('storage', (event) => browserClientCapabilities.storageChanged(event));
let underlyingClicks = 0;
const state = { host: browserClientCapabilities, runtime: app?.runtime, client: webClient, mounts: 0, cleanups: 0,
  fetch: gatewayFetch, chat: useChatStore, session: useSessionStore,
  i18n, assets: { promo, background }, underlyingClicks: () => underlyingClicks };
Object.assign(window, { webuiFixture: state });
function LifecycleProbe() {
  const context = useOptionalWebUIAppContext();
  state.runtime = context?.runtime;
  const [value, setValue] = useState('fresh');
  useEffect(() => {
    state.mounts++;
    const release = webClient.retainConnection();
    return () => {
      state.cleanups++;
      release();
    };
  }, []);
  return <input data-testid="webui-fixture-lifecycle-input" value={value} onChange={(event) => setValue(event.target.value)} />;
}
createRoot(document.getElementById('root')!).render(lifecycle
  ? <WebUIAppRoot><LifecycleProbe /></WebUIAppRoot>
  : <WebUIProvider runtime={app!.runtime} registry={registry}>
    <WebUIEntryHost>
      <SessionSidebar activeNav="chat" onNavigate={() => undefined} onNewSession={() => undefined} showNewSession={false} />
      <main><button data-testid="webui-fixture-underlying" onClick={() => { underlyingClicks++; }}>Workspace action</button></main>
    </WebUIEntryHost>
  </WebUIProvider>,
);
void webClient.connect();
