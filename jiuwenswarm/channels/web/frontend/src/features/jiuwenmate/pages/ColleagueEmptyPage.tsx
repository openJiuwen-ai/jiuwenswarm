import { MessageSquarePlus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import sleepingBee from '../../../assets/jiuwenmate/illustrations/sleeping-bee.png';
import { GroupChatWorkspace } from '../../groupChat/GroupChatWorkspace';
import { mergeChatEntries } from '../chatEntries';
import { GroupConversationRecord } from '../components/GroupConversationRecord';
import type { JiuwenMateGroupChat } from './ColleagueWorkspacePage';

interface ColleagueEmptyPageProps {
  onCreate: () => void;
  groupChat?: JiuwenMateGroupChat;
}

export function ColleagueEmptyPage({ onCreate, groupChat }: ColleagueEmptyPageProps) {
  const { t } = useTranslation();
  const directory = groupChat?.directory;
  const entries = mergeChatEntries([], directory?.groups ?? [], directory?.pinnedIds ?? new Set<string>());
  const renderGroup = (entry: (typeof entries.others)[number]) => entry.kind === 'group' ? (
    <GroupConversationRecord
      key={entry.key}
      group={entry.group}
      active={entry.group.group_id === groupChat?.activeGroupId}
      pinned={entry.pinned}
      pinDisabled={directory?.pinningId != null}
      onOpen={() => groupChat?.onSelectGroup(entry.group.group_id)}
      onRename={() => directory?.openEdit(entry.group)}
      onPin={() => directory?.pin(entry.group, !entry.pinned)}
      onDelete={() => directory?.openDelete(entry.group)}
    />
  ) : null;

  return (
    <div className={`jiuwenmate-workspace${groupChat ? '' : ' jiuwenmate-workspace--empty'}`} data-testid="jiuwenmate-colleague-empty">
      <aside className="jiuwenmate-records" data-testid="jiuwenmate-chat-sidebar">
        <header>
          <h2>{t('jiuwenmate.workspace.tabs.conversations')}</h2>
        </header>
        {groupChat ? (
          <div className="jiuwenmate-chat-sidebar-actions">
            <button type="button" onClick={groupChat.directory.openCreate} data-testid="jiuwenmate-create-group-chat">
              <MessageSquarePlus aria-hidden />
              <span>{t('groupChat.create')}</span>
            </button>
          </div>
        ) : null}
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-conversation-record-list">
          {entries.pinned.length > 0 ? (
            <>
              <header><strong>{t('jiuwenmate.workspace.pinned')}</strong></header>
              {entries.pinned.map(renderGroup)}
            </>
          ) : null}
          {entries.others.length + entries.pinned.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSessions')}</p>
          ) : entries.others.map(renderGroup)}
        </div>
      </aside>
      {groupChat?.activeGroupId ? (
        <main className="jiuwenmate-workspace__main">
          <GroupChatWorkspace key={groupChat.activeGroupId} groupId={groupChat.activeGroupId}
            directory={groupChat.directory} onBack={() => groupChat.onSelectGroup()} />
        </main>
      ) : (
      <section className="jiuwenmate-colleague-empty">
        <header className="jiuwenmate-page-header">
          <div>
            <h1>{t('jiuwenmate.colleagueEmpty.title')}</h1>
            <p>{t('jiuwenmate.colleagueEmpty.subtitle')}</p>
          </div>
          <button type="button" className="jiuwenmate-dark-button" onClick={onCreate}>
            {t('jiuwenmate.colleagueEmpty.create')}
          </button>
        </header>
        <div className="jiuwenmate-colleague-empty__body">
          <img className="jiuwenmate-colleague-empty__bee" src={sleepingBee} alt="" data-testid="jiuwenmate-colleague-empty-bee" />
          <h2>{t('jiuwenmate.colleagueEmpty.emptyTitle')}</h2>
          <button
            type="button"
            className="jiuwenmate-outline-button"
            onClick={onCreate}
            data-testid="jiuwenmate-colleague-empty-start"
          >
            {t('jiuwenmate.colleagueEmpty.goCreate')}
          </button>
        </div>
      </section>
      )}
    </div>
  );
}
