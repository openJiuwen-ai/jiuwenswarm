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
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import collapseIcon from '../../../assets/jiuwenmate/icons/collapse.svg';
import { MarkdownRenderer } from '../../../components/MarkdownRenderer';
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
import {
  chatPhase,
  errorMessage,
  formatSharedFileSize,
  isPending,
  type JiuwenMateChatRequest,
  type JiuwenMateSchedule,
  type JiuwenMateSharedFile,
} from '../jiuwenmateApi';
import { ScheduleBoard } from '../schedules/ScheduleBoard';
import { ScheduleDialog } from '../schedules/ScheduleDialog';
import { scheduleSummary } from '../schedules/state';
import type { ScheduleDraft, ScheduleRun } from '../schedules/types';
import type { JiuwenMateChannel, JiuwenMateChannelMessage, JiuwenMateColleague, JiuwenMateTraceRow } from '../types';
import { useConversations } from '../useConversations';
import { newId, usePrivateChat, type JiuwenMateChatApi } from '../usePrivateChat';
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
  chat: JiuwenMateChatApi;
  /** Changes each time the connection to the server is restored. */
  connectionEpoch: number;
  onSelectColleague: (id: string) => void;
  onCreateSchedule: (id: string) => void;
  schedules?: readonly JiuwenMateSchedule[];
  savingSchedule?: boolean;
  onSaveSchedule?: (agentId: string, draft: ScheduleDraft) => void | Promise<void>;
  onUpdateSchedule?: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetScheduleEnabled?: (planId: string, enabled: boolean) => void | Promise<void>;
  onDeleteSchedule?: (planId: string) => void | Promise<void>;
  onLoadScheduleHistory?: (planId: string) => Promise<{ plan: JiuwenMateSchedule; runs: ScheduleRun[] }>;
  channelApi?: JiuwenMateChannelApi;
}

const PROMPT_ICONS = [ClipboardList, CalendarDays, FileText];
const FEATURES = [
  { key: 'chat', image: featureChat },
  { key: 'channel', image: featureChannel },
  { key: 'schedule', image: featureSchedule },
] as const;

export interface JiuwenMateChannelApi {
  listChannels(agentId: string): Promise<readonly JiuwenMateChannel[]>;
  channelMessages(agentId: string, groupId: string): Promise<readonly JiuwenMateChannelMessage[]>;
  channelTrace(agentId: string, groupId: string): Promise<{ startedAt: string; rows: readonly JiuwenMateTraceRow[] }>;
}

function initialState(colleague: JiuwenMateColleague, intent?: JiuwenMateWorkspaceIntent | null): JiuwenMateWorkspaceState {
  let state = createWorkspaceState(colleague.id, intent?.channelId ?? null);
  if (intent) state = selectRecordTab(state, intent.recordTab);
  if (intent?.trace && intent.channelId) state = openChannelTrace(state);
  return state;
}

