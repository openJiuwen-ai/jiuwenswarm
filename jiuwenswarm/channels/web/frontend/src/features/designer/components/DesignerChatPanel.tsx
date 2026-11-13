<<<<<<< HEAD
import { PanelRight, Square, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
||||||| e487e9a30
import { PanelRight, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
=======
import { FileText, Film, Image as ImageIcon, LoaderCircle, Music, PanelRight, Square, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react';
>>>>>>> origin/develop
import { useTranslation } from 'react-i18next';
import SendPlaneIcon from '../../../assets/send_plane.svg?react';
import { MessageList } from '../../../components/ChatPanel/MessageList';
import { InlineQuestionCard } from '../../../components/ChatPanel/InlineQuestionCard';
import { InteractionSlot } from '../../../components/InteractionSlot';
import { isImeCompositionKey } from '../../../components/ChatPanel/imeComposition';
import { webClient } from '../../../services/webClient';
<<<<<<< HEAD
import { ApplicationPluginTaskInputActions } from '../../../applicationPlugins/ApplicationPluginOutlet';
import { requestSettingsModule } from '../../settings/settingsNavigation';
import { toast } from '../../../components/ui';
||||||| e487e9a30
=======
import { ApplicationPluginTaskInputActions } from '../../../applicationPlugins/ApplicationPluginOutlet';
import { SETTINGS_CONFIG_CHANGED_EVENT } from '../../settings/services/settingsContract';
import { requestSettingsModule } from '../../settings/settingsNavigation';
import { toast } from '../../../components/ui';
>>>>>>> origin/develop
import { useChatStore } from '../../../stores/chatStore';
import { useSessionStore } from '../../../stores/sessionStore';
import { buildConversationModelPayload } from '../../../multi-session/state/conversationModelSelection';
import type { Message, UserAnswer } from '../../../types';
import {
  DESIGNER_TEAM_MODE,
  bindDesignerChatSession,
  composeDesignerSession,
  sendDesignerLeaderMessage,
} from '../designerEntry';
<<<<<<< HEAD
import { findDesignModelsProblem, loadedDesignChatAnthropicProblem } from '../designModelsReadiness';
||||||| e487e9a30
=======
import { findDesignModelsProblem } from '../designModelsReadiness';
>>>>>>> origin/develop
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
<<<<<<< HEAD
import { useDesignerRunStore } from '../designerRunStore';
||||||| e487e9a30
=======
import { designerChatStopControl, isDesignerChatStoppable } from '../designerGraphLock';
import { useDesignerRunStore } from '../designerRunStore';
>>>>>>> origin/develop
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
<<<<<<< HEAD
import DesignerModelBadge from './DesignerModelBadge';
||||||| e487e9a30
=======
import { designerAssetPreviewUrl } from '../designerAssetUrl';
import {
  collectMentionedNodes,
  detectMentionQuery,
  filterMentionOptions,
  insertMention,
  isPreviewableMention,
  mentionOptions,
} from '../designerMentions';
import DesignerModelBadge from './DesignerModelBadge';
>>>>>>> origin/develop
import {
  DESIGNER_REF_LIMITS,
  designerReferenceKindFromMime,
  designerReferencePreviewUrl,
  filesToBootstrapReferences,
  type DesignerReferenceKind,
  type DesignerStoredReference,
} from '../designerReferences';

type ComposerDraft = {
  id: string;
  file: File;
  kind: DesignerReferenceKind;
  filename: string;
  previewUrl: string | null;
};

export function DesignerEmptyState({
  variant,
  errorMessage,
}: {
  variant: 'empty' | 'error';
  errorMessage?: string | null;
}) {
  const { t } = useTranslation();

  return (
    <div className="designer-page__state" data-testid="designer-empty-state" data-variant={variant}>
      <div className="designer-page__state-card">
        <h2 className="designer-page__state-title">
          {variant === 'error' ? t('designer.loadErrorTitle') : t('designer.emptyTitle')}
        </h2>
        <p className="designer-page__state-desc">
          {variant === 'error' ? errorMessage || t('designer.loadErrorFallback') : t('designer.emptyDescription')}
        </p>
      </div>
    </div>
  );
}

function ReferenceChips({
  items,
  onRemove,
  removeLabel,
}: {
  items: Array<DesignerStoredReference & { previewUrl?: string | null; id?: string }>;
  onRemove?: (id: string) => void;
  removeLabel?: string;
}) {
  if (items.length === 0) return null;
  return (
    <ul className="designer-chat-panel__refs" data-testid="designer-chat-panel-refs">
      {items.map((item, index) => {
        const key = item.id || `${item.kind}-${item.filename}-${index}`;
        const preview = item.previewUrl || designerReferencePreviewUrl(item);
        return (
          <li key={key} className="designer-chat-panel__ref" data-kind={item.kind}>
            {preview ? <img src={preview} alt="" /> : <span>{item.kind}</span>}
            <em title={item.filename}>{item.filename}</em>
            {onRemove && item.id ? (
              <button
                type="button"
                className="designer-chat-panel__ref-remove"
                aria-label={removeLabel || 'remove'}
                onClick={() => onRemove(item.id as string)}
              >
                <X size={12} aria-hidden />
              </button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/** Stand-in thumbnail for an "@" option with no image preview. */
function MentionKindIcon({ type }: { type: string }) {
  if (type === 'video') return <Film size={14} />;
  if (type === 'audio') return <Music size={14} />;
  if (type === 'image') return <ImageIcon size={14} />;
  return <FileText size={14} />;
}

const EMPTY_MESSAGES: Message[] = [];

export function DesignerChatPanel() {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const sessionId = String(domainGraph?.metadata?.session_id ?? '').trim() || null;
  const messages = useChatStore((state) =>
    sessionId ? state.runtimes[sessionId]?.messages ?? EMPTY_MESSAGES : EMPTY_MESSAGES,
  );
  const isProcessing = useChatStore((state) =>
    sessionId ? state.runtimes[sessionId]?.isProcessing ?? false : false,
  );
  const runIsRunning = useDesignerRunStore((state) => state.isRunning);
  const runTriggerSource = useDesignerRunStore((state) => state.run?.metadata?.trigger_source ?? null);
  const leaderRunActive = runIsRunning && runTriggerSource === 'leader';
  // After chat.final, isProcessing is false while the leader-triggered canvas
  // autorun continues. Keep the stop control for that phase too.
  const canStop = isDesignerChatStoppable({
    isProcessing,
    isRunning: runIsRunning,
    triggerSource: runTriggerSource,
  });
  const toggleChatSidebar = useDesignerUiStore((state) => state.toggleChatSidebar);
  const bodyRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<ComposerDraft[]>([]);
  const [attachError, setAttachError] = useState('');
  const [sending, setSending] = useState(false);
<<<<<<< HEAD
  const stopRequestedRef = useRef(false);
  // The canvas owns exactly one session: read and write the model against that
  // session id. `activeSessionId` follows the app route and can point at
  // another conversation while this panel stays mounted (#5394, #8995).
  const selectedModelName = useSessionStore((state) =>
    sessionId ? state.runtimes[sessionId]?.selectedModelName ?? null : null,
||||||| e487e9a30
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const chatAvailableModels = useSessionStore((state) => state.chatAvailableModels);
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
=======
  const [stopPending, setStopPending] = useState(false);
  const stopControl = designerChatStopControl({ canStop, stopPending });
  const [atMenu, setAtMenu] = useState<{ start: number; query: string; index: number } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Safari can fire compositionend before the keydown that confirms a candidate.
  const composingRef = useRef(false);
  const stopRequestedRef = useRef(false);
  // The canvas owns exactly one session: read and write the model against that
  // session id. `activeSessionId` follows the app route and can point at
  // another conversation while this panel stays mounted (#5394, #8995).
  const selectedModelName = useSessionStore((state) =>
    sessionId ? state.runtimes[sessionId]?.selectedModelName ?? null : null,
>>>>>>> origin/develop
  );
  const selectedModelSelection = useSessionStore((state) =>
    sessionId ? state.runtimes[sessionId]?.selectedModelSelection ?? null : null,
  );
<<<<<<< HEAD
  const setChatModel = useDesignerStore((state) => state.setChatModel);
  const availableModelCount = useSessionStore((state) => state.chatAvailableModels.length);
  // The director chain resolves its preferred model from graph.metadata.model_name,
  // and leader rounds run on the session selection: keep both in sync so a pick
  // made here takes effect on the next round instead of only relabelling the UI.
  useEffect(() => {
    if (!sessionId || !selectedModelName) return;
    setChatModel(selectedModelName);
  }, [selectedModelName, sessionId, setChatModel]);
||||||| e487e9a30
  const modelLabel = selectedModel?.alias || selectedModel?.model_name || '';
=======
  const setChatModel = useDesignerStore((state) => state.setChatModel);
  const availableModelCount = useSessionStore((state) => state.chatAvailableModels.length);
  const selectedModelId = selectedModelSelection?.type === 'model' ? selectedModelSelection.id : undefined;
  // The director chain resolves its preferred model from graph metadata,
  // and leader rounds run on the session selection: keep both in sync so a pick
  // made here takes effect on the next round instead of only relabelling the UI.
  // A shared model_name is not enough; the stable id distinguishes custom rows.
  useEffect(() => {
    if (!sessionId || !selectedModelName) return;
    setChatModel(selectedModelName, selectedModelId);
  }, [selectedModelId, selectedModelName, sessionId, setChatModel]);
>>>>>>> origin/develop

  const composerBusy = isProcessing || sending;
  const chatBusy = composerBusy || leaderRunActive;
  const canSend = Boolean(draft.trim() || attachments.length > 0);
<<<<<<< HEAD
  const ensureDuplexSession = useCallback(async () => {
    if (!sessionId || !domainGraph?.graph_id || isDesignerPreviewGraph(domainGraph)) return null;
    try {
      await useDesignerStore.getState().flushSave();
      bindDesignerChatSession(sessionId);
      return sessionId;
    } catch (error) {
      setAttachError(error instanceof Error ? error.message : t('designer.loadErrorFallback'));
      return null;
    }
  }, [sessionId, domainGraph, t]);

  // Design has no model switch: a missing chat model means no leader round can
  // run at all, so warn here instead of failing only after the user sends
  // (#5266). Re-checked whenever the catalog size changes, e.g. after the user
  // fixes Settings in another tab.
  const [blockedProblem, setBlockedProblem] = useState('');
  useEffect(() => {
    if (!sessionId) {
      setBlockedProblem('');
      return;
    }
    let cancelled = false;
    void findDesignModelsProblem(sessionId).then((problem) => {
      if (!cancelled) setBlockedProblem(problem ?? '');
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, availableModelCount, selectedModelName]);
||||||| e487e9a30
=======
  const ensureDuplexSession = useCallback(async () => {
    if (!sessionId || !domainGraph?.graph_id || isDesignerPreviewGraph(domainGraph)) return null;
    try {
      await useDesignerStore.getState().flushSave();
      bindDesignerChatSession(sessionId);
      return sessionId;
    } catch (error) {
      setAttachError(error instanceof Error ? error.message : t('designer.loadErrorFallback'));
      return null;
    }
  }, [sessionId, domainGraph, t]);

  // Design has no model switch: a missing chat model means no leader round can
  // run at all, so warn here instead of failing only after the user sends
  // (#5266). Re-checked when the model catalog size changes or a config change
  // is pushed, since generation-slot switches never change the catalog.
  const [blockedProblem, setBlockedProblem] = useState('');
  const [configRevision, setConfigRevision] = useState(0);
  useEffect(() => {
    const onConfigChanged = () => setConfigRevision((value) => value + 1);
    window.addEventListener(SETTINGS_CONFIG_CHANGED_EVENT, onConfigChanged);
    return () => window.removeEventListener(SETTINGS_CONFIG_CHANGED_EVENT, onConfigChanged);
  }, []);
  useEffect(() => {
    if (!sessionId) {
      setBlockedProblem('');
      return;
    }
    let cancelled = false;
    void findDesignModelsProblem().then((problem) => {
      if (!cancelled) setBlockedProblem(problem ?? '');
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, availableModelCount, configRevision, selectedModelName]);

  // "@" offers the canvas nodes by label; mentioned nodes preview above the input.
  const allMentionOptions = useMemo(() => mentionOptions(domainGraph), [domainGraph]);
  const atCandidates = useMemo(
    () => (atMenu ? filterMentionOptions(allMentionOptions, atMenu.query) : []),
    [allMentionOptions, atMenu],
  );
  const draftMentions = useMemo(
    () => collectMentionedNodes(domainGraph, draft).filter(isPreviewableMention),
    [domainGraph, draft],
  );

  const chooseMention = useCallback(
    (label: string) => {
      if (!atMenu) return;
      const next = insertMention(draft, atMenu, label);
      setDraft(next.value);
      setAtMenu(null);
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(next.caret, next.caret);
      });
    },
    [atMenu, draft],
  );

  const onDraftChange = useCallback((event: ChangeEvent<HTMLTextAreaElement>) => {
    const { value } = event.target;
    setDraft(value);
    const token = detectMentionQuery(value, event.target.selectionStart ?? value.length, allMentionOptions);
    setAtMenu(token ? { ...token, index: 0 } : null);
  }, [allMentionOptions]);
>>>>>>> origin/develop

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (sessionId) bindDesignerChatSession(sessionId);
  }, [sessionId]);

<<<<<<< HEAD
  useEffect(() => {
    if (isProcessing || !stopRequestedRef.current) return;
    stopRequestedRef.current = false;
    useDesignerRunStore.getState().applyLeaderActivity(null);
  }, [isProcessing]);

||||||| e487e9a30
=======
  useEffect(() => {
    if (canStop) return;
    if (stopRequestedRef.current) {
      stopRequestedRef.current = false;
      useDesignerRunStore.getState().applyLeaderActivity(null);
    }
    setStopPending(false);
  }, [canStop]);

>>>>>>> origin/develop
  const limitError = useCallback(
    (kind: DesignerReferenceKind) => {
      if (kind === 'image') return t('designer.chat.limitImages');
      if (kind === 'video') return t('designer.chat.limitVideo');
      return t('designer.chat.limitAudio');
    },
    [t],
  );

  const addFiles = useCallback(
    (fileList: FileList | File[]) => {
      const next = Array.from(fileList);
      if (next.length === 0) return;
      setAttachError('');
      setAttachments((current) => {
        const counts = { image: 0, video: 0, audio: 0 };
        for (const item of current) counts[item.kind] += 1;
        const accepted: ComposerDraft[] = [];
        for (const file of next) {
          const kind = designerReferenceKindFromMime(file.type, file.name);
          if (!kind) {
            setAttachError(t('designer.chat.unsupported'));
            continue;
          }
          if (counts[kind] >= DESIGNER_REF_LIMITS[kind]) {
            setAttachError(limitError(kind));
            continue;
          }
          counts[kind] += 1;
          accepted.push({
            id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
            file,
            kind,
            filename: file.name,
            previewUrl: kind === 'image' ? URL.createObjectURL(file) : null,
          });
        }
        return accepted.length > 0 ? [...current, ...accepted] : current;
      });
    },
    [limitError, t],
  );

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => {
      const target = current.find((item) => item.id === id);
      if (target?.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(target.previewUrl);
      return current.filter((item) => item.id !== id);
    });
  }, []);

  const handleSend = useCallback(() => {
    const content = draft.trim();
    if ((!content && attachments.length === 0) || chatBusy) return;
<<<<<<< HEAD
    // Same gate as the landing page: a round needs the chat model and both
    // generation slots, so refuse loudly instead of failing mid-run (#5266).
    // Anthropic is read from the in-memory catalog so the click returns before
    // any design RPC, even if the warning effect has not finished.
    const protocolProblem = loadedDesignChatAnthropicProblem(sessionId, selectedModelName);
    if (protocolProblem || blockedProblem) {
      toast.open({ content: protocolProblem || blockedProblem, variant: 'error' });
      return;
    }
||||||| e487e9a30
=======
    // Same gate as the landing page: a round needs the chat model and both
    // generation slots, so refuse loudly instead of failing mid-run (#5266).
    if (blockedProblem) {
      toast.open({ content: blockedProblem, variant: 'error' });
      return;
    }
    // One execution runs at a time, so one message may change one referenced node.
    if (collectMentionedNodes(domainGraph, content).length > 1) {
      toast.open({ content: t('designer.chat.multipleMentions'), variant: 'error' });
      return;
    }
>>>>>>> origin/develop
    setSending(true);
    const files = attachments.map((item) => item.file);
    void filesToBootstrapReferences(files)
      .then(async (converted) => {
        if (converted.error) {
          const map: Record<string, string> = {
            image_limit: t('designer.chat.limitImages'),
            video_limit: t('designer.chat.limitVideo'),
            audio_limit: t('designer.chat.limitAudio'),
            too_large: t('designer.chat.tooLarge'),
            unsupported: t('designer.chat.unsupported'),
          };
          setAttachError(map[converted.error] || t('designer.chat.unsupported'));
          return;
        }
        setDraft('');
        attachments.forEach((item) => {
          if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
        });
        setAttachments([]);
        const existingGraph = domainGraph && !isDesignerPreviewGraph(domainGraph) ? domainGraph : null;
        const projectId = String(existingGraph?.project_id || '');
        if (!existingGraph?.graph_id || !sessionId) {
          setAttachError(t('designer.loadErrorFallback'));
          return undefined;
        }
        if (existingGraph.nodes.length === 0 && projectId) {
          return composeDesignerSession({
            projectId,
            sessionId,
            prompt: content,
            references: converted.refs,
            errorText: t('designer.chat.bootstrapError'),
          });
        }
        return sendDesignerLeaderMessage({
          sessionId,
          prompt: content,
          errorText: t('designer.chat.updateError'),
        });
      })
      .finally(() => {
        setSending(false);
      });
<<<<<<< HEAD
  }, [attachments, blockedProblem, chatBusy, domainGraph, draft, selectedModelName, sessionId, t]);

  const handleStop = useCallback(() => {
    if (!sessionId) return;
    setAttachError('');
    stopRequestedRef.current = true;
    const runStore = useDesignerRunStore.getState();
    const leaderRunActive = runStore.isRunning && runStore.run?.metadata?.trigger_source === 'leader';
    // Cancel through the engine first: pausing the leader kills its trigger
    // task outright, skipping the abort that unwinds the run cleanly.
    void (leaderRunActive ? runStore.cancel(domainGraph) : Promise.resolve())
      .then(() =>
        webClient.request('chat.interrupt', { session_id: sessionId, intent: 'pause', mode: DESIGNER_TEAM_MODE }),
      )
      .catch(() => {
        stopRequestedRef.current = false;
        setAttachError(t('designer.chat.stopError'));
      });
  }, [domainGraph, sessionId, t]);
||||||| e487e9a30
  }, [attachments, chatBusy, domainGraph, draft, sessionId, t]);
=======
  }, [attachments, blockedProblem, chatBusy, domainGraph, draft, selectedModelName, sessionId, t]);

  const handleStop = useCallback(() => {
    if (!sessionId || stopPending) return;
    setAttachError('');
    setStopPending(true);
    stopRequestedRef.current = true;
    const runStore = useDesignerRunStore.getState();
    const cancelLeaderRun =
      runStore.isRunning && runStore.run?.metadata?.trigger_source === 'leader';
    const interruptChat = Boolean(
      useChatStore.getState().getRuntime(sessionId)?.isProcessing,
    );
    // Cancel the canvas autorun first: pausing the leader alone can kill its
    // trigger task without unwinding the run. Skip chat.interrupt when the
    // leader turn already finished and only child nodes are still running.
    void (cancelLeaderRun ? runStore.cancel(domainGraph) : Promise.resolve())
      .then(() => {
        if (!interruptChat) return undefined;
        return webClient.request('chat.interrupt', {
          session_id: sessionId,
          intent: 'pause',
          mode: DESIGNER_TEAM_MODE,
        });
      })
      .catch(() => {
        stopRequestedRef.current = false;
        setStopPending(false);
        setAttachError(t('designer.chat.stopError'));
      });
  }, [domainGraph, sessionId, stopPending, t]);
>>>>>>> origin/develop

  const handleUserAnswer = useCallback(
    async (requestId: string, answers: UserAnswer[], source?: string): Promise<boolean> => {
      if (!sessionId) return false;
      const pendingQuestion = useChatStore.getState().getRuntime(sessionId)?.pendingQuestions[0];
      const interruptId =
        pendingQuestion?.request_id === requestId ? pendingQuestion.interruptId : undefined;
      try {
        if (pendingQuestion?.duplexJobId && pendingQuestion.request_id === requestId) {
          await webClient.request('video.search.answer', {
            session_id: sessionId, job_id: pendingQuestion.duplexJobId, request_id: requestId, answers,
          });
          useChatStore.getState().consumePendingQuestion(sessionId, pendingQuestion);
          return true;
        }
        await webClient.request('chat.send', {
          session_id: sessionId,
          query: '',
          mode: DESIGNER_TEAM_MODE,
          request_id: requestId,
          answers,
          ...buildConversationModelPayload({
            mode: DESIGNER_TEAM_MODE,
            selectedModelName,
            selectedModelSelection,
          }),
          ...(source ? { source } : {}),
          ...(interruptId ? { interrupt_id: interruptId } : {}),
        });
        if (pendingQuestion) {
          useChatStore.getState().consumePendingQuestion(sessionId, pendingQuestion);
        }
        return true;
      } catch {
        return false;
      }
    },
    [sessionId, selectedModelName, selectedModelSelection],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      // While an IME is composing, Enter/Tab confirm a candidate, the arrows move
      // through candidates and Escape cancels: none of them is meant for the
      // "@" menu or for sending (Firefox and some WebKit report key "Enter" here).
      if (isImeCompositionKey(event.nativeEvent, composingRef.current)) return;
      if (atMenu && atCandidates.length > 0) {
        const count = atCandidates.length;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          const step = event.key === 'ArrowDown' ? 1 : -1;
          setAtMenu((cur) => (cur ? { ...cur, index: (cur.index + step + count) % count } : cur));
          return;
        }
        if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault();
          chooseMention(atCandidates[Math.min(atMenu.index, count - 1)].label);
          return;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          setAtMenu(null);
          return;
        }
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        handleSend();
      }
    },
    [atCandidates, atMenu, chooseMention, handleSend],
  );

  return (
    <aside
      className="designer-chat-panel designer-chat-panel--docked"
      aria-label={t('designer.sidebar.conversation')}
      data-testid="designer-chat-panel"
    >
      <div className="designer-chat-panel__header">
        <h2 className="designer-chat-panel__title" data-testid="designer-chat-panel-title">
          {t('designer.sidebar.conversation')}
        </h2>
        <button
          type="button"
          className="designer-chat-panel__collapse"
          onClick={toggleChatSidebar}
          aria-label={t('designer.chat.collapse')}
          title={t('designer.chat.collapse')}
          data-testid="designer-chat-panel-collapse"
        >
          <PanelRight size={18} aria-hidden />
        </button>
      </div>

      <div className="designer-chat-panel__body" ref={bodyRef} data-testid="designer-chat-panel-body">
        {messages.length === 0 ? (
          <p className="designer-chat-panel__empty">{t('designer.chat.emptyHint')}</p>
        ) : (
          <MessageList messages={messages} virtualized={false} />
        )}
        <InlineQuestionCard onSubmit={handleUserAnswer} />
      </div>
      <div className="designer-chat-panel__composer">
        <InteractionSlot onSubmit={handleUserAnswer} />
        {attachments.length > 0 ? (
          <ReferenceChips
            items={attachments.map((item) => ({
              id: item.id,
              kind: item.kind,
              filename: item.filename,
              previewUrl: item.previewUrl,
            }))}
            onRemove={removeAttachment}
            removeLabel={t('designer.chat.removeAttachment')}
          />
        ) : null}
        {draftMentions.length > 0 ? (
          <ReferenceChips
            items={draftMentions.map((item) => ({
              kind: item.type === 'video' ? 'video' : 'image',
              filename: item.label,
              previewUrl: item.type === 'image' ? designerAssetPreviewUrl(item.uri) : null,
            }))}
          />
        ) : null}
        {attachError ? (
          <p className="designer-chat-panel__attach-error" data-testid="designer-chat-panel-attach-error">
            {attachError}
          </p>
        ) : null}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*,video/*,audio/*"
          multiple
          hidden
          data-testid="designer-chat-panel-file"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files);
            event.target.value = '';
          }}
        />
        {/* The leader cannot run without a chat model: warn before the user
            types, and point straight at the Settings page (#5266). */}
        {blockedProblem ? (
          <div
            className="designer-chat-panel__model-warning"
            role="status"
            data-testid="designer-chat-model-warning"
          >
            <span className="designer-chat-panel__model-warning-text">{blockedProblem}</span>
            <button
              type="button"
              className="designer-chat-panel__model-warning-action"
              onClick={() => requestSettingsModule('models')}
              data-testid="designer-chat-model-warning-action"
            >
              {t('designer.models.openModelSettings')}
            </button>
          </div>
        ) : null}
        {/* Canvas chat sidebar: hide attach control; landing prompt keeps its own attach UI. */}
        <div className="designer-chat-panel__composer-box">
          {atMenu && atCandidates.length > 0 ? (
            <ul className="designer-chat-panel__at-menu" role="listbox" data-testid="designer-chat-panel-at-menu">
              {atCandidates.map((item, index) => {
                const preview = item.type === 'image' ? designerAssetPreviewUrl(item.uri) : null;
                return (
                  <li key={item.nodeId} role="option" aria-selected={index === atMenu.index}>
                    <button
                      type="button"
                      className={index === atMenu.index ? 'is-active' : ''}
                      data-testid="designer-chat-panel-at-option"
                      data-node-id={item.nodeId}
                      onMouseDown={(event) => {
                        event.preventDefault();
                        chooseMention(item.label);
                      }}
                    >
                      <span className="designer-chat-panel__at-thumb" data-kind={item.type} aria-hidden>
                        {preview ? <img src={preview} alt="" /> : <MentionKindIcon type={item.type} />}
                      </span>
                      <span className="designer-chat-panel__at-label" title={item.label}>
                        {item.label}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
          <textarea
            ref={textareaRef}
            className="designer-chat-panel__input"
            placeholder={t('designer.chat.inputPlaceholder')}
            value={draft}
            disabled={sending}
            data-testid="designer-chat-panel-input"
            onChange={onDraftChange}
            onKeyDown={onKeyDown}
            onBlur={() => setAtMenu(null)}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
            }}
          />
          <div className="designer-chat-panel__composer-actions">
            {sessionId ? (
              /* No model switch on the canvas: show the model Design runs on (#5266). */
              <DesignerModelBadge sessionId={sessionId} />
            ) : null}
<<<<<<< HEAD
            <ApplicationPluginTaskInputActions
              eligible={
                Boolean(sessionId && domainGraph?.graph_id && !isDesignerPreviewGraph(domainGraph)) &&
                !chatBusy && attachments.length === 0
              }
              hasTextDraft={Boolean(draft.trim())}
              sessionId={sessionId}
              ensureSession={ensureDuplexSession}
              labels={{
                start: t('chat.taskFullDuplexStart'),
                starting: t('chat.taskFullDuplexStarting'),
                stop: t('chat.taskFullDuplexStop'),
              }}
              fallback={
                isProcessing ? (
                  <button
                    type="button"
                    className="designer-chat-panel__send"
                    onClick={handleStop}
                    aria-label={t('designer.chat.stop')}
                    title={t('designer.chat.stop')}
                    data-testid="designer-chat-panel-stop"
                  >
                    <Square className="designer-chat-panel__stop-icon" fill="currentColor" aria-hidden />
                  </button>
                ) : (
                  <button
                    type="button"
                    className="designer-chat-panel__send"
                    disabled={chatBusy || !canSend}
                    onClick={handleSend}
                    aria-label={t('designer.chat.send')}
                    title={blockedProblem || undefined}
                    data-testid="designer-chat-panel-send"
                  >
                    <SendPlaneIcon className="designer-chat-panel__send-icon" aria-hidden />
                  </button>
                )
              }
            />
||||||| e487e9a30
            <button
              type="button"
              className="designer-chat-panel__send"
              disabled={chatBusy || !canSend}
              onClick={handleSend}
              aria-label={t('designer.chat.send')}
              data-testid="designer-chat-panel-send"
            >
              <SendPlaneIcon className="designer-chat-panel__send-icon" aria-hidden />
            </button>
=======
            <ApplicationPluginTaskInputActions
              eligible={
                Boolean(sessionId && domainGraph?.graph_id && !isDesignerPreviewGraph(domainGraph)) &&
                !chatBusy && attachments.length === 0
              }
              hasTextDraft={Boolean(draft.trim())}
              sessionId={sessionId}
              ensureSession={ensureDuplexSession}
              labels={{
                start: t('chat.taskFullDuplexStart'),
                starting: t('chat.taskFullDuplexStarting'),
                stop: t('chat.taskFullDuplexStop'),
              }}
              fallback={
                stopControl === 'stopping' ? (
                  <button
                    type="button"
                    className="designer-chat-panel__send"
                    disabled
                    aria-busy
                    aria-label={t('designer.chat.stopping')}
                    title={t('designer.chat.stopping')}
                    data-testid="designer-chat-panel-stop"
                    data-variant="running"
                  >
                    <LoaderCircle
                      className="designer-chat-panel__send-spinner animate-spin"
                      size={18}
                      aria-hidden
                    />
                  </button>
                ) : stopControl === 'stop' ? (
                  <button
                    type="button"
                    className="designer-chat-panel__send"
                    onClick={handleStop}
                    aria-label={t('designer.chat.stop')}
                    title={t('designer.chat.stop')}
                    data-testid="designer-chat-panel-stop"
                  >
                    <Square className="designer-chat-panel__stop-icon" fill="currentColor" aria-hidden />
                  </button>
                ) : (
                  <button
                    type="button"
                    className="designer-chat-panel__send"
                    disabled={chatBusy || !canSend}
                    onClick={handleSend}
                    aria-label={chatBusy ? t('designer.chat.running') : t('designer.chat.send')}
                    aria-busy={chatBusy}
                    title={blockedProblem || (chatBusy ? t('designer.chat.running') : undefined)}
                    data-testid="designer-chat-panel-send"
                    data-variant={chatBusy ? 'running' : 'idle'}
                  >
                    {/* A leader round can run for minutes (graph autorun); the button
                        must read as "working", never as the idle send plane (#5410). */}
                    {chatBusy ? (
                      <LoaderCircle
                        className="designer-chat-panel__send-spinner animate-spin"
                        size={18}
                        aria-hidden
                      />
                    ) : (
                      <SendPlaneIcon className="designer-chat-panel__send-icon" aria-hidden />
                    )}
                  </button>
                )
              }
            />
>>>>>>> origin/develop
          </div>
        </div>
      </div>
    </aside>
  );
}
