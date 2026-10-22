import { MessageSquare, Pin, PinOff, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { GroupInfo } from './client';

export function ChatDirectory({ groups, activeId, pinnedIds, pinningId, loading, hasMore, loadingMore,
  onSelect, onCreate, onPin, onMore }: {
  groups: GroupInfo[];
  activeId?: string;
  pinnedIds: ReadonlySet<string>;
  pinningId: string | null;
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onPin: (group: GroupInfo, pinned: boolean) => void;
  onMore: () => void;
}) {
  const { t } = useTranslation();
  const sections = [
    { key: 'pinned', title: t('groupChat.pinned'), items: groups.filter((item) => pinnedIds.has(item.session_id)) },
    { key: 'conversations', title: t('groupChat.conversations'),
      items: groups.filter((item) => !pinnedIds.has(item.session_id)) },
  ];
  return <aside className="group-chat-sidebar" data-testid="group-chat-sidebar" aria-label={t('groupChat.open')}>
    <h2 data-testid="group-chat-sidebar-title">{t('groupChat.open')}</h2>
    <button type="button" className="group-chat-new" onClick={onCreate} data-testid="group-chat-create">
      <Plus size={18} aria-hidden />{t('groupChat.create')}
    </button>
    <div className="group-chat-sidebar-list" data-testid="group-chat-sidebar-list">
      {loading && !groups.length && <p data-testid="group-chat-directory-loading">{t('groupChat.loading')}</p>}
      {!loading && !groups.length && <p data-testid="group-chat-sidebar-empty">{t('groupChat.noConversations')}</p>}
      {sections.map((section) => section.items.length > 0 && <section key={section.key}
        data-testid="group-chat-directory-section" data-variant={section.key}>
        <h3 data-testid="group-chat-directory-heading">{section.title}</h3>
        {section.items.map((item) => {
          const pinned = pinnedIds.has(item.session_id);
          return <div key={item.group_id} className={`group-chat-conversation-row${activeId === item.group_id ? ' is-active' : ''}`}
            data-testid="group-chat-directory-item" data-variant={item.group_id}>
            <button type="button" className="group-chat-conversation-select" disabled={item.status !== 'ready'}
              aria-current={activeId === item.group_id ? 'page' : undefined} onClick={() => onSelect(item.group_id)}
              data-testid="group-chat-select-conversation" data-variant={item.group_id}>
              <span className="group-chat-avatar"><MessageSquare size={21} aria-hidden /></span>
              <span className="group-chat-conversation-summary"><strong>{item.name}</strong>
                <span>{item.description || t('groupChat.groupSize', { count: item.members.length + 1 })}</span>
              </span>
            </button>
            <button type="button" className="group-chat-pin" disabled={pinningId !== null || item.status !== 'ready'}
              aria-label={t(pinned ? 'groupChat.unpin' : 'groupChat.pin')} aria-pressed={pinned}
              title={t(pinned ? 'groupChat.unpin' : 'groupChat.pin')} onClick={() => onPin(item, !pinned)}
              data-testid="group-chat-pin-conversation" data-variant={item.group_id}>
              {pinned ? <PinOff size={15} aria-hidden /> : <Pin size={15} aria-hidden />}
            </button>
          </div>;
        })}
      </section>)}
      {hasMore && <button type="button" className="group-chat-text-button" disabled={loadingMore}
        onClick={onMore} data-testid="group-chat-more-groups">{t('groupChat.more')}</button>}
    </div>
  </aside>;
}