export function ColleagueWorkspacePage({
  colleagues,
  selectedColleagueId,
  intent,
  chat,
  connectionEpoch,
  onSelectColleague,
  onCreateSchedule,
  schedules = [],
  savingSchedule = false,
  onSaveSchedule,
  onUpdateSchedule,
  onSetScheduleEnabled,
  onDeleteSchedule,
  onLoadScheduleHistory,
  channelApi,
  ...listHandlers
}: ColleagueWorkspacePageProps) {
  const { t } = useTranslation();
  const colleague = colleagues.find((item) => item.id === selectedColleagueId) ?? colleagues[0];
  const [state, setState] = useState(() => initialState(colleague, intent));
  const [query, setQuery] = useState('');
  const [board, setBoard] = useState(false);
  const [scheduleId, setScheduleId] = useState<string | null>(null);
  const [scheduleEditor, setScheduleEditor] = useState<JiuwenMateSchedule | undefined>(undefined);
  const [draft, setDraft] = useState('');
  // A conversation id not used yet; it becomes a listed conversation when its first message is sent.
  const [freshId, setFreshId] = useState(newId);
  const [picked, setPicked] = useState<{ agentId: string; conversationId: string } | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const agentId = colleague?.id ?? '';
  const conversationList = useConversations(chat, agentId, connectionEpoch);
  const records = conversationList.conversations;
  const activeConversationId =
    picked?.agentId === agentId ? picked.conversationId : conversationList.loaded ? (records[0]?.id ?? freshId) : '';
  const colleaguePlans = schedules.filter((plan) => plan.agentId === agentId);
  const activePlan = colleaguePlans.find((plan) => plan.id === scheduleId) ?? colleaguePlans[0] ?? null;
  const chatConversationId = state.recordTab === 'schedules' && activePlan ? activePlan.conversationId : activeConversationId;
  const privateChat = usePrivateChat(chat, agentId, chatConversationId, connectionEpoch);
  const [channels, setChannels] = useState<readonly JiuwenMateChannel[]>([]);
  const [channelMessages, setChannelMessages] = useState<readonly JiuwenMateChannelMessage[]>([]);
  const [traceRows, setTraceRows] = useState<readonly JiuwenMateTraceRow[]>([]);
  const [traceStartedAt, setTraceStartedAt] = useState('');
  const [channelNotice, setChannelNotice] = useState('');

  useEffect(() => {
    if (!channelApi || !agentId) return undefined;
    let cancelled = false;
    setChannelNotice('');
    channelApi.listChannels(agentId).then((items) => {
      if (cancelled) return;
      setChannels(items);
      setState((current) => {
        if (current.selectedChannelId && items.some((item) => item.id === current.selectedChannelId)) return current;
        return items[0] ? { ...current, selectedChannelId: items[0].id } : { ...current, selectedChannelId: null };
      });
    }).catch((error: unknown) => {
      if (!cancelled) setChannelNotice(errorMessage(error) || 'failed');
    });
    return () => {
      cancelled = true;
    };
  }, [channelApi, agentId]);

  useEffect(() => {
    if (!channelApi || !agentId || !state.selectedChannelId) {
      setChannelMessages([]);
      setTraceRows([]);
      setTraceStartedAt('');
      return undefined;
    }
    let cancelled = false;
    const groupId = state.selectedChannelId;
    Promise.all([channelApi.channelMessages(agentId, groupId), channelApi.channelTrace(agentId, groupId)])
      .then(([messages, trace]) => {
        if (cancelled) return;
        setChannelMessages(messages);
        setTraceRows(trace.rows);
        setTraceStartedAt(trace.startedAt);
      })
      .catch((error: unknown) => {
        if (!cancelled) setChannelNotice(errorMessage(error) || 'failed');
      });
    return () => {
      cancelled = true;
    };
  }, [channelApi, agentId, state.selectedChannelId]);

  if (!colleague) return null;

  const selectedChannel = channels.find((item) => item.id === state.selectedChannelId) ?? null;
  const activeSession = records.find((item) => item.id === activeConversationId) ?? null;
  const keyword = query.trim();
  const visibleRecords = keyword ? records.filter((item) => item.title.includes(keyword)) : records;
  const visibleChannels = keyword ? channels.filter((item) => item.name.includes(keyword)) : channels;

  const handleSelectColleague = (id: string) => {
    const next = colleagues.find((item) => item.id === id);
    if (!next) return;
    onSelectColleague(id);
    setPicked(null);
    setBoard(false);
    setScheduleId(null);
    setChannels([]);
    setChannelMessages([]);
    setTraceRows([]);
    setState((current) => ({ ...selectColleague(current, id), selectedChannelId: null }));
  };

  const openConversation = (conversationId: string) => setPicked({ agentId: colleague.id, conversationId });

  const createSession = () => {
    const id = newId();
    setFreshId(id);
    openConversation(id);
    setDraft('');
    setState((current) => selectRecordTab(current, 'conversations'));
  };

  const renameSession = (id: string, title: string) => {
    const nextTitle = title.trim();
    setRenamingId(null);
    const current = records.find((item) => item.id === id);
    if (!nextTitle || nextTitle === current?.title) return;
    void conversationList.rename(id, nextTitle);
  };

  const deleteSession = async (id: string) => {
    if (!(await conversationList.remove(id))) return;
    setFreshId(newId());
    if (activeConversationId === id) setPicked(null);
  };

  const sendDraft = async () => {
    const text = draft.trim();
    if (!text || privateChat.sending || !activeConversationId) return;
    const conversation = await privateChat.send(text);
    if (!conversation) return;
    setDraft('');
    conversationList.upsert(conversation);
  };

  const running = privateChat.requests.find((item) => chatPhase(item) === 'running') ?? null;

  const composer = (
    <>
      {privateChat.error || conversationList.error ? (
        <p className="jiuwenmate-chat-error" role="alert" data-testid="jiuwenmate-chat-error">
          {t('jiuwenmate.chat.failed', { reason: privateChat.error ?? conversationList.error })}
        </p>
      ) : null}
      <ConversationComposer
        value={draft}
        onChange={setDraft}
        onSend={() => void sendDraft()}
        sending={privateChat.sending}
        onStop={running ? () => void privateChat.abort(running.requestId, 'stop') : undefined}
        stopping={running?.control?.status === 'accepted'}
      />
    </>
  );

  const renderRequest = (request: JiuwenMateChatRequest) => {
    const phase = chatPhase(request);
    const control = request.control;
    const awaiting = request.recovery?.decision === 'awaiting_continuation';
    const unverified = request.recovery?.unverifiedToolCalls.length ?? 0;
    const note =
      awaiting
        ? unverified > 0
          ? t('jiuwenmate.chat.recovery.awaitingUnverified', { count: unverified })
          : t('jiuwenmate.chat.recovery.awaiting')
        : control?.status === 'failed'
        ? t('jiuwenmate.chat.control.failed', { reason: control.detail ?? '' })
        : isPending(request) && control
          ? t(control.reason === 'stop' ? 'jiuwenmate.chat.control.stopping' : 'jiuwenmate.chat.control.cancelling')
          : phase === 'completed'
            ? request.replies.length === 0 && request.files.length === 0
              ? t('jiuwenmate.chat.noReply')
              : null
            : t(`jiuwenmate.chat.status.${phase}`);
    const draft = isPending(request) && request.draft?.text ? request.draft.text : null;
    return (
      <li key={request.requestId} data-testid="jiuwenmate-chat-request" data-phase={phase}>
        <p className="jiuwenmate-chat-message jiuwenmate-chat-message--user">{request.text}</p>
        {request.replies.map((reply, index) => (
          <article key={index} className="jiuwenmate-chat-message jiuwenmate-chat-message--colleague" data-testid="jiuwenmate-chat-reply">
            <ColleagueAvatar colleague={colleague} size="small" />
            <MarkdownRenderer content={reply} className="chat-text chat-markdown jiuwenmate-chat-markdown" />
          </article>
        ))}
        {request.files.map((file) => (
          <article
            key={file.outputId}
            className="jiuwenmate-chat-message jiuwenmate-chat-message--colleague"
            data-testid="jiuwenmate-chat-file"
          >
            <ColleagueAvatar colleague={colleague} size="small" />
            {renderSharedFile(file)}
          </article>
        ))}
        {draft ? (
          <article className="jiuwenmate-chat-message jiuwenmate-chat-message--colleague" data-testid="jiuwenmate-chat-draft">
            <ColleagueAvatar colleague={colleague} size="small" />
            <MarkdownRenderer content={draft} isStreaming className="chat-text chat-markdown jiuwenmate-chat-markdown" />
          </article>
        ) : null}
        {note ? (
          <small className="jiuwenmate-chat-status" data-phase={phase}>
            {request.reason ? `${note}：${request.reason}` : note}
            {phase === 'queued' && !control ? (
              <button
                type="button"
                className="jiuwenmate-chat-cancel"
                onClick={() => void privateChat.abort(request.requestId, 'cancel')}
                data-testid="jiuwenmate-chat-cancel"
              >
                {t('jiuwenmate.chat.control.cancel')}
              </button>
            ) : null}
          </small>
        ) : null}
      </li>
    );
  };

  const renderSharedFile = (file: JiuwenMateSharedFile) => (
    <div className="jiuwenmate-chat-file">
      {file.caption ? <p className="jiuwenmate-chat-file__caption">{file.caption}</p> : null}
      <div className="jiuwenmate-chat-file__card">
        <FileText aria-hidden className="jiuwenmate-chat-file__icon" />
        <div className="jiuwenmate-chat-file__meta">
          <strong>{file.name}</strong>
          <small>{formatSharedFileSize(file.size)}</small>
        </div>
        {file.downloadUrl ? (
          <a
            className="jiuwenmate-chat-file__download"
            href={file.downloadUrl}
            download={file.name}
            data-testid="jiuwenmate-chat-file-download"
          >
            {t('jiuwenmate.chat.file.download')}
          </a>
        ) : (
          <span className="jiuwenmate-chat-file__unavailable">{t('jiuwenmate.chat.file.unavailable')}</span>
        )}
      </div>
    </div>
  );

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
                aria-current={record.id === activeConversationId ? 'true' : undefined}
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
                  <button type="button" onClick={() => openConversation(record.id)}>
                    {record.title}
                  </button>
                )}
                <ActionMenu
                  label={t('jiuwenmate.workspace.more')}
                  items={[
                    { key: 'rename', label: t('jiuwenmate.workspace.rename'), icon: PencilLine, onSelect: () => setRenamingId(record.id) },
                    { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: () => void deleteSession(record.id) },
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
                  <em className="jiuwenmate-platform-badge">
                    {channel.platform === 'feishu' ? t('jiuwenmate.im.feishu') : channel.platform}
                  </em>
                </button>
              </div>
            ))
          )}
        </div>
      );
    }
    const visiblePlans = keyword ? colleaguePlans.filter((plan) => plan.name.includes(keyword)) : colleaguePlans;
    return (
      <div className="jiuwenmate-records__section" data-testid="jiuwenmate-schedule-record-list">
        <header>
          <strong>{t('jiuwenmate.workspace.tabs.schedules')}</strong>
          <button
            type="button"
            className="jiuwenmate-icon-button"
            aria-label={t('jiuwenmate.schedule.createTitle')}
            onClick={() => onCreateSchedule(colleague.id)}
          >
            <Plus aria-hidden />
          </button>
        </header>
        {visiblePlans.length === 0 ? (
          <p className="jiuwenmate-records__empty jiuwenmate-records__empty--start">{t('jiuwenmate.workspace.noSchedules')}</p>
        ) : (
          visiblePlans.map((plan) => (
            <div key={plan.id} className="jiuwenmate-records__item" aria-current={plan.id === activePlan?.id ? 'true' : undefined}>
              <button type="button" onClick={() => setScheduleId(plan.id)}>{plan.name}</button>
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  { key: 'edit', label: t('jiuwenmate.schedule.editTask'), icon: PencilLine, onSelect: () => setScheduleEditor(plan) },
                  { key: 'delete', label: t('jiuwenmate.schedule.delete'), icon: Trash2, onSelect: () => void onDeleteSchedule?.(plan.id) },
                ]}
              />
            </div>
          ))
        )}
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
        {composer}
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
          {activeSession?.title ?? t('jiuwenmate.chat.title')}
          <ChevronDown aria-hidden />
        </strong>
        <button type="button" className="jiuwenmate-link-button" onClick={createSession}>
          <CirclePlus aria-hidden />
          {t('jiuwenmate.workspace.newSession')}
        </button>
      </header>
      <ol className="jiuwenmate-session-view__body jiuwenmate-chat-messages" data-testid="jiuwenmate-chat-messages">
        {privateChat.requests.map(renderRequest)}
      </ol>
      <footer>
        {composer}
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
          rows={traceRows}
          startedAt={traceStartedAt}
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
          {channelNotice ? <p className="jiuwenmate-records__empty">{channelNotice}</p> : null}
          {!channelNotice && channelMessages.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noChannelMessages')}</p>
          ) : null}
          {channelMessages.map((message) => (
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
                {message.status === 'recalled' ? t('jiuwenmate.workspace.messageRecalled') : message.content}
              </p>
              {message.attachment ? (
                <div className="jiuwenmate-attachment-card">
                  <img src={documentIcon} alt="" aria-hidden />
                  <span>
                    <strong>
                      {message.attachment.kind === 'file'
                        ? t('jiuwenmate.workspace.attachment', { count: message.attachment.title })
                        : message.attachment.title}
                    </strong>
                    <small>{message.attachment.kind === 'file' ? t('jiuwenmate.workspace.attachmentKind') : message.attachment.kind}</small>
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
          <p>{channelNotice || t('jiuwenmate.workspace.noChannelsHint')}</p>
        </section>
      );
    }
    if (state.recordTab === 'schedules') {
      if (!activePlan) {
        return (
          <section className="jiuwenmate-main-empty" data-testid="jiuwenmate-schedule-main-empty">
            <img src={scheduleEmpty} alt="" aria-hidden />
            <p>{t('jiuwenmate.workspace.noSchedules')}</p>
            <button type="button" className="jiuwenmate-outline-button jiuwenmate-outline-button--small" onClick={() => onCreateSchedule(colleague.id)}>
              {t('jiuwenmate.schedule.createTitle')}
            </button>
          </section>
        );
      }
      return (
        <section className="jiuwenmate-session-view" data-testid="jiuwenmate-schedule-session">
          <header className="jiuwenmate-main-header">
            <strong>
              {activePlan.name}
              <ChevronDown aria-hidden />
            </strong>
            <small>{scheduleSummary(activePlan)}</small>
          </header>
          <ol className="jiuwenmate-session-view__body jiuwenmate-chat-messages">
            {privateChat.requests.map(renderRequest)}
          </ol>
          {composer}
        </section>
      );
    }
    return privateChat.requests.length > 0 ? renderSession() : renderWelcome();
  };

  return (
    <div
      className={`jiuwenmate-workspace${board || state.recordsCollapsed ? ' jiuwenmate-workspace--collapsed' : ''}`}
      data-testid="jiuwenmate-colleague-workspace"
    >
      <ColleagueList
        colleagues={colleagues}
        selectedId={colleague.id}
        schedulesSelected={board}
        onSelect={handleSelectColleague}
        {...listHandlers}
        onOpenSchedules={() => setBoard(true)}
      />
      {board && onSaveSchedule && onUpdateSchedule && onSetScheduleEnabled && onDeleteSchedule && onLoadScheduleHistory ? (
        <ScheduleBoard
          schedules={schedules}
          colleagues={colleagues}
          saving={savingSchedule}
          onCreate={onSaveSchedule}
          onUpdate={onUpdateSchedule}
          onSetEnabled={onSetScheduleEnabled}
          onDelete={onDeleteSchedule}
          onLoadHistory={onLoadScheduleHistory}
        />
      ) : null}
      {!board && !state.recordsCollapsed ? (
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
      {!board ? <main className="jiuwenmate-workspace__main">
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
      </main> : null}
      {scheduleEditor && onUpdateSchedule ? (
        <ScheduleDialog
          key={scheduleEditor.id}
          open
          plan={scheduleEditor}
          executorId={colleague.id}
          saving={savingSchedule}
          onClose={() => setScheduleEditor(undefined)}
          onSubmit={(_executor, draft) => onUpdateSchedule(scheduleEditor.id, draft)}
        />
      ) : null}
    </div>
  );
}
