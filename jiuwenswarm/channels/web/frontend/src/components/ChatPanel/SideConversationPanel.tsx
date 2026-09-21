import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowUp, GitFork, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import { MessageList } from './MessageList';
import './SideConversationPanel.css';

interface SideConversationPanelProps {
  sessionId: string;
  parentTitle: string;
  onSendMessage: (content: string) => Promise<boolean>;
  onClose: () => void;
}

export function SideConversationPanel({ sessionId, parentTitle, onSendMessage, onClose }: SideConversationPanelProps) {
  const { t } = useTranslation();
  const messages = useChatStore((state) => state.runtimes[sessionId]?.messages ?? []);
  const isProcessing = useChatStore((state) => state.runtimes[sessionId]?.isProcessing ?? false);
  const toolExecutionCount = useChatStore((state) => state.runtimes[sessionId]?.toolExecutionOrder.length ?? 0);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [sendFailed, setSendFailed] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const followBottomRef = useRef(true);

  useLayoutEffect(() => {
    followBottomRef.current = true;
  }, [sessionId]);

  useLayoutEffect(() => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const followBottom = () => {
      if (followBottomRef.current) viewport.scrollTop = viewport.scrollHeight;
    };
    followBottom();
    const observer = new ResizeObserver(followBottom);
    observer.observe(viewport);
    if (viewport.firstElementChild) observer.observe(viewport.firstElementChild);
    return () => observer.disconnect();
  }, [sessionId, isProcessing, messages, toolExecutionCount]);

  const submit = async () => {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true);
    setSendFailed(false);
    try {
      const sent = await onSendMessage(content);
      if (sent) {
        setDraft('');
      } else {
        setSendFailed(true);
      }
    } catch (error) {
      console.error('Failed to send side conversation message:', error);
      setSendFailed(true);
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  };

  return (
    <aside className="side-conversation-panel" data-testid="side-conversation-panel">
      <header className="side-conversation-panel__header">
        <div className="side-conversation-panel__heading">
          <div className="side-conversation-panel__title">
            <GitFork size={15} strokeWidth={1.8} aria-hidden="true" />
            <span>{t('chat.sideConversation.title')}</span>
          </div>
          <div
            className="side-conversation-panel__origin"
            title={parentTitle}
            data-testid="side-conversation-panel-origin"
          >
            {t('chat.sideConversation.from', { title: parentTitle })}
          </div>
        </div>
        <button
          type="button"
          className="side-conversation-panel__close"
          onClick={onClose}
          title={t('chat.sideConversation.close')}
          aria-label={t('chat.sideConversation.close')}
          data-testid="side-conversation-panel-close"
        >
          <X size={16} strokeWidth={1.8} aria-hidden="true" />
        </button>
      </header>

      <div
        className="side-conversation-panel__messages"
        ref={scrollRef}
        data-timeline-scroll-root
        data-testid="side-conversation-panel-messages"
        onScroll={(event) => {
          const viewport = event.currentTarget;
          followBottomRef.current = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 1;
        }}
        onWheel={(event) => {
          if (event.deltaY < 0) followBottomRef.current = false;
        }}
      >
        {messages.length > 0 || toolExecutionCount > 0 ? (
          <MessageList messages={messages} sessionId={sessionId} />
        ) : (
          <div className="side-conversation-panel__empty" data-testid="side-conversation-panel-empty">
            <GitFork size={20} strokeWidth={1.5} aria-hidden="true" />
            <span>{t('chat.sideConversation.empty')}</span>
          </div>
        )}
      </div>

      <div className="side-conversation-panel__composer">
        <div className="side-conversation-panel__input-shell">
          <textarea
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              if (sendFailed) setSendFailed(false);
            }}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.sideConversation.placeholder')}
            aria-label={t('chat.sideConversation.placeholder')}
            rows={3}
            data-testid="side-conversation-panel-input"
          />
          <button
            type="button"
            className="side-conversation-panel__send"
            disabled={!draft.trim() || sending}
            onClick={() => {
              void submit();
            }}
            title={t('chat.sideConversation.send')}
            aria-label={t('chat.sideConversation.send')}
            data-testid="side-conversation-panel-send"
          >
            {sending ? (
              <LoaderCircle className="side-conversation-panel__spinner" size={15} aria-hidden="true" />
            ) : (
              <ArrowUp size={15} strokeWidth={2} aria-hidden="true" />
            )}
          </button>
        </div>
        {sendFailed ? (
          <div className="side-conversation-panel__error" role="alert">
            {t('chat.sideConversation.sendFailed')}
          </div>
        ) : isProcessing ? (
          <div className="side-conversation-panel__status" data-testid="side-conversation-panel-status">
            <LoaderCircle className="side-conversation-panel__spinner" size={12} aria-hidden="true" />
            {t('chat.sideConversation.working')}
          </div>
        ) : null}
      </div>
    </aside>
  );
}
