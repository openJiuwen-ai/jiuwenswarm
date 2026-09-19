/**
 * The bottom chat strip: the current session's own stream, reduced to the last
 * agent message, its receipt chip and the composer. History lives in the rail.
 */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Minus, MessageSquare, ChevronUp } from 'lucide-react';
import { useChatStore } from '../../../../channels/web/frontend/src/stores/chatStore';
import { InputArea } from '../../../../channels/web/frontend/src/components/ChatPanel/InputArea';
import { InteractionSlot } from '../../../../channels/web/frontend/src/components/InteractionSlot';
import beeAvatar from '../../../../channels/web/frontend/src/assets/bee-static.png';
import type { ComponentProps } from 'react';
import { editingDocId, receiptsFromExecutions } from '../features/clouddoc/receipts';

export type ComposerProps = ComponentProps<typeof InputArea>;

export function ChatStrip({ composer, visible, titleOf, onHide, onShow, unreadReceipts, onLocate, canLocate, onHistory, onUserAnswer }: {
  composer: ComposerProps;
  /** Answering an authorization prompt. The strip has its own composer, so it needs
   *  its own slot to put the prompt in: a write started here could otherwise never be
   *  approved -- the input greyed out and told the person to deal with the item above,
   *  and there was no item above, because the only slot in the app was the main chat's.
   *  Same handler as that one; one prompt, wherever the person happens to be. */
  onUserAnswer: (requestId: string, answers: Parameters<ComponentProps<typeof InteractionSlot>['onSubmit']>[1], source?: string) => Promise<boolean>;
  visible: boolean;
  /** 聚焦标签页的文档名，作为回落值。 */
  /** doc_id -> 标题，用来给"正在编辑"标签取模型实际在动的那篇。 */
  titleOf?: (docId: string) => string;
  /** Whether locate can actually act on this receipt. The strip has neither
   *  the row nor the document's kind, so the workbench answers for it -- one
   *  predicate, not a second opinion. */
  canLocate?: (docId: string, receiptId: string) => boolean;
  onHide: () => void;
  onShow: () => void;
  unreadReceipts: number;
  onLocate: (docId: string, receiptId: string) => void;
  onHistory: () => void;
}) {
  const { t } = useTranslation();
  const sid = useChatStore((s) => s.activeSessionId) ?? '';
  const messages = useChatStore((s) => s.runtimes[sid]?.messages);
  const executions = useChatStore((s) => s.runtimes[sid]?.toolExecutions);
  const last = useMemo(() => {
    const list = messages ?? [];
    for (let i = list.length - 1; i >= 0; i--) if (list[i].role === 'assistant' && list[i].content) return list[i];
    return null;
  }, [messages]);
  // Present tense, and only that: the line appears while a write is in flight and
  // names the document that write is aimed at. It used to fall back to the
  // focused tab, so it read "editing: <whatever you have open>" when nothing was
  // happening at all -- a claim about the agent's behaviour that was false most
  // of the time it was on screen.
  const workingTitle = useMemo(() => {
    const docId = editingDocId(executions ? executions.values() : undefined);
    if (!docId) return '';
    return (titleOf ? titleOf(docId) : '') || docId;
  }, [executions, titleOf]);
  const lastReceipt = useMemo(() => {
    const all = receiptsFromExecutions(executions ? executions.values() : undefined);
    return all.length ? all[all.length - 1] : null;
  }, [executions]);

  if (!visible) {
    return (
      <button type="button" className="doc-workbench__chat-pill" onClick={onShow} data-testid="doc-workbench-chat-pill">
        <MessageSquare size={14} /> {t('docs.workbench.chat')}
        {unreadReceipts > 0 && <span className="doc-workbench__tab-dot" style={{ background: 'var(--color-conversation-unread)' }} />}
        {unreadReceipts > 0 && <span className="text-text-muted">{t('docs.workbench.newReceipts', { n: unreadReceipts })}</span>}
        <ChevronUp size={14} />
      </button>
    );
  }
  return (
    <div className="doc-workbench__chat" data-testid="doc-workbench-chat">
      <div className="doc-workbench__chat-last">
        <img src={beeAvatar} className="doc-workbench__chat-avatar" alt="jiuwen" />
        {workingTitle && (
          <span className="doc-workbench__chat-doc" title={workingTitle} data-testid="doc-workbench-chat-doc">
            {t('docs.workbench.editingDoc')}{workingTitle}
          </span>
        )}
        <span className="doc-workbench__chat-last-text" data-testid="doc-workbench-chat-last">{last?.content ?? t('docs.workbench.noReplyYet')}</span>
        {lastReceipt && (
          <span className="inline-flex items-center gap-2" data-testid="doc-workbench-chat-receipt">
            <span className="font-mono text-[11px] text-text-muted">{t('docs.workbench.receipt')} {lastReceipt.receiptId.slice(0, 8)}</span>
            {(!canLocate || canLocate(lastReceipt.docId, lastReceipt.receiptId)) && (
              <a className="text-xs text-text-link hover:underline" onClick={() => onLocate(lastReceipt.docId, lastReceipt.receiptId)}>{t('docs.workbench.locate')}</a>
            )}
          </span>
        )}
        <a className="text-xs text-text-link hover:underline" onClick={onHistory}>{t('docs.workbench.history')}</a>
        <div className="h-4 w-px bg-border" />
        <button type="button" className="doc-workbench__icon-btn" style={{ width: 24, height: 24 }} onClick={onHide} title={t('docs.workbench.hideChat')} data-testid="doc-workbench-chat-hide"><Minus size={16} /></button>
      </div>

      <InteractionSlot onSubmit={onUserAnswer} />
      <InputArea {...composer} />
    </div>
  );
}
