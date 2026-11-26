import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import i18n from '../../src/i18n/index.ts';
import { ApplicationPresentationHost } from '../../src/features/webui/presentation/ApplicationPresentationHost.tsx';
import { WebUIProvider } from '../../src/features/webui/react/WebUIProvider.tsx';
import { createComponentRendererRegistry } from '../../src/features/webui/renderer/adapters/componentRegistry.ts';
import { componentCatalog } from '../../src/features/webui/catalog/componentCatalog.ts';
import { createWebUIRuntime } from '../../src/features/webui/runtime/index.ts';
import { createMockTransport } from '../../src/features/webui/transport/index.ts';
import { useSessionStore, useChatStore, useWorkspaceStore } from '../../src/stores/index.ts';
import '../../src/features/webui/renderer/adapters/webuiApplicationSurface/WebUIApplicationSurface.css';

const registry = createComponentRendererRegistry();
const transport = createMockTransport();
const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
runtime.start();
useSessionStore.setState({ sessions: [{ session_id: 'a', title: 'Alpha', mode: 'agent' },
  { session_id: 'b', title: 'Beta', mode: 'agent' }], currentSession: null });
useWorkspaceStore.setState({ projectSessions: {}, pinnedSessions: [] });
useChatStore.getState().ensureRuntime('a');
const fixture = (window as Window & { presentationFixture?: { target: Record<string, string>;
  root: { component: string; props: Record<string, unknown> } } }).presentationFixture;
const target = fixture?.target || { scope: 'application', slot_id: 'application.presentation', package_id: 'overlay.demo', instance_key: 'pet' };
const log: unknown[] = [];
let revision = 0;
function publish(mode = 'overlay') {
  transport.injectRenderFrame({ target, revision: ++revision, mode: 'replace', schema_version: 1,
    root: { component: 'ApplicationSurface', props: { title: { zh: '悬浮示例', en: 'Floating example' },
      src: '/extension-assets/overlay.demo/g1/pet.html', width: 160, height: 120, ...fixture?.root.props, mode } } });
}
function observe(activity: string, content?: string, key = 'a') {
  useChatStore.getState().ensureRuntime(key);
  const store = useChatStore.getState();
  const current = store.runtimes[key];
  useChatStore.setState({ runtimes: { ...store.runtimes, [key]: { ...current,
    isThinking: activity === 'thinking', isProcessing: activity === 'thinking' || activity === 'replying' || activity === 'toolRunning',
    error: activity === 'failed' ? 'Fixture error' : null, executionError: null, currentStreamContent: '',
    pendingQuestions: activity === 'needsInput' ? [{}] : [], lastFinalReply: null,
    toolExecutions: activity === 'toolRunning' ? new Map([['t1', { status: 'pending', toolCall: { name: 'Read' } }]]) : new Map(),
    messages: content === undefined ? [] : [{ id: 'u1', role: 'user', content: 'Check the companion', timestamp: 1 },
      { id: 'r1', role: 'assistant', content, timestamp: 2 }],
  } } });
}
let disconnect = () => {};
let sendGate: Promise<void> | null = null;
let releaseSend = () => {};
let selectGate: Promise<void> | null = null;
let releaseSelect = () => {};
Object.assign(window, { overlayDemo: {
  log, publish, observe,
  observeConversation: (key: string, activity: string, content?: string) => observe(activity, content, key),
  holdNextSelect: () => { selectGate = new Promise<void>((resolve) => { releaseSelect = resolve; }); },
  releaseSelect: () => releaseSelect(),
  holdNextSend: () => { sendGate = new Promise<void>((resolve) => { releaseSend = resolve; }); },
  releaseSend: () => releaseSend(),
  drafts: () => Object.fromEntries(Object.entries(useChatStore.getState().runtimes)
    .map(([key, chat]) => [key, chat.inputValue])),
  clear: () => transport.injectClearFrame({ target, revision: ++revision }),
  disconnect: () => disconnect(),
  language: (language: string) => i18n.changeLanguage(language),
} });

function Demo() {
  useEffect(() => { publish(); }, []);
  const [page, setPage] = useState('chat');
  const [sessionId, setSessionId] = useState('a');
  const [connected, setConnected] = useState(true);
  const [clicks, setClicks] = useState(0);
  disconnect = () => setConnected(false);
  return <WebUIProvider runtime={runtime} registry={registry}>
    <div style={{ position: 'absolute', left: 240, top: 100, width: 420, height: 300,
      transform: 'translateZ(0)', overflow: 'hidden', border: '1px solid currentColor' }} data-testid="demo-clipped-parent">
      <ApplicationPresentationHost sessionId={sessionId} connected={connected}
        onSelect={async (conversation) => { log.push(['select', conversation.conversationKey]);
          const gate = selectGate; selectGate = null;
          if (gate) await gate; }}
        onSend={async (conversation, text) => { log.push(['send', conversation.conversationKey, text]);
          const gate = sendGate; sendGate = null;
          if (gate) await gate;
          return { accepted: true, conversationKey: conversation.conversationKey }; }}
        onStop={async (conversation) => { log.push(['stop', conversation.conversationKey]); return true; }}
        onWorkspace={async (conversation) => { log.push(['workspace', conversation?.conversationKey]); }} />
      <main data-testid="demo-workspace">
        <button data-testid="demo-settings" onClick={() => setPage('settings')}>Settings</button>
        <button data-testid="demo-session" onClick={() => setSessionId('b')}>Beta</button>
        <button data-testid="demo-click" onClick={() => setClicks(value => value + 1)}>Click {clicks}</button>
        <p data-testid="demo-page">{page}</p><p data-testid="demo-active-session">{sessionId}</p>
      </main>
    </div>
  </WebUIProvider>;
}
createRoot(document.getElementById('root')!).render(<Demo />);
