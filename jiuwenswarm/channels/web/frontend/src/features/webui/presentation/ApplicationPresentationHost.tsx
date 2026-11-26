// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore, useWorkspaceStore } from '../../../stores/index.ts';
import { WebUIHost } from '../react/WebUIHost.tsx';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../../../components/ui/DropdownMenu/DropdownMenu.tsx';
import { PresentationHostContext, type PresentationDeclaration } from './hostContext.ts';
import { presentationConversations } from './conversations.ts';
import { selectConversationModel } from '../../../multi-session/state/modelSelectionActions.ts';
import { createPresentationReplyReader } from './reply.ts';
import { createPresentationOverlay } from './overlay.ts';
import { PresentationOverlay } from './PresentationOverlay.tsx';
import { webClient } from '../../../services/webClient';
import { useOptionalWebUIAppContext, useWebUIRuntime } from '../react/WebUIProvider.tsx';
import { createPresentationStartHandler, PRESENTATION_START_EVENT, PRESENTATION_RESULT_METHOD } from './start.ts';
import {
  createPresentationRequests, parsePresentationRequest, PRESENTATION_SLOT,
  type PresentationConversation, type PresentationRequest, type PresentationState, type PresentationNativeApi, type PresentationChild,
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
// Survives host remounts on the same browser connection; a page reload creates a new connection.
let nextPresentationId = 0;

function allocatePresentationId(): number {
  if (nextPresentationId >= Number.MAX_SAFE_INTEGER) throw new Error('OPEN_FAILED');
  return ++nextPresentationId;
}

function samePresentationSource(
  previous: PresentationDeclaration | undefined,
  next: PresentationDeclaration,
): boolean {
  return previous?.key === next.key && (previous.mode ?? 'desktop') === (next.mode ?? 'desktop')
    && previous.resourceUrl === next.resourceUrl;
}

function useActiveStore<State>(enabled: boolean, store: {
  getState: () => State; subscribe: (notify: () => void) => () => void;
}): State | null {
  return useSyncExternalStore(enabled ? store.subscribe : noSubscription,
    useCallback(() => enabled ? store.getState() : null, [enabled, store]));
}

/** The host owns semantic state and operations; plugin markup stays entirely in its sandbox. */
export function ApplicationPresentationHost(props: ApplicationPresentationHostProps) {
  const context = useOptionalWebUIAppContext();
  return context ? <ConnectedApplicationPresentationHost {...props} /> : null;
}

function ConnectedApplicationPresentationHost(props: ApplicationPresentationHostProps) {
  const { t, i18n } = useTranslation();
  const [isActive, setIsActive] = useState(false);
  const sessionState = useActiveStore(isActive, useSessionStore);
  const chatState = useActiveStore(isActive, useChatStore);
  const workspaceState = useActiveStore(isActive, useWorkspaceStore);
  const replyReader = useRef(createPresentationReplyReader());
  const selected = useRef<string | null>(props.sessionId);
  const active = useRef<PresentationDeclaration | null>(null);
  const generation = useRef<string | null>(null);
  const presentationId = useRef<number | null>(null);
  const activation = useRef<Promise<{ generation: string }> | null>(null);
  const closing = useRef<{
    api: PresentationNativeApi; key: string; pending: Promise<unknown> | null;
  } | null>(null);
  const childWindows = useRef(new Map<string, { generation: string; parentGeneration: string }>());
  const childSync = useRef<Promise<unknown> | null>(null);
  const requestLedgers = useRef(new Map<string, ReturnType<typeof createPresentationRequests>>());
  const pendingStart = useRef<PresentationDeclaration | null>(null);
  const [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState('');
  const [themeVersion, setThemeVersion] = useState(0);
  const [available, setAvailable] = useState(new Map<string, PresentationDeclaration>());
  // Rerenders must not restart a form that was already attempted, closed, or failed.
  const autoStartAttempts = useRef(new Map<string, PresentationDeclaration>());
  const manualCandidates = useMemo(() => [...available.values()]
    .filter((item) => !item.autoStart), [available]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const starting = useRef(false);
  const runtime = useWebUIRuntime();
  const desktopApi = window.jiuwenDesktop?.presentation;
  const overlay = useMemo(() => createPresentationOverlay(() => ({
    x: 0, y: 0, width: window.innerWidth, height: window.innerHeight,
  })), []);
  const activeApi = useRef<PresentationNativeApi | null>(null);
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

  const clearActive = useCallback(() => {
    active.current = null;
    activeApi.current = null;
    generation.current = null;
    presentationId.current = null;
    activation.current = null;
    pendingStart.current = null;
    childWindows.current.clear();
    childSync.current = null;
    requestLedgers.current.clear();
    setIsActive(false);
  }, []);
  const closePendingSurface = useCallback((): Promise<unknown> => {
    const surface = closing.current;
    if (!surface) return Promise.resolve();
    if (surface.pending) return surface.pending;
    const pending = Promise.resolve().then(() => surface.api.close(surface.key));
    surface.pending = pending;
    void pending.then(() => {
      if (closing.current === surface) closing.current = null;
    }, () => {
      // Keep ownership until a later close confirms the container is gone.
      surface.pending = null;
    });
    return pending;
  }, []);
  const closeSurface = useCallback(async (api: PresentationNativeApi, key: string) => {
    while (closing.current && (closing.current.api !== api || closing.current.key !== key)) {
      await closePendingSurface();
    }
    if (!closing.current) closing.current = { api, key, pending: null };
    return closePendingSurface();
  }, [closePendingSurface]);
  const reportClosed = useCallback((declaration: PresentationDeclaration, id: number | null, reason: string) => {
    if (declaration.revision === undefined || id === null) return;
    const reportedReason = reason === 'workspace' ? 'closed' : reason;
    void runtime.notifyClosed(declaration.key, reportedReason, declaration.revision, id).then((receipt) => {
      if (!receipt?.accepted) setNotice(words.current('webuiPresentation.updateFailed'));
    });
  }, [runtime]);
  const port = useMemo(() => ({
    async activate(declaration: PresentationDeclaration) {
      if (!callbacks.current.connected) fail('DISCONNECTED');
      const api = declaration.mode === 'overlay' ? overlay.api : desktopApi;
      if (!api) fail('DESKTOP_REQUIRED');
      if (starting.current) fail('BUSY');
      if (active.current?.key === declaration.key && active.current.mode === declaration.mode
        && active.current.src === declaration.src && generation.current) return;
      starting.current = true;
      setOpening(true);
      let pending: Promise<{ generation: string }> | null = null;
      const attempt = declaration;
      try {
        const previous = active.current;
        const previousApi = activeApi.current;
        clearActive();
        pendingStart.current = attempt;
        if (previous && previousApi && previousApi !== api) await closeSurface(previousApi, previous.key);
        else await closePendingSurface();
        if (pendingStart.current !== attempt) fail('STALE_REQUEST');
        active.current = declaration;
        presentationId.current = allocatePresentationId();
        activeApi.current = api!;
        latest.current = readSnapshotRef.current();
        setIsActive(true);
        setNotice('');
        // Register the activation before open can synchronously close a previous in-app surface.
        pending = Promise.resolve().then(() => {
          if (pendingStart.current !== attempt) fail('STALE_REQUEST');
          if (!callbacks.current.connected) fail('DISCONNECTED');
          return api!.open({ key: declaration.key, mode: declaration.mode, src: declaration.src,
            title: declaration.title, width: declaration.width, height: declaration.height, config: declaration.config,
            resourceUrl: new URL(declaration.resourceUrl, window.location.href).href, state: latest.current! });
        }).then(async (opened) => {
          if (activation.current !== pending) fail('STALE_REQUEST');
          const conversation = latest.current!.conversations.find((item) => item.conversationKey === selected.current);
          if (conversation && conversation.conversationKey !== callbacks.current.sessionId) {
            await callbacks.current.onSelect(conversation.sessionId === 'new'
              ? { ...conversation, mode: 'agent' } : conversation);
          }
          if (activation.current !== pending) fail('STALE_REQUEST');
          return opened;
        });
        activation.current = pending;
        const opened = await pending;
        if (activation.current === pending) generation.current = opened.generation;
      } catch (error) {
        if (pending && activation.current === pending) {
          const failed = active.current;
          const failedId = presentationId.current;
          clearActive();
          await closeSurface(api!, declaration.key);
          if (failed) reportClosed(failed, failedId, 'open_failed');
        }
        if (error instanceof Error && error.message === 'STALE_REQUEST'
          && autoStartAttempts.current.get(declaration.key) === declaration) {
          autoStartAttempts.current.delete(declaration.key);
        }
        throw error;
      } finally {
        if (pendingStart.current === attempt) pendingStart.current = null;
        starting.current = false;
        setOpening(false);
      }
    },
    update(declaration: PresentationDeclaration) {
      setAvailable((previous) => {
        if (previous.get(declaration.key) === declaration) return previous;
        const next = new Map(previous); next.set(declaration.key, declaration); return next;
      });
      const pending = pendingStart.current;
      if (!active.current && pending?.key === declaration.key
        && (pending.revision !== declaration.revision || pending.mode !== declaration.mode || pending.src !== declaration.src)) {
        pendingStart.current = null;
      }
      if (active.current?.key !== declaration.key) return;
      const api = activeApi.current;
      if (active.current.mode !== declaration.mode || active.current.src !== declaration.src) {
        clearActive();
        if (api) void closeSurface(api, declaration.key).catch(() => setNotice(words.current('webuiPresentation.updateFailed')));
        return;
      }
      active.current = declaration;
      void api?.update(declaration.key, { config: declaration.config }).catch(() => setNotice(words.current('webuiPresentation.updateFailed')));
    },
    release(key: string) {
      autoStartAttempts.current.delete(key);
      if (pendingStart.current?.key === key) pendingStart.current = null;
      setAvailable((previous) => {
        if (!previous.has(key)) return previous;
        const next = new Map(previous); next.delete(key); return next;
      });
      if (active.current?.key !== key) return;
      const api = activeApi.current;
      clearActive();
      if (api) void closeSurface(api, key).catch(() => setNotice(words.current('webuiPresentation.updateFailed')));
    },
  }), [desktopApi, overlay, fail, clearActive, closeSurface, closePendingSurface, reportClosed]);

  useEffect(() => {
    if (!props.connected) return;
    for (const declaration of available.values()) {
      if (!declaration.autoStart) {
        autoStartAttempts.current.delete(declaration.key);
        continue;
      }
      if (samePresentationSource(autoStartAttempts.current.get(declaration.key), declaration)) continue;
      if (starting.current) break;
      autoStartAttempts.current.set(declaration.key, declaration);
      if (active.current && active.current.key !== declaration.key) {
        setNotice(words.current('webuiPresentation.autoStartBusy'));
        continue;
      }
      if (declaration.mode !== 'overlay' && !desktopApi) {
        reportClosed(declaration, allocatePresentationId(), 'open_failed');
        setNotice(words.current('webuiPresentation.desktopRequired'));
        continue;
      }
      void port.activate(declaration).catch(() => {
        if (autoStartAttempts.current.get(declaration.key) === declaration) {
          setNotice(words.current('webuiPresentation.openFailed'));
        }
      });
    }
  }, [available, port, props.connected, opening, desktopApi, reportClosed]);

  useEffect(() => () => {
    const declaration = active.current;
    const api = activeApi.current;
    active.current = null;
    activeApi.current = null;
    generation.current = null;
    activation.current = null;
    pendingStart.current = null;
    presentationId.current = null;
    childWindows.current.clear();
    childSync.current = null;
    requestLedgers.current.clear();
    const shutdown = declaration && api ? closeSurface(api, declaration.key) : closePendingSurface();
    void shutdown.catch((error) => console.error('Presentation shutdown failed', error));
    overlay.dispose();
  }, [overlay, closeSurface, closePendingSurface]);

  useEffect(() => {
    const handle = createPresentationStartHandler({
      runtime, language: () => i18n.resolvedLanguage ?? i18n.language ?? 'zh', desktop: () => !!desktopApi,
      activate: port.activate,
      result: (params) => webClient.request(PRESENTATION_RESULT_METHOD, params),
    });
    return webClient.on(PRESENTATION_START_EVENT, (event) => {
      void handle(event.payload).catch(() => setNotice(words.current('webuiPresentation.openFailed')));
    });
  }, [desktopApi, i18n, port, runtime]);

  useEffect(() => {
    const ledgers = requestLedgers.current;
    const sending = new Set<string>();
    const changing = new Set<string>();
    let creatingConversation = false;
    // A request is current on the active parent window or on a child whose parent generation still matches.
    const currentRequest = (windowKey: string, requestGeneration: string) =>
      (active.current?.key === windowKey && generation.current === requestGeneration)
      || (childWindows.current.get(windowKey)?.generation === requestGeneration
        && childWindows.current.get(windowKey)?.parentGeneration === generation.current);
    const execute = async (request: PresentationRequest, requestGeneration: string, windowKey: string, api: PresentationNativeApi) => {
      const declaration = active.current;
      if (!declaration || !latest.current!.connected) return fail('DISCONNECTED');
      if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
      const p = request.params;
      const key = p.conversationKey as string | undefined;
      const conversation = key ? latest.current!.conversations.find((item) => item.conversationKey === key) : null;
      if (key && !conversation) fail('TARGET_INACTIVE');
      switch (request.action) {
        case 'createConversation': {
          if (windowKey !== declaration!.key) fail('TARGET_INACTIVE');
          if (creatingConversation || sending.has('new') || changing.has('new')) fail('REQUEST_BUSY');
          const draft = latest.current!.conversations.find((item) => item.sessionId === 'new');
          if (!draft?.canSend) fail('CONVERSATION_BUSY');
          creatingConversation = true;
          sending.add('new');
          try {
            const target = { ...draft!, mode: p.mode as string };
            await callbacks.current.onSelect(target);
            if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
            useChatStore.getState().ensureRuntime('new');
            useChatStore.getState().setInputValue('new', p.text as string);
            const sent = await callbacks.current.onSend(target, p.text as string);
            if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
            if (sent.conversationKey && sent.conversationKey !== 'new') selected.current = sent.conversationKey;
            latest.current = readSnapshotRef.current();
            setRevision((value) => value + 1);
            if (!sent.accepted || !sent.conversationKey || sent.conversationKey === 'new') fail('SEND_FAILED');
            return { conversationKey: sent.conversationKey };
          } finally { creatingConversation = false; sending.delete('new'); }
        }
        case 'children': {
          if (windowKey !== declaration!.key) fail('TARGET_INACTIVE');
          // Only the native desktop source implements child windows; in-app overlays stay single-surface.
          if (!api.children) fail('WINDOWS_UNSUPPORTED');
          if (childSync.current) fail('REQUEST_BUSY');
          const sync = api.children!(declaration!.key, requestGeneration, p.windows as PresentationChild[]).then((result) => {
            if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
            const previous = childWindows.current;
            childWindows.current = new Map(result.windows.map((item) => [item.key,
              { generation: item.generation, parentGeneration: requestGeneration }]));
            for (const item of previous.values()) {
              if (!result.windows.some((entry) => entry.generation === item.generation)) ledgers.delete(item.generation);
            }
            return result;
          });
          childSync.current = sync;
          try { return await sync; }
          finally { if (childSync.current === sync) childSync.current = null; }
        }
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
          if (creatingConversation) fail('REQUEST_BUSY');
          await callbacks.current.onSelect(key === 'new' ? { ...conversation!, mode: 'agent' } : conversation!);
          if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
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
            if (conversation!.sessionId === 'new' && currentRequest(windowKey, requestGeneration)) selected.current = sent.conversationKey;
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
          if (creatingConversation) fail('REQUEST_BUSY');
          if (declaration.mode === 'overlay' && p.transition) fail('UNSUPPORTED_ACTION');
          await callbacks.current.onWorkspace(conversation ?? null);
          if (!currentRequest(windowKey, requestGeneration)) fail('STALE_REQUEST');
          if (declaration.mode === 'overlay') return true;
          if (p.transition && !latest.current!.reducedMotion) {
            await api.transitionWorkspace(declaration!.key, p.transition as { durationMs: number });
          } else await api.close(declaration!.key);
          return true;
        case 'resize': return api.resize(windowKey, p.width as number, p.height as number);
        case 'move': return api.move(windowKey, p.deltaX as number, p.deltaY as number);
        case 'hitTest': return api.hitTest(windowKey, p.interactive as boolean);
      }
    };
    const sources = desktopApi ? [desktopApi, overlay.api] : [overlay.api];
    const releases = sources.map((api) => {
      const offRequest = api.onRequest(async (envelope) => {
        if (activeApi.current !== api || !active.current) return;
        const isChild = envelope.parentKey === active.current.key && envelope.parentGeneration === generation.current;
        if (active.current.key !== envelope.key && !isChild) return;
        // Wait for surface readiness; queued messages never choose their own activation generation.
        const pending = activation.current;
        if (!pending) return;
        try { await pending; } catch { return; }
        if (isChild && childSync.current) { try { await childSync.current; } catch { return; } }
        if (activation.current !== pending || activeApi.current !== api || !active.current
          || !currentRequest(envelope.key, envelope.generation)) return;
        const request = parsePresentationRequest(envelope.request);
        if (!request) return;
        let ledger = ledgers.get(envelope.generation);
        if (!ledger) {
          ledger = createPresentationRequests((request) => execute(request, envelope.generation, envelope.key, api), (error) => {
            const code = error instanceof Error ? error.message : 'ACTION_FAILED';
            const message = words.current(`webuiPresentation.errors.${code}`, { defaultValue: words.current('webuiPresentation.actionFailed') });
            return { code, message };
          });
          ledgers.set(envelope.generation, ledger);
        }
        void ledger!(request).then((result) => api.respond(envelope.generation, result))
          .catch(() => setNotice(words.current('webuiPresentation.actionFailed')));
      });
      const offClosed = api.onClosed(async (event) => {
        if (childWindows.current.get(event.key)?.generation === event.generation) {
          childWindows.current.delete(event.key);
          ledgers.delete(event.generation);
          return;
        }
        const pending = activation.current;
        if (generation.current === null && pending) {
          try { await pending; } catch { return; }
        }
        if (activeApi.current !== api || active.current?.key !== event.key
          || (generation.current && generation.current !== event.generation)) return;
        const declaration = active.current;
        const closedId = presentationId.current;
        const mode = declaration.mode;
        clearActive();
        if (!['replaced', 'shutdown'].includes(event.reason)) reportClosed(declaration, closedId, event.reason);
        if (mode === 'overlay') {
          if (['ready_timeout', 'render_failed'].includes(event.reason)) setNotice(words.current('webuiPresentation.openFailed'));
        } else if (!['workspace', 'replaced', 'shutdown'].includes(event.reason)) setNotice(words.current('webuiPresentation.restored'));
      });
      return () => { offRequest(); offClosed(); };
    });
    return () => releases.forEach((release) => release());
  }, [desktopApi, overlay, fail, clearActive, reportClosed]);

  useEffect(() => {
    const declaration = active.current;
    const api = activeApi.current;
    if (!api || !declaration || !snapshot) return;
    if (!snapshot.connected) {
      clearActive();
      void closeSurface(api, declaration.key).catch(() => setNotice(t('webuiPresentation.updateFailed')));
      return;
    }
    void api.update(declaration.key, { state: snapshot }).catch(() => setNotice(t('webuiPresentation.updateFailed')));
  }, [snapshot, t, clearActive, closeSurface]);

  return (
    <PresentationHostContext.Provider value={port}>
      <WebUIHost slotId={PRESENTATION_SLOT} />
      <PresentationOverlay controller={overlay} />
      {manualCandidates.length ? <div className="webui-presentation-launcher" data-testid="webui-presentation-launcher">
        <DropdownMenu open={menuOpen} onOpenChange={(open) => { setMenuOpen(open); if (open) setNotice(''); }}>
          <DropdownMenuTrigger asChild><button type="button" className="webui-presentation-launcher__button"
            data-testid="webui-presentation-open" disabled={!props.connected || opening
              || (!desktopApi && !manualCandidates.some((item) => item.mode === 'overlay'))}>
            {opening ? t('webuiPresentation.opening') : t('webuiPresentation.choose')}
          </button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {manualCandidates.map((declaration) => <DropdownMenuItem key={declaration.key}
              disabled={declaration.mode !== 'overlay' && !desktopApi}
              onSelect={() => {
                setNotice('');
                void port.activate(declaration).catch(() => setNotice(t('webuiPresentation.openFailed')));
              }}>{declaration.title}</DropdownMenuItem>)}
          </DropdownMenuContent>
        </DropdownMenu>
        {!desktopApi && manualCandidates.some((item) => item.mode !== 'overlay')
          ? <span>{t('webuiPresentation.desktopRequired')}</span> : null}
      </div> : null}
      {notice ? <div role="status" className="webui-presentation-notice" data-testid="webui-presentation-notice">{notice}</div> : null}
    </PresentationHostContext.Provider>
  );
}
