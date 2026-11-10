import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore, useWorkspaceStore } from '../../../stores/index.ts';
import { WebUIHost } from '../react/WebUIHost.tsx';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../../../components/ui/DropdownMenu/DropdownMenu.tsx';
import { PresentationHostContext, type PresentationDeclaration } from './hostContext.ts';
import { presentationConversations } from './conversations.ts';
import { selectConversationModel } from '../../../multi-session/state/modelSelectionActions.ts';
import { createPresentationReplyReader } from './reply.ts';
import { webClient } from '../../../services/webClient';
import { useWebUIRuntime } from '../react/WebUIProvider.tsx';
import { createPresentationStartHandler, PRESENTATION_START_EVENT, PRESENTATION_RESULT_METHOD } from './start.ts';
import {
  createPresentationRequests, parsePresentationRequest, PRESENTATION_SLOT,
  type PresentationConversation, type PresentationRequest, type PresentationState,
} from './protocol.ts';

export interface ApplicationPresentationHostProps {
  sessionId: string;
  connected: boolean;
  onSelect: (conversation: PresentationConversation) => Promise<void>;
  onSend: (conversation: PresentationConversation, text: string) => Promise<{ accepted: boolean; conversationKey: string }>;
  onStop: (conversation: PresentationConversation) => Promise<unknown>;
  onWorkspace: (conversation: PresentationConversation | null) => Promise<void>;
}

const THEME_TOKENS = [
  '--color-surface-elevated', '--color-surface-muted', '--color-surface-hover', '--color-surface-page',
  '--color-text-primary', '--color-text-secondary', '--color-text-inverse', '--color-text-link', '--color-border-default',
];

const noSubscription = () => () => {};

function useActiveStore<State>(enabled: boolean, store: {
  getState: () => State; subscribe: (notify: () => void) => () => void;
}): State | null {
  return useSyncExternalStore(enabled ? store.subscribe : noSubscription,
    useCallback(() => enabled ? store.getState() : null, [enabled, store]));
}

