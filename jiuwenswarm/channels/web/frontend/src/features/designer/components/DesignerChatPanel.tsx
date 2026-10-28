import { LoaderCircle, PanelRight, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import SendPlaneIcon from '../../../assets/send_plane.svg?react';
import ChatModelSelector from '../../../components/ChatPanel/ChatModelSelector';
import { MessageList } from '../../../components/ChatPanel/MessageList';
import { InlineQuestionCard } from '../../../components/ChatPanel/InlineQuestionCard';
import { InteractionSlot } from '../../../components/InteractionSlot';
import { webClient } from '../../../services/webClient';
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
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
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
  const toggleChatSidebar = useDesignerUiStore((state) => state.toggleChatSidebar);
  const bodyRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<ComposerDraft[]>([]);
  const [attachError, setAttachError] = useState('');
  const [sending, setSending] = useState(false);
  // The canvas owns exactly one session: read and write the model against that
  // session id. `activeSessionId` follows the app route and can point at
  // another conversation while this panel stays mounted (#5394, #8995).
  const selectedModelName = useSessionStore((state) =>
    sessionId ? state.runtimes[sessionId]?.selectedModelName ?? null : null,
  );
  const selectedModelSelection = useSessionStore((state) =>
    sessionId ? state.runtimes[sessionId]?.selectedModelSelection ?? null : null,
  );
  const setChatModel = useDesignerStore((state) => state.setChatModel);
  // The director chain resolves its preferred model from graph.metadata.model_name,
  // and leader rounds run on the session selection: keep both in sync so a pick
  // made here takes effect on the next round instead of only relabelling the UI.
  useEffect(() => {
    if (!sessionId || !selectedModelName) return;
    setChatModel(selectedModelName);
  }, [selectedModelName, sessionId, setChatModel]);

  const chatBusy = isProcessing || sending;
  const canSend = Boolean(draft.trim() || attachments.length > 0);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (sessionId) bindDesignerChatSession(sessionId);
  }, [sessionId]);

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
  }, [attachments, chatBusy, domainGraph, draft, sessionId, t]);

  const handleUserAnswer = useCallback(
    async (requestId: string, answers: UserAnswer[], source?: string): Promise<boolean> => {
      if (!sessionId) return false;
      const pendingQuestion = useChatStore.getState().getRuntime(sessionId)?.pendingQuestions[0];
      try {
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
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        handleSend();
      }
    },
    [handleSend],
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
        {/* Canvas chat sidebar: hide attach control; landing prompt keeps its own attach UI. */}
        <div className="designer-chat-panel__composer-box">
          <textarea
            className="designer-chat-panel__input"
            placeholder={t('designer.chat.inputPlaceholder')}
            value={draft}
            disabled={chatBusy}
            data-testid="designer-chat-panel-input"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="designer-chat-panel__composer-actions">
            {sessionId ? (
              <ChatModelSelector sessionId={sessionId} disabled={chatBusy} />
            ) : null}
            <button
              type="button"
              className="designer-chat-panel__send"
              disabled={chatBusy || !canSend}
              onClick={handleSend}
              aria-label={chatBusy ? t('designer.chat.running') : t('designer.chat.send')}
              aria-busy={chatBusy}
              title={chatBusy ? t('designer.chat.running') : undefined}
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
          </div>
        </div>
      </div>
    </aside>
  );
}
