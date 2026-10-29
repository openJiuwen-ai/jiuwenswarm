import { useState } from 'react';
import { MessageSquarePlus, MessagesSquare, MoreHorizontal, PencilLine, Pin, PinOff, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/DropdownMenu/DropdownMenu';
import type { GroupInfo } from './client';
import { conversationTime, timestampMillis } from './state';

export function ChatDirectory({ groups, activeId, pinnedIds, pinningId, loading, hasMore, loadingMore,
  onSelect, onCreate, onPin, onRename, onDelete, onMore }: {
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
  onRename: (group: GroupInfo) => void;
  onDelete: (group: GroupInfo) => void;
  onMore: () => void;
}) {
  const { t } = useTranslation();
  const [menuId, setMenuId] = useState<string | null>(null);
  const sections = [
    { key: 'pinned', title: t('groupChat.pinned'), items: groups.filter((item) => pinnedIds.has(item.session_id)) },
    { key: 'conversations', title: t('groupChat.conversations'),
      items: groups.filter((item) => !pinnedIds.has(item.session_id)) },
  ];
  return <aside className="group-chat-sidebar" data-testid="group-chat-sidebar" aria-label={t('groupChat.open')}>
    <h2 data-testid="group-chat-sidebar-title">{t('groupChat.open')}</h2>
    <button type="button" className="group-chat-new" onClick={onCreate} data-testid="group-chat-create">
      <MessageSquarePlus size={20} aria-hidden />{t('groupChat.create')}
    </button>
    <div className="group-chat-sidebar-list" data-testid="group-chat-sidebar-list">
      {loading && !groups.length && <p data-testid="group-chat-directory-loading">{t('groupChat.loading')}</p>}
      {!loading && !groups.length && <p data-testid="group-chat-sidebar-empty">{t('groupChat.noConversations')}</p>}
      {sections.map((section) => section.items.length > 0 && <section key={section.key}
        data-testid="group-chat-directory-section" data-variant={section.key}>
        <h3 data-testid="group-chat-directory-heading">{section.title}</h3>
        {section.items.map((item) => {
          const pinned = pinnedIds.has(item.session_id);
          const updatedAt = timestampMillis(item.updated_at || item.created_at);
          return <div key={item.group_id} className={`group-chat-conversation-row${activeId === item.group_id ? ' is-active' : ''}`}
            data-testid="group-chat-directory-item" data-variant={item.group_id}>
            <button type="button" className="group-chat-conversation-select" disabled={item.status !== 'ready'}
              aria-current={activeId === item.group_id ? 'page' : undefined} onClick={() => onSelect(item.group_id)}
              data-testid="group-chat-select-conversation" data-variant={item.group_id}>
              <span className="group-chat-avatar group-chat-group-avatar"><MessagesSquare size={22} aria-hidden /></span>
              <span className="group-chat-conversation-summary">
                <span className="group-chat-conversation-title">
                  <strong>{item.name}</strong>
                  {updatedAt > 0 && <time dateTime={new Date(updatedAt).toISOString()}
                    data-testid="group-chat-conversation-time">{conversationTime(updatedAt)}</time>}
                </span>
                <span>{item.description || t('groupChat.groupSize', { count: item.members.length + 1 })}</span>
              </span>
            </button>
            <DropdownMenu open={menuId === item.group_id}
              onOpenChange={(open) => setMenuId(open ? item.group_id : null)}>
              <DropdownMenuTrigger className="group-chat-pin group-chat-row-more" disabled={item.status !== 'ready'}
                aria-label={t('groupChat.moreActions')} title={t('groupChat.moreActions')}
                data-testid="group-chat-conversation-more" data-variant={item.group_id}>
                <MoreHorizontal size={16} aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent side="right" align="start" className="group-chat-row-menu"
                data-testid="group-chat-conversation-menu">
                <DropdownMenuItem icon={<PencilLine size={15} />} onSelect={() => onRename(item)}
                  data-testid="group-chat-rename-conversation" data-variant={item.group_id}>
                  {t('groupChat.rename')}
                </DropdownMenuItem>
                <DropdownMenuItem icon={pinned ? <PinOff size={15} /> : <Pin size={15} />} disabled={pinningId !== null}
                  onSelect={() => onPin(item, !pinned)} aria-pressed={pinned}
                  data-testid="group-chat-pin-conversation" data-variant={item.group_id}>
                  {t(pinned ? 'groupChat.unpin' : 'groupChat.pin')}
                </DropdownMenuItem>
                <DropdownMenuItem icon={<Trash2 size={15} />} danger onSelect={() => onDelete(item)}
                  data-testid="group-chat-delete-conversation" data-variant={item.group_id}>
                  {t('groupChat.deleteShort')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>;
        })}
      </section>)}
      {hasMore && <button type="button" className="group-chat-text-button" disabled={loadingMore}
        onClick={onMore} data-testid="group-chat-more-groups">{t('groupChat.more')}</button>}
    </div>
  </aside>;
}
