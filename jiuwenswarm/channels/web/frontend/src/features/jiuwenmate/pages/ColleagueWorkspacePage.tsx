import {
  ArrowLeft,
  CalendarDays,
  ClipboardList,
  ChevronDown,
  CirclePlus,
  Folder,
  FileText,
  PanelLeftOpen,
  Pin,
  PinOff,
  PencilLine,
  Plus,
  Trash2,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../../../components/MarkdownRenderer';
import { FilePreview } from '../../../components/ArtifactsPanel/FilePreview';
import documentIcon from '../../../assets/jiuwenmate/icons/document.svg';
import featureChannel from '../../../assets/jiuwenmate/illustrations/feature-channel.png';
import featureChat from '../../../assets/jiuwenmate/illustrations/feature-chat.png';
import featureSchedule from '../../../assets/jiuwenmate/illustrations/feature-schedule.png';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { ConversationComposer } from '../components/ConversationComposer';
import {
  chatPhase,
  errorMessage,
  formatSharedFileSize,
  isPending,
  latestSharedFiles,
  sharedFileKey,
  type JiuwenMateChatRequest,
  type JiuwenMateConversation,
  type JiuwenMateModelCatalog,
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
  clearManagementChannel,
  createWorkspaceState,
  openChannelTrace,
  selectChannel,
  selectManagementChannel,
  selectRecordTab,
  selectTraceRow,
  setRecordsCollapsed,
  setTraceMetric,
  toggleTraceFilter,
  toggleTraceTurn,
  mergeWorkspaceConversations,
  type JiuwenMateRecordTab,
  type JiuwenMateWorkspaceState,
} from '../workspaceState';
import { ChannelTracePage } from './ChannelTracePage';

export interface JiuwenMateWorkspaceIntent {
  recordTab: JiuwenMateRecordTab;
  channelId: string | null;
  trace: boolean;
}

interface ColleagueWorkspacePageProps {
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
  modelCatalog?: JiuwenMateModelCatalog;
  onRenameColleague: (agentId: string, name: string) => Promise<void>;
}

const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
type RightPanel = 'management' | 'artifacts' | null;

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
  modelCatalog = EMPTY_MODEL_CATALOG,
  onRenameColleague,
}: ColleagueWorkspacePageProps) {
  const { t } = useTranslation();
  const colleague = colleagues.find((item) => item.id === selectedColleagueId) ?? colleagues[0];
  const [state, setState] = useState(() => initialState(colleague, intent));
  const channelId = state.managementChannelId ?? state.selectedChannelId;
  const [board, setBoard] = useState(false);
  const [rightPanel, setRightPanel] = useState<RightPanel>(null);
  const [managementTab, setManagementTab] = useState<Exclude<JiuwenMateRecordTab, 'conversations'>>('channels');
  const [managementSearch, setManagementSearch] = useState('');
  const [selectedArtifact, setSelectedArtifact] = useState<JiuwenMateSharedFile | null>(null);
  const [scheduleId, setScheduleId] = useState<string | null>(null);
  const [scheduleEditor, setScheduleEditor] = useState<JiuwenMateSchedule | undefined>(undefined);
  const [draft, setDraft] = useState('');
  // A conversation id not used yet; it becomes a listed conversation when its first message is sent.
  const [freshId, setFreshId] = useState(newId);
  const [picked, setPicked] = useState<{ agentId: string; conversationId: string } | null>(null);
  const [allRecords, setAllRecords] = useState<Array<JiuwenMateConversation & { agentId: string }>>([]);
  const [allRecordsError, setAllRecordsError] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const cancelRename = useRef(false);
  const agentId = colleague?.id ?? '';
  const conversationList = useConversations(chat, agentId, connectionEpoch);
  const records = mergeWorkspaceConversations(
    allRecords,
    agentId,
    conversationList.conversations,
    conversationList.loaded && !conversationList.error,
  );
  const activeConversationId =
    picked?.agentId === agentId ? picked.conversationId : conversationList.loaded ? (records.find((item) => item.agentId === agentId)?.id ?? freshId) : '';
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
    let cancelled = false;
    setAllRecordsError(null);
    Promise.all(
      colleagues.map(async (item) =>
        chat.listConversations(item.id).then(
          (conversations) => conversations.map((conversation) => ({ ...conversation, agentId: item.id })),
          (reason: unknown) => {
            if (!cancelled) setAllRecordsError(errorMessage(reason));
            return [];
          },
        ),
      ),
    ).then((lists) => {
        if (cancelled) return;
        setAllRecords(lists.flat().sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)));
      });
    return () => {
      cancelled = true;
    };
  }, [chat, colleagues, connectionEpoch]);

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
    if (!channelApi || !agentId || !channelId) {
      setChannelMessages([]);
      setTraceRows([]);
      setTraceStartedAt('');
      return undefined;
    }
    let cancelled = false;
    const groupId = channelId;
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
  }, [channelApi, agentId, channelId]);

  if (!colleague) return null;

  const selectedChannel = channels.find((item) => item.id === channelId) ?? null;
  const artifacts = latestSharedFiles(privateChat.requests);
  const visibleArtifact = selectedArtifact
    ? artifacts.find((file) => sharedFileKey(file) === sharedFileKey(selectedArtifact)) ?? null
    : null;
  const openConversation = (conversationAgentId: string, conversationId: string) => {
    if (conversationAgentId !== colleague.id) onSelectColleague(conversationAgentId);
    setState((current) => clearManagementChannel(current));
    setPicked({ agentId: conversationAgentId, conversationId });
  };

  const createSession = () => {
    const id = newId();
    setFreshId(id);
    openConversation(colleague.id, id);
    setDraft('');
    setState((current) => selectRecordTab(current, 'conversations'));
  };

  const renameEmployee = (sessionAgentId: string, name: string) => {
    const nextName = name.trim();
    setRenamingId(null);
    const current = colleagues.find((item) => item.id === sessionAgentId);
    if (!current || !nextName || nextName === current.name) return;
    void Promise.resolve(onRenameColleague(sessionAgentId, nextName)).catch((reason: unknown) => {
      setAllRecordsError(errorMessage(reason));
    });
  };

  const deleteSession = async (sessionAgentId: string, id: string) => {
    const current = records.find((item) => item.agentId === sessionAgentId && item.id === id);
    if (!current) return;
    if (current.agentId === agentId) {
      if (!(await conversationList.remove(id))) return;
    } else {
      try {
        await chat.deleteConversation(current.agentId, id);
      } catch (reason) {
        setAllRecordsError(errorMessage(reason));
        return;
      }
    }
    setAllRecords((items) => items.filter((item) => item.agentId !== current.agentId || item.id !== id));
    setFreshId(newId());
    if (picked?.agentId === current.agentId && picked.conversationId === id) setPicked(null);
  };

  const toggleConversationPin = async (record: JiuwenMateConversation & { agentId: string }) => {
    const pinned = !record.pinned;
    let updated: JiuwenMateConversation | null;
    if (record.agentId === agentId) {
      updated = await conversationList.setPinned(record.id, pinned);
    } else {
      try {
        updated = await chat.setConversationPinned(record.agentId, record.id, pinned);
      } catch (reason) {
        setAllRecordsError(errorMessage(reason));
        return;
      }
    }
    if (!updated) return;
    setAllRecords((items) =>
      items.map((item) =>
        item.agentId === record.agentId && item.id === record.id
          ? { ...updated, agentId: item.agentId, lastMessage: item.lastMessage }
          : item,
      ),
    );
  };

  const sendDraft = async () => {
    const text = draft.trim();
    if (!text || privateChat.sending || !activeConversationId) return;
    const conversation = await privateChat.send(text);
    if (!conversation) return;
    setDraft('');
    conversationList.upsert(conversation);
    setAllRecords((items) => [
      { ...conversation, agentId },
      ...items.filter((item) => item.agentId !== agentId || item.id !== conversation.id),
    ]);
  };

  const running = privateChat.requests.find((item) => chatPhase(item) === 'running') ?? null;

  const composer = (
    <>
      {privateChat.error || conversationList.error || allRecordsError ? (
        <p className="jiuwenmate-chat-error" role="alert" data-testid="jiuwenmate-chat-error">
          {t('jiuwenmate.chat.failed', { reason: privateChat.error ?? conversationList.error ?? allRecordsError })}
        </p>
      ) : null}
      <ConversationComposer
        value={draft}
        onChange={setDraft}
        onSend={() => void sendDraft()}
        sending={privateChat.sending}
        onStop={running ? () => void privateChat.abort(running.requestId, 'stop') : undefined}
        stopping={running?.control?.status === 'accepted'}
        modelCatalog={modelCatalog}
        modelId={colleague?.capabilities.model ?? ''}
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

  const renderChatPanelButtons = () => (
    <div className="jiuwenmate-main-header__actions">
      <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.artifacts')} aria-pressed={rightPanel === 'artifacts'} data-testid="jiuwenmate-open-artifacts-panel" onClick={() => setRightPanel((current) => current === 'artifacts' ? null : 'artifacts')}>
        <Folder aria-hidden />
      </button>
      <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.management')} aria-pressed={rightPanel === 'management'} data-testid="jiuwenmate-open-management-panel" onClick={() => setRightPanel((current) => current === 'management' ? null : 'management')}>
        <ClipboardList aria-hidden />
      </button>
    </div>
  );

  const renderRecordList = () => {
    if (state.recordTab === 'conversations') {
      const pinnedRecords = records.filter((record) => record.pinned);
      const unpinnedRecords = records.filter((record) => !record.pinned);
      const renderConversation = (record: (typeof records)[number]) => (
        <div
          key={`${record.agentId}:${record.id}`}
          className="jiuwenmate-records__item jiuwenmate-records__item--conversation"
          aria-current={record.agentId === agentId && record.id === activeConversationId ? 'true' : undefined}
          data-testid="jiuwenmate-conversation-record"
        >
          {renamingId === `${record.agentId}:${record.id}` ? (
            <div className="jiuwenmate-records__conversation">
              <ColleagueAvatar colleague={colleagues.find((item) => item.id === record.agentId) ?? colleague} size="medium" />
              <span className="jiuwenmate-records__conversation-copy">
                <input
                  autoFocus
                  defaultValue={colleagues.find((item) => item.id === record.agentId)?.name ?? ''}
                  aria-label={t('jiuwenmate.workspace.rename')}
                  onMouseDown={(event) => event.stopPropagation()}
                  onBlur={(event) => {
                    if (cancelRename.current) {
                      cancelRename.current = false;
                      setRenamingId(null);
                      return;
                    }
                    renameEmployee(record.agentId, event.target.value);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') event.currentTarget.blur();
                    if (event.key === 'Escape') {
                      cancelRename.current = true;
                      event.currentTarget.blur();
                    }
                  }}
                />
                <small>{record.lastMessage || record.title}</small>
              </span>
              <time>{new Date(record.updatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</time>
            </div>
          ) : (
            <button type="button" onClick={() => openConversation(record.agentId, record.id)}>
              <ColleagueAvatar colleague={colleagues.find((item) => item.id === record.agentId) ?? colleague} size="medium" />
              <span className="jiuwenmate-records__conversation-copy">
                <strong>{colleagues.find((item) => item.id === record.agentId)?.name ?? record.agentId}</strong>
                <small>{record.lastMessage || record.title}</small>
              </span>
              <time>{new Date(record.updatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</time>
            </button>
          )}
          <ActionMenu
            label={t('jiuwenmate.workspace.more')}
            items={[
              { key: 'rename', label: t('jiuwenmate.workspace.rename'), icon: PencilLine, onSelect: () => setRenamingId(`${record.agentId}:${record.id}`) },
              {
                key: 'pin',
                label: t(record.pinned ? 'jiuwenmate.workspace.unpinConversation' : 'jiuwenmate.workspace.pinConversation'),
                icon: record.pinned ? PinOff : Pin,
                onSelect: () => void toggleConversationPin(record),
              },
              { key: 'delete', label: t('jiuwenmate.workspace.deleteConversation'), icon: Trash2, onSelect: () => void deleteSession(record.agentId, record.id) },
            ]}
          />
        </div>
      );
      return (
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-conversation-record-list">
          {pinnedRecords.length > 0 ? (
            <>
              <header>
                <strong>{t('jiuwenmate.workspace.pinned')}</strong>
              </header>
              {pinnedRecords.map(renderConversation)}
            </>
          ) : null}
          <header>
            <strong>{t('jiuwenmate.workspace.tabs.conversations')}</strong>
            <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.newSession')} onClick={createSession}>
              <CirclePlus aria-hidden />
            </button>
          </header>
          {records.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSessions')}</p>
          ) : (
            unpinnedRecords.map(renderConversation)
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
          {channels.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noChannels')}</p>
          ) : (
            channels.map((channel) => (
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
        {colleaguePlans.length === 0 ? (
          <p className="jiuwenmate-records__empty jiuwenmate-records__empty--start">{t('jiuwenmate.workspace.noSchedules')}</p>
        ) : (
          colleaguePlans.map((plan) => (
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
        <header className="jiuwenmate-main-header jiuwenmate-chat-welcome__header">
          <strong>{colleague.name}</strong>
          {renderChatPanelButtons()}
        </header>
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
          {colleague.name}
        </strong>
        {renderChatPanelButtons()}
      </header>
      <ol
        className="jiuwenmate-session-view__body jiuwenmate-chat-messages"
        data-testid="jiuwenmate-chat-messages"
        aria-busy={privateChat.loading}
      >
        {privateChat.loading ? (
          <li className="jiuwenmate-chat-history-loading">{t('jiuwenmate.workspace.loadingHistory')}</li>
        ) : null}
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
    if (state.managementChannelId) {
      return selectedChannel ? renderChannel(selectedChannel) : (
        <section className="jiuwenmate-main-empty" data-testid="jiuwenmate-channel-main-empty">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{channelNotice || t('jiuwenmate.workspace.noChannelsHint')}</p>
        </section>
      );
    }
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
    return privateChat.loading || privateChat.requests.length > 0 ? renderSession() : renderWelcome();
  };

  return (
    <div
      className={`jiuwenmate-workspace${board || state.recordsCollapsed ? ' jiuwenmate-workspace--collapsed' : ''}${rightPanel ? ' jiuwenmate-workspace--panel-open' : ''}`}
      data-testid="jiuwenmate-colleague-workspace"
    >
      {board && onSaveSchedule && onUpdateSchedule && onSetScheduleEnabled && onDeleteSchedule && onLoadScheduleHistory ? (
        <>
          <button type="button" className="jiuwenmate-link-button" onClick={() => setBoard(false)}>
            {t('jiuwenmate.workspace.tabs.conversations')}
          </button>
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
        </>
      ) : null}
      {!board && !state.recordsCollapsed ? (
        <aside className="jiuwenmate-records" data-testid="jiuwenmate-record-panel">
          <header>
            <h2>{t('jiuwenmate.nav.messages')}</h2>
          </header>
          <div className="jiuwenmate-chat-sidebar-actions">
            <button type="button" onClick={() => setBoard(true)}>
              <CalendarDays aria-hidden />
              <span>{t('jiuwenmate.workspace.tabs.schedules')}</span>
            </button>
          </div>
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
      {!board && rightPanel ? (
        <aside className="jiuwenmate-side-panel" data-testid={`jiuwenmate-${rightPanel}-panel`}>
          {rightPanel === 'artifacts' && visibleArtifact ? (
            <header className="jiuwenmate-side-panel__preview-header">
              <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.backToArtifacts')} onClick={() => setSelectedArtifact(null)}><ArrowLeft aria-hidden /></button>
              <strong>{visibleArtifact.name}</strong>
            </header>
          ) : (
            <header className="jiuwenmate-side-panel__header">
              <h2>{t(rightPanel === 'management' ? 'jiuwenmate.workspace.management' : 'jiuwenmate.workspace.artifacts')}</h2>
              <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.closePanel')} onClick={() => { setRightPanel(null); setSelectedArtifact(null); }}>
                <X aria-hidden />
              </button>
            </header>
          )}
          {rightPanel === 'management' ? (
            <>
              <div className="jiuwenmate-side-panel__tabs" role="tablist" aria-label={t('jiuwenmate.workspace.management')}>
                {(['channels', 'schedules'] as const).map((tab) => (
                  <button key={tab} type="button" role="tab" aria-selected={managementTab === tab} onClick={() => setManagementTab(tab)}>
                    {t(`jiuwenmate.workspace.tabs.${tab}`)}
                  </button>
                ))}
              </div>
              <div className="jiuwenmate-side-panel__body">
                {managementTab === 'channels' ? (
                  <>
                    <input className="jiuwenmate-side-panel__search" value={managementSearch} onChange={(event) => setManagementSearch(event.target.value)} placeholder={t('jiuwenmate.workspace.searchKeyword')} aria-label={t('jiuwenmate.workspace.searchKeyword')} />
                    <strong className="jiuwenmate-side-panel__count">{t('jiuwenmate.workspace.channelsCount', { count: channels.length })}</strong>
                    {channelNotice ? <p className="jiuwenmate-records__empty">{channelNotice}</p> : null}
                    {!channelNotice && channels.length === 0 ? <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noChannels')}</p> : null}
                    {channels.filter((channel) => channel.name.toLocaleLowerCase().includes(managementSearch.trim().toLocaleLowerCase())).map((channel) => (
                      <button key={channel.id} type="button" className="jiuwenmate-side-panel__item" aria-current={channel.id === selectedChannel?.id && (state.managementChannelId === channel.id || state.recordTab === 'channels') ? 'true' : undefined} onClick={() => setState((current) => selectManagementChannel(current, channel.id))}>
                        <span className="jiuwenmate-letter-avatar" data-variant={channel.id}>{channel.name.slice(0, 1)}</span>
                        <span>{channel.name}</span>
                        <em className="jiuwenmate-platform-badge">{channel.platform === 'feishu' ? t('jiuwenmate.im.feishu') : channel.platform}</em>
                      </button>
                    ))}
                  </>
                ) : (
                  <>
                    <button type="button" className="jiuwenmate-side-panel__add" onClick={() => onCreateSchedule(colleague.id)}><Plus aria-hidden />{t('jiuwenmate.schedule.createTitle')}</button>
                    <input className="jiuwenmate-side-panel__search" value={managementSearch} onChange={(event) => setManagementSearch(event.target.value)} placeholder={t('jiuwenmate.workspace.searchKeyword')} aria-label={t('jiuwenmate.workspace.searchKeyword')} />
                    {colleaguePlans.length === 0 ? <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSchedules')}</p> : null}
                    {colleaguePlans.filter((plan) => plan.name.toLocaleLowerCase().includes(managementSearch.trim().toLocaleLowerCase())).map((plan) => (
                      <button key={plan.id} type="button" className="jiuwenmate-side-panel__item" aria-current={plan.id === activePlan?.id && state.recordTab === 'schedules' ? 'true' : undefined} onClick={() => { setScheduleId(plan.id); setState((current) => selectRecordTab(current, 'schedules')); }}>
                        <CalendarDays aria-hidden />
                        <span>{plan.name}</span>
                      </button>
                    ))}
                  </>
                )}
              </div>
            </>
          ) : (
              <div className={`jiuwenmate-side-panel__body${visibleArtifact ? ' jiuwenmate-side-panel__body--preview' : ''}`} data-testid="jiuwenmate-artifact-list">
              {visibleArtifact ? (
                <FilePreview artifact={{ id: visibleArtifact.outputId, name: visibleArtifact.name, downloadUrl: visibleArtifact.downloadUrl ?? undefined, size: visibleArtifact.size }} />
              ) : artifacts.length === 0 ? (
                <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noArtifacts')}</p>
              ) : artifacts.map((file) => (
                <button key={file.outputId} type="button" className="jiuwenmate-side-panel__artifact" onClick={() => setSelectedArtifact(file)}>
                  <FileText aria-hidden />
                  <span><strong>{file.name}</strong><small>{formatSharedFileSize(file.size)}</small></span>
                </button>
              ))}
            </div>
          )}
        </aside>
      ) : null}
    </div>
  );
}