/** The host owns semantic state and operations; plugin markup stays entirely in its sandbox. */
export function ApplicationPresentationHost(props: ApplicationPresentationHostProps) {
  const { t, i18n } = useTranslation();
  const [isActive, setIsActive] = useState(false);
  const sessionState = useActiveStore(isActive, useSessionStore);
  const chatState = useActiveStore(isActive, useChatStore);
  const workspaceState = useActiveStore(isActive, useWorkspaceStore);
  const replyReader = useRef(createPresentationReplyReader());
  const selected = useRef<string | null>(props.sessionId);
  const active = useRef<PresentationDeclaration | null>(null);
  const generation = useRef<string | null>(null);
  const activation = useRef<Promise<{ generation: string }> | null>(null);
  const [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState('');
  const [themeVersion, setThemeVersion] = useState(0);
  const [available, setAvailable] = useState(new Map<string, PresentationDeclaration>());
  const [menuOpen, setMenuOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const starting = useRef(false);
  const runtime = useWebUIRuntime();
  const api = window.jiuwenDesktop?.presentation;
  const callbacks = useRef(props);
  callbacks.current = props;
  const words = useRef(t);
  words.current = t;

  useEffect(() => {
    if (!active.current) { selected.current = props.sessionId; setRevision((value) => value + 1); }
  }, [props.sessionId, isActive]);
  useEffect(() => {
    if (!isActive) return;
    const update = () => setThemeVersion((value) => value + 1);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true,
      attributeFilter: ['data-theme', 'data-color-mode', 'style', 'class', 'data-webui-appearance', 'data-webui-background', 'data-webui-surface'] });
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    motion.addEventListener('change', update);
    return () => { observer.disconnect(); motion.removeEventListener('change', update); };
  }, [isActive]);
  const readSnapshot = (): PresentationState => {
    const { sessions, currentSession, runtimes: sessionRuntimes, chatAvailableModels: models } = useSessionStore.getState();
    const { projectSessions, pinnedSessions } = useWorkspaceStore.getState();
    const { runtimes } = useChatStore.getState();
    const visibleSessions = [...new Map([
      ...pinnedSessions, ...Object.values(projectSessions).flat(), ...sessions, ...(currentSession ? [currentSession] : []),
    ].map((session) => [session.session_id, session])).values()];
    const conversations = presentationConversations(visibleSessions, runtimes,
      t('webuiPresentation.newConversation'), replyReader.current.identify).map((conversation) => ({ ...conversation,
      mode: sessionRuntimes[conversation.sessionId]?.mode ?? conversation.mode,
      modelId: sessionRuntimes[conversation.sessionId]?.selectedModelSelection?.type === 'model'
        ? sessionRuntimes[conversation.sessionId].selectedModelSelection!.id
        : models.find((model) => model.model_name === useSessionStore.getState().getEffectiveModelName(conversation.sessionId))?.model_id ?? null,
    }));
    const keys = new Set(conversations.map((item) => item.conversationKey));
    if (selected.current && !keys.has(selected.current)) selected.current = null;
    const computed = getComputedStyle(document.documentElement);
    const theme = Object.fromEntries(THEME_TOKENS.map((key) => [key, computed.getPropertyValue(key).trim()]));
    return { locale: i18n.resolvedLanguage || i18n.language, connected: props.connected,
      selectedConversationKey: selected.current, conversations, theme,
      models: models.filter((model) => model.model_id).map((model) => ({ id: model.model_id!, label: model.alias || model.model_name })),
      reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches };
  };
  const readSnapshotRef = useRef(readSnapshot);
  readSnapshotRef.current = readSnapshot;
  const snapshot = useMemo(() => isActive ? readSnapshot() : null,
    [isActive, sessionState, chatState, workspaceState, revision, themeVersion, i18n.language, i18n.resolvedLanguage, props.connected, t]);
  const latest = useRef<PresentationState | null>(null);
  if (snapshot) latest.current = snapshot;
  const fail = useCallback((code: string): never => { throw new Error(code); }, []);

  const port = useMemo(() => ({
    async activate(declaration: PresentationDeclaration) {
      if (!api || !callbacks.current.connected) fail('DISCONNECTED');
      if (starting.current) fail('BUSY');
      if (active.current?.key === declaration.key && generation.current) return;
      starting.current = true;
      active.current = declaration;
      latest.current = readSnapshotRef.current();
      setIsActive(true);
      generation.current = null;
      setNotice('');
      const pending = api!.open({ ...declaration,
        resourceUrl: new URL(declaration.resourceUrl, window.location.href).href, state: latest.current! }).then(async (opened) => {
        if (activation.current !== pending) fail('STALE_REQUEST');
        const conversation = latest.current!.conversations.find((item) => item.conversationKey === selected.current);
        if (conversation) await callbacks.current.onSelect(conversation);
        if (activation.current !== pending) fail('STALE_REQUEST');
        return opened;
      });
      activation.current = pending;
      try {
        const opened = await pending;
        if (activation.current === pending) generation.current = opened.generation;
      } catch (error) {
        if (activation.current === pending) {
          active.current = null;
          setIsActive(false);
          generation.current = null;
          activation.current = null;
          void api!.close(declaration.key);
        }
        throw error;
      } finally {
        starting.current = false;
      }
    },
    update(declaration: PresentationDeclaration) {
      setAvailable((previous) => {
        if (previous.get(declaration.key) === declaration) return previous;
        const next = new Map(previous); next.set(declaration.key, declaration); return next;
      });
      if (active.current?.key !== declaration.key) return;
      active.current = declaration;
      void api?.update(declaration.key, { config: declaration.config }).catch(() => setNotice(words.current('webuiPresentation.updateFailed')));
    },
    release(key: string) {
      setAvailable((previous) => {
        if (!previous.has(key)) return previous;
        const next = new Map(previous); next.delete(key); return next;
      });
      if (active.current?.key !== key) return;
      active.current = null;
      setIsActive(false);
      generation.current = null;
      activation.current = null;
      void api?.close(key);
    },
  }), [api, fail]);

  useEffect(() => {
    const handle = createPresentationStartHandler({
      runtime, language: () => i18n.resolvedLanguage ?? i18n.language ?? 'zh', desktop: () => !!api,
      activate: async (declaration) => {
        setOpening(true);
        try { await port.activate(declaration); }
        finally { setOpening(false); }
      },
      result: (params) => webClient.request(PRESENTATION_RESULT_METHOD, params),
    });
    return webClient.on(PRESENTATION_START_EVENT, (event) => {
      void handle(event.payload).catch(() => setNotice(words.current('webuiPresentation.openFailed')));
    });
  }, [api, i18n, port, runtime]);

  useEffect(() => {
    if (!api) return;
    let ledgerGeneration: string | null = null;
    let ledger: ReturnType<typeof createPresentationRequests> | null = null;
    const sending = new Set<string>();
    const changing = new Set<string>();
    const execute = async (request: PresentationRequest, requestGeneration: string) => {
      const declaration = active.current;
      if (!declaration || !latest.current!.connected) fail('DISCONNECTED');
      if (generation.current !== requestGeneration) fail('STALE_REQUEST');
      const p = request.params;
      const key = p.conversationKey as string | undefined;
      const conversation = key ? latest.current!.conversations.find((item) => item.conversationKey === key) : null;
      if (key && !conversation) fail('TARGET_INACTIVE');
      switch (request.action) {
        case 'reply':
          if (selected.current !== key) fail('STALE_REQUEST');
          return replyReader.current.read(useChatStore.getState().runtimes[key!], p.replyId as number, p.offset as number, p.role as 'user' | 'assistant' | undefined);
        case 'model': {
          if (conversation!.mode !== 'agent' || conversation!.teamId) fail('TARGET_INACTIVE');
          if (!conversation!.canSend || useChatStore.getState().runtimes[key!]?.isProcessing || sending.has(key!) || changing.has(key!)) fail('CONVERSATION_BUSY');
          if (!latest.current!.models.some((model) => model.id === p.modelId)) fail('MODEL_UNAVAILABLE');
          changing.add(key!);
          try {
            await selectConversationModel(conversation!.sessionId, { type: 'model', id: p.modelId as string });
            return true;
          } finally { changing.delete(key!); }
        }
        case 'select':
          await callbacks.current.onSelect(conversation!);
          if (generation.current !== requestGeneration) fail('STALE_REQUEST');
          if (!latest.current!.conversations.some((item) => item.conversationKey === key)) fail('TARGET_INACTIVE');
          selected.current = key!;
          setRevision((value) => value + 1);
          return true;
        case 'draft':
          useChatStore.getState().ensureRuntime(key!);
          useChatStore.getState().setInputValue(key!, p.text as string);
          window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: key!, value: p.text } }));
          return true;
        case 'send': {
          if (!conversation!.canSend || sending.has(key!) || changing.has(key!)) fail('CONVERSATION_BUSY');
          sending.add(key!);
          try {
            const sent = await callbacks.current.onSend(conversation!, p.text as string);
            if (!sent.accepted) fail('SEND_FAILED');
            if (conversation!.sessionId === 'new' && generation.current === requestGeneration) selected.current = sent.conversationKey;
            // Preserve edits made while waiting for the existing send receipt.
            if (useChatStore.getState().runtimes[key!]?.inputValue === p.text) {
              useChatStore.getState().setInputValue(key!, '');
              window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: key!, value: '' } }));
            }
            setRevision((value) => value + 1);
            return true;
          } finally { sending.delete(key!); }
        }
        case 'stop':
          if (!await callbacks.current.onStop(conversation!)) fail('ACTION_FAILED');
          return true;
        case 'workspace':
          await callbacks.current.onWorkspace(conversation ?? null);
          if (generation.current !== requestGeneration) fail('STALE_REQUEST');
          if (p.transition && !latest.current!.reducedMotion) {
            await api.transitionWorkspace(declaration!.key, p.transition as { durationMs: number });
          } else await api.close(declaration!.key);
          return true;
        case 'resize': return api.resize(declaration!.key, p.width as number, p.height as number);
        case 'move': return api.move(declaration!.key, p.deltaX as number, p.deltaY as number);
        case 'hitTest': return api.hitTest(declaration!.key, p.interactive as boolean);
      }
    };
    const offRequest = api.onRequest(async (envelope) => {
      if (!active.current || active.current.key !== envelope.key) return;
      // Wait for native readiness; queued messages never choose their own activation generation.
      const pending = activation.current;
      if (!pending) return;
      try { await pending; } catch { return; }
      if (activation.current !== pending || !active.current || active.current.key !== envelope.key) return;
      if (generation.current !== envelope.generation) return;
      const request = parsePresentationRequest(envelope.request);
      if (!request) return;
      if (ledgerGeneration !== envelope.generation) {
        ledgerGeneration = envelope.generation;
        ledger = createPresentationRequests((request) => execute(request, envelope.generation), (error) => {
          const code = error instanceof Error ? error.message : 'ACTION_FAILED';
          const message = words.current(`webuiPresentation.errors.${code}`, { defaultValue: words.current('webuiPresentation.actionFailed') });
          return { code, message };
        });
      }
      void ledger!(request).then((result) => api.respond(envelope.generation, result))
        .catch(() => setNotice(words.current('webuiPresentation.actionFailed')));
    });
    const offClosed = api.onClosed(async (event) => {
      const pending = activation.current;
      if (generation.current === null && pending) {
        try { await pending; } catch { return; }
      }
      if (active.current?.key !== event.key || (generation.current && generation.current !== event.generation)) return;
      active.current = null;
      setIsActive(false);
      generation.current = null;
      activation.current = null;
      if (!['workspace', 'replaced', 'shutdown'].includes(event.reason)) setNotice(words.current('webuiPresentation.restored'));
    });
    return () => { offRequest(); offClosed(); };
  }, [api, fail]);

  useEffect(() => {
    const declaration = active.current;
    if (!api || !declaration || !snapshot) return;
    if (!snapshot.connected) {
      void api.close(declaration.key);
      return;
    }
    void api.update(declaration.key, { state: snapshot }).catch(() => setNotice(t('webuiPresentation.updateFailed')));
  }, [api, snapshot, t]);

  return (
    <PresentationHostContext.Provider value={port}>
      <WebUIHost slotId={PRESENTATION_SLOT} />
      {available.size ? <div className="webui-presentation-launcher" data-testid="webui-presentation-launcher">
        <DropdownMenu open={menuOpen} onOpenChange={(open) => { setMenuOpen(open); if (open) setNotice(''); }}>
          <DropdownMenuTrigger asChild><button type="button" className="webui-presentation-launcher__button"
            data-testid="webui-presentation-open" disabled={!api || !props.connected || opening}>
            {opening ? t('webuiPresentation.opening') : t('webuiPresentation.choose')}
          </button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {[...available.values()].map((declaration) => <DropdownMenuItem key={declaration.key}
              onSelect={() => {
                setOpening(true); setNotice('');
                void port.activate(declaration).catch(() => setNotice(t('webuiPresentation.openFailed')))
                  .finally(() => setOpening(false));
              }}>{declaration.title}</DropdownMenuItem>)}
          </DropdownMenuContent>
        </DropdownMenu>
        {!api ? <span>{t('webuiPresentation.desktopRequired')}</span> : null}
      </div> : null}
      {notice ? <div role="status" className="webui-presentation-notice" data-testid="webui-presentation-notice">{notice}</div> : null}
    </PresentationHostContext.Provider>
  );
}
