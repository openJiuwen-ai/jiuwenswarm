import {
  CalendarDays,
  ChevronDown,
  CirclePlus,
  ClipboardList,
  FileText,
  PanelLeftOpen,
  PencilLine,
  Plus,
  Search,
  Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import collapseIcon from '../../../assets/jiuwenmate/icons/collapse.svg';
import documentIcon from '../../../assets/jiuwenmate/icons/document.svg';
import featureChannel from '../../../assets/jiuwenmate/illustrations/feature-channel.png';
import featureChat from '../../../assets/jiuwenmate/illustrations/feature-chat.png';
import featureSchedule from '../../../assets/jiuwenmate/illustrations/feature-schedule.png';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { ColleagueList, type ColleagueListHandlers } from '../components/ColleagueList';
import { ConversationComposer } from '../components/ConversationComposer';
import { SegmentedTabs } from '../components/SegmentedTabs';
import { JIUWENMATE_CHANNEL_MESSAGES, JIUWENMATE_CHANNELS, JIUWENMATE_TRACE_ROWS, JIUWENMATE_TRACE_STARTED_AT } from '../mockData';
import type { JiuwenMateChannel, JiuwenMateColleague } from '../types';
import {
  closeChannelTrace,
  createWorkspaceState,
  openChannelTrace,
  selectChannel,
  selectColleague,
  selectRecordTab,
  selectTraceRow,
  setRecordsCollapsed,
  setTraceMetric,
  toggleTraceFilter,
  toggleTraceTurn,
  type JiuwenMateRecordTab,
  type JiuwenMateWorkspaceState,
} from '../workspaceState';
import { ChannelTracePage } from './ChannelTracePage';

export interface JiuwenMateWorkspaceIntent {
  recordTab: JiuwenMateRecordTab;
  channelId: string | null;
  trace: boolean;
}

interface ColleagueWorkspacePageProps extends ColleagueListHandlers {
  colleagues: readonly JiuwenMateColleague[];
  selectedColleagueId: string;
  intent?: JiuwenMateWorkspaceIntent | null;
  onSelectColleague: (id: string) => void;
  onCreateSchedule: (id: string) => void;
}

interface ConversationRecord {
  id: string;
  title: string;
}

const PROMPT_ICONS = [ClipboardList, CalendarDays, FileText];
const FEATURES = [
  { key: 'chat', image: featureChat },
  { key: 'channel', image: featureChannel },
  { key: 'schedule', image: featureSchedule },
] as const;

function colleagueChannels(colleague: JiuwenMateColleague): readonly JiuwenMateChannel[] {
  return colleague.imConfigured ? JIUWENMATE_CHANNELS : [];
}

function initialState(colleague: JiuwenMateColleague, intent?: JiuwenMateWorkspaceIntent | null): JiuwenMateWorkspaceState {
  const channels = colleagueChannels(colleague);
  const channelId = channels.find((item) => item.id === intent?.channelId)?.id ?? channels[0]?.id ?? null;
  let state = createWorkspaceState(colleague.id, channelId);
  if (intent) state = selectRecordTab(state, intent.recordTab);
  if (intent?.trace && channelId) state = openChannelTrace(state);
  return state;
}

export function ColleagueWorkspacePage({
  colleagues,
  selectedColleagueId,
  intent,
  onSelectColleague,
  onCreateSchedule,
  ...listHandlers
}: ColleagueWorkspacePageProps) {
  const { t } = useTranslation();
  const colleague = colleagues.find((item) => item.id === selectedColleagueId) ?? colleagues[0];
  const [state, setState] = useState(() => initialState(colleague, intent));
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [sessions, setSessions] = useState<Record<string, ConversationRecord[]>>({});
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);

  if (!colleague) return null;

  const channels = colleagueChannels(colleague);
  const selectedChannel = channels.find((item) => item.id === state.selectedChannelId) ?? null;
  const records = sessions[colleague.id] ?? [];
  const activeSession = records.find((item) => item.id === activeSessionId) ?? null;
  const keyword = query.trim();
  const visibleRecords = keyword ? records.filter((item) => item.title.includes(keyword)) : records;
  const visibleChannels = keyword ? channels.filter((item) => item.name.includes(keyword)) : channels;

  const handleSelectColleague = (id: string) => {
    const next = colleagues.find((item) => item.id === id);
    if (!next) return;
    onSelectColleague(id);
    setActiveSessionId(null);
    setState((current) => {
      const switched = selectColleague(current, id);
      return { ...switched, selectedChannelId: colleagueChannels(next)[0]?.id ?? null };
    });
  };

  const createSession = () => {
    const record = { id: `session-${Date.now()}`, title: t('jiuwenmate.workspace.newSession') };
    setSessions((current) => ({ ...current, [colleague.id]: [record, ...(current[colleague.id] ?? [])] }));
    setActiveSessionId(record.id);
    setState((current) => selectRecordTab(current, 'conversations'));
  };

  const renameSession = (id: string, title: string) => {
    const nextTitle = title.trim();
    setRenamingId(null);
    if (!nextTitle) return;
    setSessions((current) => ({
      ...current,
      [colleague.id]: (current[colleague.id] ?? []).map((item) => (item.id === id ? { ...item, title: nextTitle } : item)),
    }));
  };

  const deleteSession = (id: string) => {
    setSessions((current) => ({ ...current, [colleague.id]: (current[colleague.id] ?? []).filter((item) => item.id !== id) }));
    if (activeSessionId === id) setActiveSessionId(null);
  };

  const tabs = [
    { value: 'conversations' as const, label: t('jiuwenmate.workspace.tabs.conversations') },
    { value: 'channels' as const, label: t('jiuwenmate.workspace.tabs.channels') },
    { value: 'schedules' as const, label: t('jiuwenmate.workspace.tabs.schedules') },
  ];

  const renderRecordList = () => {
    if (state.recordTab === 'conversations') {
      return (
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-conversation-record-list">
          <header>
            <strong>{t('jiuwenmate.workspace.sessionsCount', { count: records.length })}</strong>
            <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.newSession')} onClick={createSession}>
              <CirclePlus aria-hidden />
            </button>
          </header>
          {visibleRecords.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSessions')}</p>
          ) : (
            visibleRecords.map((record) => (
              <div
                key={record.id}
                className="jiuwenmate-records__item"
                aria-current={record.id === activeSessionId ? 'true' : undefined}
                data-testid="jiuwenmate-conversation-record"
              >
                {renamingId === record.id ? (
                  <input
                    autoFocus
                    defaultValue={record.title}
                    aria-label={t('jiuwenmate.workspace.rename')}
                    onBlur={(event) => renameSession(record.id, event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') renameSession(record.id, event.currentTarget.value);
                      if (event.key === 'Escape') setRenamingId(null);
                    }}
                  />
                ) : (
                  <button type="button" onClick={() => setActiveSessionId(record.id)}>
                    {record.title}
                  </button>
                )}
                <ActionMenu
                  label={t('jiuwenmate.workspace.more')}
                  items={[
                    { key: 'rename', label: t('jiuwenmate.workspace.rename'), icon: PencilLine, onSelect: () => setRenamingId(record.id) },
                    { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: () => deleteSession(record.id) },
                  ]}
                />
              </div>
            ))
          )}
        </div>
      );
    }
    if (state.recordTab === 'channels') {
      return (
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-channel-record-list">
          <header>
            <strong>{t('jiuwenmate.workspace.channelsCount', { count: channels.length })}</strong>
          </header>
          {visibleChannels.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noChannels')}</p>
          ) : (
            visibleChannels.map((channel) => (
              <div key={channel.id} className="jiuwenmate-records__item jiuwenmate-records__item--channel" aria-current={channel.id === selectedChannel?.id ? 'true' : undefined}>
                <button
                  type="button"
                  onClick={() => setState((current) => selectChannel(current, channel.id))}
                  data-testid="jiuwenmate-channel-record"
                  data-variant={channel.id}
                >
                  <span className="jiuwenmate-letter-avatar" data-variant={channel.id}>
                    {channel.name.slice(0, 1)}
                  </span>
                  <span>{channel.name}</span>
                  <em className="jiuwenmate-platform-badge">{t('jiuwenmate.im.feishu')}</em>
                </button>
              </div>
            ))
          )}
        </div>
      );
    }
    return (
      <div className="jiuwenmate-records__section" data-testid="jiuwenmate-schedule-record-list">
        <header>
          <strong>{t('jiuwenmate.workspace.tabs.schedules')}</strong>
          <button
            type="button"
            className="jiuwenmate-icon-button"
            aria-label={t('jiuwenmate.workspace.newSchedule')}
            onClick={() => onCreateSchedule(colleague.id)}
          >
            <Plus aria-hidden />
          </button>
        </header>
        <p className="jiuwenmate-records__empty jiuwenmate-records__empty--start">{t('jiuwenmate.workspace.noSchedules')}</p>
      </div>
    );
  };

  const renderWelcome = () => {
    const prompts =
      colleague.capabilities.starterPrompts.length > 0
        ? colleague.capabilities.starterPrompts.slice(0, 3)
        : [0, 1, 2].map((index) => t(`jiuwenmate.workspace.defaultPrompts.${index}`));
    return (
      <section className="jiuwenmate-chat-welcome" data-testid="jiuwenmate-chat-welcome">
        <div className="jiuwenmate-chat-welcome__center">
          <ColleagueAvatar colleague={colleague} size="large" />
          <h2>{colleague.name}</h2>
          <p>{colleague.description}</p>
        </div>
        <ConversationComposer value={draft} onChange={setDraft} />
        <div className="jiuwenmate-chat-welcome__suggestions">
          {prompts.map((prompt, index) => {
            const Icon = PROMPT_ICONS[index % PROMPT_ICONS.length];
            return (
              <button type="button" key={`${prompt}-${index}`} onClick={() => setDraft(prompt)}>
                <Icon aria-hidden />
                {prompt}
              </button>
            );
          })}
        </div>
        <div className="jiuwenmate-chat-welcome__features">
          <h3>{t('jiuwenmate.workspace.featuresTitle')}</h3>
          <div>
            {FEATURES.map((feature) => (
              <article key={feature.key}>
                <img src={feature.image} alt="" aria-hidden />
                <strong>{t(`jiuwenmate.workspace.features.${feature.key}.title`)}</strong>
                <p>{t(`jiuwenmate.workspace.features.${feature.key}.description`)}</p>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  };

  const renderSession = () => (
    <section className="jiuwenmate-session-view" data-testid="jiuwenmate-session-view">
      <header className="jiuwenmate-main-header">
        <strong>
          {activeSession?.title}
          <ChevronDown aria-hidden />
        </strong>
        <button type="button" className="jiuwenmate-link-button" onClick={createSession}>
          <CirclePlus aria-hidden />
          {t('jiuwenmate.workspace.newSession')}
        </button>
      </header>
      <div className="jiuwenmate-session-view__body" />
      <footer>
        <ConversationComposer value={draft} onChange={setDraft} />
        <small>{t('jiuwenmate.workspace.aiDisclaimer')}</small>
      </footer>
    </section>
  );

  const renderChannel = (channel: JiuwenMateChannel) => (
    <section className="jiuwenmate-channel-view" data-testid="jiuwenmate-channel-view">
      <header className="jiuwenmate-main-header">
        <strong>
          {channel.name}
          <ChevronDown aria-hidden />
        </strong>
        {!state.traceOpen ? (
          <button
            type="button"
            className="jiuwenmate-link-button"
            onClick={() => setState((current) => openChannelTrace(current))}
            data-testid="jiuwenmate-view-trace-button"
          >
            <ClipboardList aria-hidden />
            {t('jiuwenmate.workspace.viewTrace')}
          </button>
        ) : null}
      </header>
      {state.traceOpen ? (
        <ChannelTracePage
          rows={JIUWENMATE_TRACE_ROWS}
          startedAt={JIUWENMATE_TRACE_STARTED_AT}
          metric={state.traceMetric}
          filter={state.traceFilter}
          expandedTurns={state.expandedTurns}
          selectedRowId={state.selectedTraceRowId}
          onMetricChange={(metric) => setState((current) => setTraceMetric(current, metric))}
          onToggleFilter={(filter) => setState((current) => toggleTraceFilter(current, filter))}
          onToggleTurn={(turn) => setState((current) => toggleTraceTurn(current, turn))}
          onSelectRow={(rowId) => setState((current) => selectTraceRow(current, rowId))}
          onBack={() => setState((current) => closeChannelTrace(current))}
        />
      ) : (
        <div className="jiuwenmate-channel-view__messages">
          {JIUWENMATE_CHANNEL_MESSAGES.map((message) => (
            <article key={message.id} data-testid="jiuwenmate-channel-message" data-variant={message.id}>
              <header>
                {message.fromColleague ? (
                  <ColleagueAvatar colleague={colleague} size="small" />
                ) : (
                  <span className="jiuwenmate-letter-avatar jiuwenmate-letter-avatar--round" data-variant={message.sender}>
                    {message.sender.slice(0, 1)}
                  </span>
                )}
                <strong>{message.fromColleague ? colleague.name : message.sender}</strong>
              </header>
              <p>
                {message.mentionColleague ? <span className="jiuwenmate-mention">@{colleague.name}</span> : null}
                {message.mentionColleague ? ' ' : null}
                {message.content}
              </p>
              {message.attachment ? (
                <div className="jiuwenmate-attachment-card">
                  <img src={documentIcon} alt="" aria-hidden />
                  <span>
                    <strong>{message.attachment.title}</strong>
                    <small>{message.attachment.kind}</small>
                  </span>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );

  const renderMain = () => {
    if (state.recordTab === 'channels') {
      if (selectedChannel) return renderChannel(selectedChannel);
      return (
        <section className="jiuwenmate-main-empty" data-testid="jiuwenmate-channel-main-empty">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{t('jiuwenmate.workspace.noChannelsHint')}</p>
        </section>
      );
    }
    if (state.recordTab === 'schedules') {
      return (
        <section className="jiuwenmate-main-empty" data-testid="jiuwenmate-schedule-main-empty">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{t('jiuwenmate.workspace.noSchedules')}</p>
          <button type="button" className="jiuwenmate-outline-button jiuwenmate-outline-button--small" onClick={() => onCreateSchedule(colleague.id)}>
            {t('jiuwenmate.workspace.newSchedule')}
          </button>
        </section>
      );
    }
    return activeSession ? renderSession() : renderWelcome();
  };

  return (
    <div
      className={`jiuwenmate-workspace${state.recordsCollapsed ? ' jiuwenmate-workspace--collapsed' : ''}`}
      data-testid="jiuwenmate-colleague-workspace"
    >
      <ColleagueList
        colleagues={colleagues}
        selectedId={colleague.id}
        onSelect={handleSelectColleague}
        {...listHandlers}
        onOpenSchedules={() => setState((current) => selectRecordTab(current, 'schedules'))}
      />
      {!state.recordsCollapsed ? (
        <aside className="jiuwenmate-records" data-testid="jiuwenmate-record-panel">
          <header>
            <h2>{colleague.name}</h2>
            <button type="button" className="jiuwenmate-link-button" onClick={() => listHandlers.onManage(colleague.id)}>
              {t('jiuwenmate.office.manage')}
            </button>
            <button
              type="button"
              className="jiuwenmate-icon-button"
              aria-label={t('jiuwenmate.workspace.collapseRecords')}
              onClick={() => setState((current) => setRecordsCollapsed(current, true))}
              data-testid="jiuwenmate-collapse-records"
            >
              <img src={collapseIcon} alt="" aria-hidden />
            </button>
          </header>
          <SegmentedTabs
            label={t('jiuwenmate.workspace.records')}
            tabs={tabs}
            value={state.recordTab}
            onChange={(tab: JiuwenMateRecordTab) => setState((current) => selectRecordTab(current, tab))}
          />
          <label className="jiuwenmate-search jiuwenmate-search--block">
            <Search aria-hidden />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label={t('jiuwenmate.workspace.searchKeyword')}
              placeholder={t('jiuwenmate.workspace.searchKeyword')}
            />
          </label>
          {renderRecordList()}
        </aside>
      ) : null}
      <main className="jiuwenmate-workspace__main">
        {state.recordsCollapsed ? (
          <button
            type="button"
            className="jiuwenmate-icon-button jiuwenmate-workspace__expand"
            aria-label={t('jiuwenmate.workspace.expandRecords')}
            onClick={() => setState((current) => setRecordsCollapsed(current, false))}
          >
            <PanelLeftOpen aria-hidden />
          </button>
        ) : null}
        {renderMain()}
      </main>
    </div>
  );
}
