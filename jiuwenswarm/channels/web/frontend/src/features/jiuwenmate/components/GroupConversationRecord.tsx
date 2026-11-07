import { MessagesSquare, PencilLine, Pin, PinOff, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { GroupInfo } from '../../groupChat/client';
import { ActionMenu } from './ActionMenu';

/** A group chat row in the chat sidebar, styled like a colleague conversation row. */
export function GroupConversationRecord({ group, active, pinned, pinDisabled, onOpen, onRename, onPin, onDelete }: {
  group: GroupInfo;
  active: boolean;
  pinned: boolean;
  pinDisabled: boolean;
  onOpen: () => void;
  onRename: () => void;
  onPin: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const updated = group.updated_at || group.created_at;
  return (
    <div
      className="jiuwenmate-records__item jiuwenmate-records__item--conversation"
      aria-current={active ? 'true' : undefined}
      data-testid="jiuwenmate-group-record"
      data-variant={group.group_id}
    >
      <button type="button" disabled={group.status !== 'ready'} onClick={onOpen} data-testid="jiuwenmate-group-record-open">
        <span className="jiuwenmate-group-avatar" aria-hidden>
          <MessagesSquare />
        </span>
        <span className="jiuwenmate-records__conversation-copy">
          <span className="jiuwenmate-group-title">
            <strong>{group.name}</strong>
            <em className="jiuwenmate-group-tag" data-testid="jiuwenmate-group-tag">{t('groupChat.groupTag')}</em>
          </span>
          <small>{group.description || t('groupChat.groupSize', { count: group.members.length + 1 })}</small>
        </span>
        {updated > 0 ? (
          <time dateTime={new Date(updated * 1000).toISOString()}>
            {new Date(updated * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
          </time>
        ) : null}
      </button>
      <ActionMenu
        label={t('jiuwenmate.workspace.more')}
        testId="jiuwenmate-group-record-menu"
        items={[
          { key: 'rename', label: t('jiuwenmate.workspace.rename'), icon: PencilLine, onSelect: onRename },
          {
            key: 'pin',
            label: t(pinned ? 'jiuwenmate.workspace.unpinConversation' : 'jiuwenmate.workspace.pinConversation'),
            icon: pinned ? PinOff : Pin,
            disabled: pinDisabled,
            onSelect: onPin,
          },
          { key: 'delete', label: t('jiuwenmate.workspace.deleteConversation'), icon: Trash2, onSelect: onDelete },
        ]}
      />
    </div>
  );
}
