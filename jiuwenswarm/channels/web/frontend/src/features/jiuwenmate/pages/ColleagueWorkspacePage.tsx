import {
  ArrowLeft,
  ClipboardList,
  ChevronDown,
  ChevronRight,
  Folder,
  FileText,
  PanelLeftOpen,
  Pin,
  PinOff,
  PencilLine,
  Plus,
  SquarePlus,
  Trash2,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../../../components/MarkdownRenderer';
import { FilePreview } from '../../../components/ArtifactsPanel/FilePreview';
import documentIcon from '../../../assets/jiuwenmate/icons/document.svg';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { GroupConversationRecord } from '../components/GroupConversationRecord';
import { GroupChatWorkspace } from '../../groupChat/GroupChatWorkspace';
import type { GroupDirectory } from '../../groupChat/useGroupDirectory';
import { mergeChatEntries } from '../chatEntries';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { ConversationComposer } from '../components/ConversationComposer';
import { GroupSharingInbox } from '../components/GroupSharingInbox';
import { SHARING_CONVERSATION_ID } from '../groupSharingApi';
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
import type {
  JiuwenMateChannel,
  JiuwenMateChannelMessage,
  JiuwenMateChannelMessagePage,
  JiuwenMateColleague,
  JiuwenMateConnection,
  JiuwenMateActivity,
  JiuwenMateTraceRow,
} from '../types';

const CHANNEL_PLATFORM_ORDER = ['feishu', 'welink', 'dingtalk'];

function channelGroups(channels: readonly JiuwenMateChannel[]): { platform: string; items: JiuwenMateChannel[] }[] {
  const buckets = new Map<string, JiuwenMateChannel[]>();
  for (const channel of channels) {
    const platform = channel.platform || 'other';
    const items = buckets.get(platform);
    if (items) items.push(channel);
    else buckets.set(platform, [channel]);
  }
  const ordered = [
    ...CHANNEL_PLATFORM_ORDER.filter((platform) => buckets.has(platform)),
    ...[...buckets.keys()].filter((platform) => !CHANNEL_PLATFORM_ORDER.includes(platform)),
  ];
  return ordered.map((platform) => ({ platform, items: buckets.get(platform) ?? [] }));
}
import { useConversations } from '../useConversations';
import { newId, usePrivateChat, type JiuwenMateChatApi } from '../usePrivateChat';
import {
  channelRecordView,
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

/** Group chats shown alongside colleague conversations in the chat sidebar. */
export interface JiuwenMateGroupChat {
  directory: GroupDirectory;
  activeGroupId?: string;
  onSelectGroup: (groupId?: string) => void;
}

interface ColleagueWorkspacePageProps {
  groupChat?: JiuwenMateGroupChat;
  colleagues: readonly JiuwenMateColleague[];
  selectedColleagueId: string;
  intent?: JiuwenMateWorkspaceIntent | null;
  chat: JiuwenMateChatApi;
  /** Changes each time the connection to the server is restored. */
  connectionEpoch: number;
  onSelectColleague: (id: string) => void;
  onCreateSchedule: (id: string) => void;
  schedules?: readonly JiuwenMateSchedule[];
  activity?: JiuwenMateActivity | null;
  savingSchedule?: boolean;
  onSaveSchedule?: (agentId: string, draft: ScheduleDraft) => void | Promise<void>;
  onUpdateSchedule?: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetScheduleEnabled?: (planId: string, enabled: boolean) => void | Promise<void>;
  onDeleteSchedule?: (planId: string) => void | Promise<void>;
  onLoadScheduleHistory?: (planId: string) => Promise<{ plan: JiuwenMateSchedule; runs: ScheduleRun[] }>;
  channelApi?: JiuwenMateChannelApi;
  modelCatalog?: JiuwenMateModelCatalog;
  onRenameColleague: (agentId: string, name: string) => Promise<void>;
  onSavePermissionProfile?: (agentId: string, profile: 'default' | 'automatic' | 'full_access') => Promise<void>;
  savingPermissionProfile?: boolean;
}

const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
type RightPanel = 'management' | 'artifacts' | null;
const COMPACT_WORKSPACE_MEDIA = '(max-width: 760px)';

function compactWorkspace() {
  return typeof window !== 'undefined' && window.matchMedia(COMPACT_WORKSPACE_MEDIA).matches;
}

export interface JiuwenMateChannelApi {
  listChannels(agentId: string): Promise<readonly JiuwenMateChannel[]>;
  channelMessages(agentId: string, groupId: string, cursor?: string): Promise<JiuwenMateChannelMessagePage>;
  channelTrace(agentId: string, groupId: string): Promise<{ startedAt: string; rows: readonly JiuwenMateTraceRow[] }>;
}

function initialState(colleague: JiuwenMateColleague, intent?: JiuwenMateWorkspaceIntent | null): JiuwenMateWorkspaceState {
  let state = createWorkspaceState(colleague.id, intent?.channelId ?? null);
  if (intent) state = selectRecordTab(state, intent.recordTab);
  if (intent?.trace && intent.channelId) state = openChannelTrace(state);
  if (compactWorkspace()) state = setRecordsCollapsed(state, true);
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
  activity = null,
  savingSchedule = false,
  onSaveSchedule,
  onUpdateSchedule,
  onSetScheduleEnabled,
  onDeleteSchedule,
  onLoadScheduleHistory,
  channelApi,
  modelCatalog = EMPTY_MODEL_CATALOG,
  onRenameColleague,
  onSavePermissionProfile,
  savingPermissionProfile = false,
  groupChat,
}: ColleagueWorkspacePageProps) {
  const { t } = useTranslation();
  const activeGroupId = groupChat?.activeGroupId;
  const leaveGroup = () => { if (activeGroupId) groupChat?.onSelectGroup(); };
  const platformLabel = (platform: string) => {
    if (platform === 'feishu') return t('jiuwenmate.im.feishu');
    if (platform === 'welink') return t('jiuwenmate.im.welink');
    if (platform === 'dingtalk') return t('jiuwenmate.im.dingtalk');
    return platform;
  };
  const colleague = colleagues.find((item) => item.id === selectedColleagueId) ?? colleagues[0];
  const colleagueId = colleague?.id ?? '';
  // Chats, files and group records follow the chosen connection; schedules stay with the Web agent.
  const [workspaceAgentId, setWorkspaceAgentId] = useState(colleagueId);
  // A Web result found while the user works in an IM workspace; opened only when the user asks.
  const [webResult, setWebResult] = useState<{ agentId: string; conversationId: string } | null>(null);
  const connections = colleague?.connections ?? [];
  const workspace = connections.find((item) => item.agentId === workspaceAgentId) ?? connections[0] ?? null;
  const channelSourceAgentKey = [...new Set([colleagueId, ...connections.map((item) => item.agentId)].filter(Boolean))]
    .sort()
    .join('\u0000');
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
  const [permissionActionId, setPermissionActionId] = useState<string | null>(null);
  const [permissionProfileError, setPermissionProfileError] = useState<string | null>(null);
  const cancelRename = useRef(false);
  const agentId = workspace?.agentId ?? colleagueId;
  const conversationList = useConversations(chat, agentId, connectionEpoch);
  const records = mergeWorkspaceConversations(
    allRecords,
    agentId,
    conversationList.conversations,
    conversationList.loaded && !conversationList.error,
  );
  const activeConversationId =
    picked?.agentId === agentId ? picked.conversationId : conversationList.loaded ? (records.find((item) => item.agentId === agentId)?.id ?? freshId) : '';
  const colleaguePlans = schedules.filter((plan) => plan.agentId === colleagueId);
  const activePlan = colleaguePlans.find((plan) => plan.id === scheduleId) ?? colleaguePlans[0] ?? null;
  const chatConversationId = state.recordTab === 'schedules' && activePlan ? activePlan.conversationId : activeConversationId;
  const chatAgentId = state.recordTab === 'schedules' && activePlan ? colleagueId : agentId;
  const webWorkspace = agentId === colleagueId && (!workspace || workspace.channel === 'web');
  const sharingConversation = webWorkspace && chatConversationId === SHARING_CONVERSATION_ID;
  const privateChat = usePrivateChat(chat, sharingConversation ? '' : chatAgentId, chatConversationId, connectionEpoch);
  const messageListRef = useRef<HTMLOListElement | null>(null);
  const boardRef = useRef(board);
  const agentIdRef = useRef(agentId);
  const conversationIdRef = useRef(chatConversationId);
  const colleaguesRef = useRef(colleagues);
  const onSelectColleagueRef = useRef(onSelectColleague);
  const reloadChatRef = useRef(privateChat.reload);
  const reloadListRef = useRef(conversationList.reload);
  const scheduleEditorRef = useRef(scheduleEditor);
  const refreshTalkRef = useRef<() => void>(() => undefined);
  boardRef.current = board;
  agentIdRef.current = agentId;
  conversationIdRef.current = chatConversationId;
  colleaguesRef.current = colleagues;
  onSelectColleagueRef.current = onSelectColleague;
  reloadChatRef.current = privateChat.reload;
  reloadListRef.current = conversationList.reload;
  scheduleEditorRef.current = scheduleEditor;
  const [channels, setChannels] = useState<readonly JiuwenMateChannel[]>([]);
  const selectedChannel = channels.find((item) => item.id === channelId) ?? null;
  const channelAgentId = selectedChannel?.ownerAgentId ?? agentId;
  const [channelMessages, setChannelMessages] = useState<readonly JiuwenMateChannelMessage[]>([]);
  const [channelNextCursor, setChannelNextCursor] = useState<string | null>(null);
  const [loadingOlderChannelMessages, setLoadingOlderChannelMessages] = useState(false);
  const [traceRows, setTraceRows] = useState<readonly JiuwenMateTraceRow[]>([]);
  const [traceStartedAt, setTraceStartedAt] = useState('');
  const [channelNotice, setChannelNotice] = useState('');
  const [settledChannelKey, setSettledChannelKey] = useState<string | null>(null);
  const channelKey = channelAgentId && channelId ? `${channelAgentId}:${channelId}` : null;
  const channelRecords = channelRecordView(channelKey, settledChannelKey, channelMessages.length);

  useEffect(() => {
    setWorkspaceAgentId(colleagueId);
    setWebResult(null);
  }, [colleagueId]);

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
    let active = true;
    let primed = false;
    let known = new Map<string, string>();
    let generation = 0;
    const tick = async () => {
      const token = ++generation;
      const people = colleaguesRef.current;
      if (!people.length) return;
      const lists = await Promise.all(
        people.map(async (item) =>
          chat.listConversations(item.id).then(
            (conversations) => conversations.map((conversation) => ({ ...conversation, agentId: item.id })),
            () => [] as Array<JiuwenMateConversation & { agentId: string }>,
          ),
        ),
      );
      if (!active || token !== generation) return;
      const flat = lists.flat().sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
      setAllRecords(flat);
      if (agentIdRef.current) void reloadListRef.current();
      const next = new Map(flat.map((item) => [`${item.agentId}:${item.id}`, item.updatedAt]));
      if (primed) {
        const newest = flat
          .filter((item) => {
            const previous = known.get(`${item.agentId}:${item.id}`);
            return previous === undefined || item.updatedAt > previous;
          })
          .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))[0];
        if (newest) {
          const same = newest.agentId === agentIdRef.current && newest.id === conversationIdRef.current;
          // Conversations listed for every colleague are Web conversations. An IM workspace the user
          // chose stays chosen, so later messages still go to its agent; the result is only offered.
          const imChosen = !people.some((item) => item.id === agentIdRef.current);
          if (!scheduleEditorRef.current && (!same || boardRef.current)) {
            if (imChosen) {
              setWebResult({ agentId: newest.agentId, conversationId: newest.id });
              if (!boardRef.current) void reloadChatRef.current();
            } else {
              if (newest.agentId !== agentIdRef.current) onSelectColleagueRef.current(newest.agentId);
              setWorkspaceAgentId(newest.agentId);
              setBoard(false);
              setState((current) => selectRecordTab(current, 'conversations'));
              setPicked({ agentId: newest.agentId, conversationId: newest.id });
            }
          }
          if (same) void reloadChatRef.current();
        } else if (!boardRef.current) {
          void reloadChatRef.current();
        }
      }
      primed = true;
      known = next;
    };
    refreshTalkRef.current = () => {
      void tick();
    };
    void tick();
    const timer = window.setInterval(() => {
      void tick();
    }, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [chat]);

  useEffect(() => {
    return chat.onStream((event) => {
      if (event.kind === 'ended') refreshTalkRef.current();
    });
  }, [chat]);

  useEffect(() => {
    const list = messageListRef.current;
    if (!list || board) return;
    list.scrollTop = list.scrollHeight;
  }, [board, chatConversationId, privateChat.requests.length]);

  useEffect(() => {
    if (!channelApi || !colleagueId) return undefined;
    let cancelled = false;
    setChannelNotice('');
    const sourceAgentIds = channelSourceAgentKey.split('\u0000').filter(Boolean);
    Promise.allSettled(sourceAgentIds.map(async (sourceAgentId) => ({
      sourceAgentId,
      items: await channelApi.listChannels(sourceAgentId),
    }))).then((results) => {
      if (cancelled) return;
      const fulfilled = results.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []);
      if (fulfilled.length === 0) {
        const failure = results.find((result) => result.status === 'rejected');
        setChannels([]);
        setChannelNotice(failure?.status === 'rejected' ? errorMessage(failure.reason) || 'failed' : 'failed');
        return;
      }
      const unique = new Map<string, JiuwenMateChannel>();
      for (const { sourceAgentId, items } of fulfilled) {
        for (const item of items) {
          const key = `${item.platform}:${item.id}`;
          if (!unique.has(key)) unique.set(key, { ...item, ownerAgentId: sourceAgentId });
        }
      }
      const items = [...unique.values()];
      setChannels(items);
      setState((current) => {
        if (current.selectedChannelId && items.some((item) => item.id === current.selectedChannelId)) return current;
        return items[0] ? { ...current, selectedChannelId: items[0].id } : { ...current, selectedChannelId: null };
      });
    });
    return () => {
      cancelled = true;
    };
  }, [channelApi, colleagueId, channelSourceAgentKey]);

  useEffect(() => {
    if (!channelApi || !channelAgentId || !channelId) {
      setChannelMessages([]);
      setChannelNextCursor(null);
      setSettledChannelKey(null);
      setTraceRows([]);
      setTraceStartedAt('');
      return undefined;
    }
    let cancelled = false;
    const groupId = channelId;
    const requestKey = `${channelAgentId}:${groupId}`;
    setChannelNotice('');
    setChannelMessages([]);
    setChannelNextCursor(null);
    channelApi
      .channelMessages(channelAgentId, groupId)
      .then((page) => {
        if (cancelled) return;
        setChannelMessages(page.messages);
        setChannelNextCursor(page.nextCursor);
        setSettledChannelKey(requestKey);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setChannelMessages([]);
        setSettledChannelKey(requestKey);
        setChannelNotice(errorMessage(error) || 'failed');
      });
    channelApi
      .channelTrace(channelAgentId, groupId)
      .then((trace) => {
        if (cancelled) return;
        setTraceRows(trace.rows);
        setTraceStartedAt(trace.startedAt);
      })
      .catch(() => {
        if (cancelled) return;
        setTraceRows([]);
        setTraceStartedAt('');
      });
    return () => {
      cancelled = true;
    };
  }, [channelApi, channelAgentId, channelId]);

  if (!colleague) return null;

  const artifacts = latestSharedFiles(privateChat.requests);

  const loadOlderChannelMessages = async () => {
    if (!channelApi || !channelAgentId || !channelId || !channelNextCursor || loadingOlderChannelMessages) return;
    setLoadingOlderChannelMessages(true);
    try {
      const page = await channelApi.channelMessages(channelAgentId, channelId, channelNextCursor);
      setChannelMessages((current) => {
        const known = new Set(current.map((item) => item.id));
        return [...page.messages.filter((item) => !known.has(item.id)), ...current];
      });
      setChannelNextCursor(page.nextCursor);
    } catch (error: unknown) {
      setChannelNotice(errorMessage(error) || 'failed');
    } finally {
      setLoadingOlderChannelMessages(false);
    }
  };
  const visibleArtifact = selectedArtifact
    ? artifacts.find((file) => sharedFileKey(file) === sharedFileKey(selectedArtifact)) ?? null
    : null;
  const ownerOf = (id: string) =>
    colleagues.find((item) => item.id === id || item.connections.some((connection) => connection.agentId === id));
  const openConversation = (conversationAgentId: string, conversationId: string) => {
    const owner = ownerOf(conversationAgentId);
    if (owner && owner.id !== colleague.id) onSelectColleague(owner.id);
    setWorkspaceAgentId(conversationAgentId);
    setWebResult(null);
    setBoard(false);
    leaveGroup();
    setState((current) => {
      const next = clearManagementChannel(current);
      return compactWorkspace() ? setRecordsCollapsed(next, true) : next;
    });
    setPicked({ agentId: conversationAgentId, conversationId });
  };

  const renameEmployee = (sessionAgentId: string, name: string) => {
    const nextName = name.trim();
    setRenamingId(null);
    const current = ownerOf(sessionAgentId);
    if (!current || !nextName || nextName === current.name) return;
    void Promise.resolve(onRenameColleague(current.id, nextName)).catch((reason: unknown) => {
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
    if (!text || privateChat.sending || !activeConversationId || sharingConversation) return;
    const conversation = await privateChat.send(text);
    if (!conversation) return;
    setDraft('');
    conversationList.upsert(conversation);
    setAllRecords((items) => [
      { ...conversation, agentId },
      ...items.filter((item) => item.agentId !== agentId || item.id !== conversation.id),
    ]);
  };

  const savePermissionProfile = async (profile: 'default' | 'automatic' | 'full_access') => {
    if (!onSavePermissionProfile || savingPermissionProfile || profile === (colleague.permissionProfile ?? 'default')) return;
    if (profile === 'full_access' && !window.confirm(t('jiuwenmate.profile.permissions.fullAccessConfirm'))) return;
    setPermissionProfileError(null);
    try {
      await onSavePermissionProfile(colleague.id, profile);
    } catch (error) {
      setPermissionProfileError(errorMessage(error) || t('jiuwenmate.chat.failed', { reason: '' }));
    }
  };

  const running = privateChat.requests.find((item) => chatPhase(item) === 'running') ?? null;

  const sharingInbox = webWorkspace && chat.sharing ? (
    <GroupSharingInbox
      key={colleagueId}
      api={chat.sharing}
      configId={colleague.configId}
      colleague={colleague}
      conversationId={chatConversationId}
      connectionEpoch={connectionEpoch}
      onOpen={() => {
        openConversation(colleagueId, SHARING_CONVERSATION_ID);
        setState((current) => selectRecordTab(current, 'conversations'));
      }}
    />
  ) : null;

  const composer = (
    <>
      {privateChat.error || conversationList.error || allRecordsError || permissionProfileError ? (
        <p className="jiuwenmate-chat-error" role="alert" data-testid="jiuwenmate-chat-error">
          {permissionProfileError ?? t('jiuwenmate.chat.failed', { reason: privateChat.error ?? conversationList.error ?? allRecordsError })}
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
        permissionProfile={colleague.permissionProfile ?? 'default'}
        onPermissionProfileChange={(profile) => void savePermissionProfile(profile)}
        permissionProfileSaving={savingPermissionProfile}
      />
    </>
  );

  const renderRequest = (request: JiuwenMateChatRequest) => {
    const phase = chatPhase(request);
    const approval = request.approval;
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
        {approval ? (
          <article className="jiuwenmate-chat-approval" data-testid="jiuwenmate-chat-permission-approval" data-status={approval.status}>
            <strong>{t('jiuwenmate.chat.approval.title')}</strong>
            {approval.intercepted.map((call, index) => (
              <div className="jiuwenmate-chat-approval__call" key={`${approval.approvalId}-${index}`}>
                <code>{call.toolName} · {call.toolCallId}</code>
                <pre>{call.arguments}</pre>
                {call.argumentsTruncated ? <small>{t('jiuwenmate.chat.approval.truncated')}</small> : null}
                {approval.status === 'pending' ? (
                  <div className="jiuwenmate-chat-approval__actions">
                    <button
                      type="button"
                      disabled={permissionActionId === approval.approvalId}
                      onClick={() => {
                        setPermissionActionId(approval.approvalId);
                        void privateChat.decidePermission(approval.approvalId, 'approve', call.toolCallId)
                          .finally(() => setPermissionActionId(null));
                      }}
                      data-testid="jiuwenmate-chat-permission-approve"
                    >
                      {t('jiuwenmate.chat.approval.approveOne')}
                    </button>
                  </div>
                ) : null}
              </div>
            ))}
            <small>
              {approval.status === 'pending'
                ? t('jiuwenmate.chat.approval.expires', {
                    time: approval.expiresAt ? new Date(approval.expiresAt).toLocaleTimeString() : '',
                  })
                : t(`jiuwenmate.chat.approval.status.${approval.status}`)}
            </small>
            {approval.status === 'pending' ? (
              <div className="jiuwenmate-chat-approval__actions">
                <button
                  type="button"
                  disabled={permissionActionId === approval.approvalId}
                  onClick={() => {
                    setPermissionActionId(approval.approvalId);
                    void privateChat.decidePermission(approval.approvalId, 'reject').finally(() => setPermissionActionId(null));
                  }}
                  data-testid="jiuwenmate-chat-permission-reject"
                >
                  {t('jiuwenmate.chat.approval.rejectAll')}
                </button>
              </div>
            ) : null}
          </article>
        ) : null}
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

  const workspaceConnections = connections.filter((connection, index, all) =>
    all.findIndex((item) => item.agentId === connection.agentId) === index,
  );
  const connectionLabel = (connection: JiuwenMateConnection) => [...new Set(
    connections
      .filter((item) => item.agentId === connection.agentId)
      .map((item) => item.channel === 'web'
        ? t('jiuwenmate.workspace.webConnection')
        : `${platformLabel(item.channel)} · ${item.connectionKey}`),
  )].join(' + ');

  const renderConnectionSelect = () =>
    workspaceConnections.length > 1 ? (
      <label className="jiuwenmate-workspace-connection" data-testid="jiuwenmate-workspace-connection">
        <span>{t('jiuwenmate.workspace.connection')}</span>
        <select
          value={agentId}
          onChange={(event) => {
            setWorkspaceAgentId(event.target.value);
            setWebResult(null);
            setPicked(null);
            setBoard(false);
          }}
          data-testid="jiuwenmate-workspace-connection-select"
        >
          {workspaceConnections.map((connection) => (
            <option key={connection.agentId} value={connection.agentId}>
              {connectionLabel(connection)}
            </option>
          ))}
        </select>
        {workspace && workspace.channel !== 'web' ? (
          <small data-testid="jiuwenmate-workspace-connection-hint">
            {t('jiuwenmate.workspace.connectionHint', { platform: platformLabel(workspace.channel) })}
          </small>
        ) : null}
        {webResult ? (
          <button
            type="button"
            className="jiuwenmate-link-button jiuwenmate-link-button--blue"
            onClick={() => {
              openConversation(webResult.agentId, webResult.conversationId);
              setState((current) => selectRecordTab(current, 'conversations'));
            }}
            data-testid="jiuwenmate-workspace-web-result"
          >
            {t('jiuwenmate.workspace.webResult')}
          </button>
        ) : null}
      </label>
    ) : null;

  const renderChatPanelButtons = () => (
    <div className="jiuwenmate-main-header__actions">
      <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.artifacts')} aria-pressed={rightPanel === 'artifacts'} data-testid="jiuwenmate-open-artifacts-panel" onClick={() => {
        setState((current) => clearManagementChannel(closeChannelTrace(current)));
        setRightPanel((current) => current === 'artifacts' ? null : 'artifacts');
      }}>
        <Folder aria-hidden />
      </button>
      <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.management')} aria-pressed={rightPanel === 'management'} data-testid="jiuwenmate-open-management-panel" onClick={() => {
        if (rightPanel === 'management') setState((current) => clearManagementChannel(closeChannelTrace(current)));
        setRightPanel((current) => current === 'management' ? null : 'management');
      }}>
        <ClipboardList aria-hidden />
      </button>
    </div>
  );

  const renderRecordList = () => {
    if (state.recordTab === 'conversations') {
      const directory = groupChat?.directory;
      const entries = mergeChatEntries(records, directory?.groups ?? [], directory?.pinnedIds ?? new Set<string>());
      const renderConversation = (record: (typeof records)[number]) => (
        <div
          key={`${record.agentId}:${record.id}`}
          className="jiuwenmate-records__item jiuwenmate-records__item--conversation"
          aria-current={!board && !activeGroupId && record.agentId === agentId && record.id === activeConversationId ? 'true' : undefined}
          data-testid="jiuwenmate-conversation-record"
        >
          {renamingId === `${record.agentId}:${record.id}` ? (
            <div className="jiuwenmate-records__conversation">
              <ColleagueAvatar colleague={ownerOf(record.agentId) ?? colleague} size="medium" />
              <span className="jiuwenmate-records__conversation-copy">
                <input
                  autoFocus
                  defaultValue={ownerOf(record.agentId)?.name ?? ''}
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
                <small>{record.id === SHARING_CONVERSATION_ID ? t('jiuwenmate.sharing.title') : record.lastMessage || record.title}</small>
              </span>
              <time>{new Date(record.updatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</time>
            </div>
          ) : (
            <button type="button" onClick={() => openConversation(record.agentId, record.id)}>
              <ColleagueAvatar colleague={ownerOf(record.agentId) ?? colleague} size="medium" />
              <span className="jiuwenmate-records__conversation-copy">
                <strong>{ownerOf(record.agentId)?.name ?? record.agentId}</strong>
                <small>{record.id === SHARING_CONVERSATION_ID ? t('jiuwenmate.sharing.title') : record.lastMessage || record.title}</small>
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
      const renderEntry = (entry: (typeof entries.pinned)[number]) => {
        if (entry.kind === 'colleague') return renderConversation(entry.record);
        const { group } = entry;
        return (
          <GroupConversationRecord
            key={entry.key}
            group={group}
            active={!board && group.group_id === activeGroupId}
            pinned={entry.pinned}
            pinDisabled={directory?.pinningId != null}
            onOpen={() => {
              setBoard(false);
              groupChat?.onSelectGroup(group.group_id);
              if (compactWorkspace()) setState((current) => setRecordsCollapsed(current, true));
            }}
            onRename={() => directory?.openEdit(group)}
            onPin={() => directory?.pin(group, !entry.pinned)}
            onDelete={() => directory?.openDelete(group)}
          />
        );
      };
      return (
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-conversation-record-list">
          {entries.pinned.length > 0 ? (
            <>
              <header>
                <strong>{t('jiuwenmate.workspace.pinned')}</strong>
              </header>
              {entries.pinned.map(renderEntry)}
            </>
          ) : null}
          <header>
            <strong>{t('jiuwenmate.workspace.tabs.conversations')}</strong>
          </header>
          {entries.pinned.length + entries.others.length === 0 ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSessions')}</p>
          ) : (
            entries.others.map(renderEntry)
          )}
          {directory?.hasMore ? (
            <button type="button" className="jiuwenmate-records__more" disabled={directory.loadingMore}
              onClick={directory.loadMore} data-testid="jiuwenmate-group-records-more">{t('groupChat.more')}</button>
          ) : null}
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
            channelGroups(channels).map((group) => (
              <section key={group.platform} className="jiuwenmate-channel-group" data-platform={group.platform}>
                <h3>{platformLabel(group.platform)}</h3>
                {group.items.map((channel) => (
                  <div key={channel.id} className="jiuwenmate-records__item jiuwenmate-records__item--channel" aria-current={channel.id === selectedChannel?.id ? 'true' : undefined}>
                    <button
                      type="button"
                      onClick={() => setState((current) => {
                        const next = selectChannel(current, channel.id);
                        return compactWorkspace() ? setRecordsCollapsed(next, true) : next;
                      })}
                      data-testid="jiuwenmate-channel-record"
                      data-variant={channel.id}
                    >
                      <span className="jiuwenmate-letter-avatar" data-variant={channel.id}>
                        {channel.name.slice(0, 1)}
                      </span>
                      <span>{channel.name}</span>
                      <em className="jiuwenmate-platform-badge">{platformLabel(channel.platform)}</em>
                    </button>
                  </div>
                ))}
              </section>
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
              <button type="button" onClick={() => {
                setScheduleId(plan.id);
                if (compactWorkspace()) setState((current) => setRecordsCollapsed(current, true));
              }}>{plan.name}</button>
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
          <strong>{t('jiuwenmate.workspace.newSession')}</strong>
          {renderConnectionSelect()}
          {renderChatPanelButtons()}
        </header>
        <div className="jiuwenmate-chat-welcome__content">
          {sharingInbox}
          <div className="jiuwenmate-chat-welcome__center">
            <ColleagueAvatar colleague={colleague} size="large" />
            <h2>{colleague.name}</h2>
            <p>{colleague.description}</p>
          </div>
          <div className="jiuwenmate-chat-welcome__suggestions">
            {prompts.map((prompt, index) => (
              <button type="button" key={`${prompt}-${index}`} onClick={() => setDraft(prompt)}>
                <span>{prompt}</span>
                <ChevronRight aria-hidden />
              </button>
            ))}
          </div>
        </div>
        <footer>
          {composer}
          <small>{t('jiuwenmate.workspace.aiDisclaimer')}</small>
        </footer>
      </section>
    );
  };

  const renderSession = () => (
    <section className="jiuwenmate-session-view" data-testid="jiuwenmate-session-view">
      <header className="jiuwenmate-main-header">
        <strong>
          {sharingConversation ? t('jiuwenmate.sharing.title') : colleague.name}
        </strong>
        {renderConnectionSelect()}
        {renderChatPanelButtons()}
      </header>
      {!sharingConversation ? sharingInbox : null}
      <ol
        ref={messageListRef}
        className="jiuwenmate-session-view__body jiuwenmate-chat-messages"
        data-testid="jiuwenmate-chat-messages"
        aria-busy={!sharingConversation && privateChat.loading}
      >
        {sharingConversation ? <li data-testid="jiuwenmate-sharing-conversation">{sharingInbox}</li> : null}
        {!sharingConversation && privateChat.loading ? (
          <li className="jiuwenmate-chat-history-loading">{t('jiuwenmate.workspace.loadingHistory')}</li>
        ) : null}
        {!sharingConversation ? privateChat.requests.map(renderRequest) : null}
      </ol>
      {!sharingConversation ? <footer>
        {composer}
        <small>{t('jiuwenmate.workspace.aiDisclaimer')}</small>
      </footer> : null}
    </section>
  );

  const renderChannel = (channel: JiuwenMateChannel, inManagementPanel = false) => (
    <section className="jiuwenmate-channel-view" data-testid={inManagementPanel ? 'jiuwenmate-management-channel-detail' : 'jiuwenmate-channel-view'}>
      <header className={inManagementPanel ? 'jiuwenmate-channel-detail__header' : 'jiuwenmate-main-header'}>
        {inManagementPanel ? (
          <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.backToChannels')}
            onClick={() => setState((current) => clearManagementChannel(closeChannelTrace(current)))} data-testid="jiuwenmate-back-to-channel-list">
            <ArrowLeft aria-hidden />
          </button>
        ) : null}
        <strong>
          {channel.name}
          <ChevronDown aria-hidden />
        </strong>
        {!state.traceOpen ? (
          <button
            type="button"
            className="jiuwenmate-link-button"
            onClick={() => setState((current) => inManagementPanel ? { ...current, traceOpen: true } : openChannelTrace(current))}
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
        <div className="jiuwenmate-channel-view__messages" aria-busy={channelRecords === 'loading'}>
          {channel.coverage === 'received_events_only' ? (
            <p className="jiuwenmate-channel-history-notice" role="note" data-testid="jiuwenmate-dingtalk-history-notice">
              {t('jiuwenmate.workspace.dingtalkHistoryCoverage')}
            </p>
          ) : null}
          {channelNextCursor ? (
            <button
              type="button"
              className="jiuwenmate-link-button jiuwenmate-channel-history-more"
              disabled={loadingOlderChannelMessages}
              onClick={() => void loadOlderChannelMessages()}
              data-testid="jiuwenmate-channel-history-more"
            >
              {loadingOlderChannelMessages
                ? t('jiuwenmate.workspace.loadingOlderChannelMessages')
                : t('jiuwenmate.workspace.loadOlderChannelMessages')}
            </button>
          ) : null}
          {channelNotice ? <p className="jiuwenmate-records__empty">{channelNotice}</p> : null}
          {!channelNotice && channelRecords === 'loading' ? (
            <p className="jiuwenmate-records__empty" data-testid="jiuwenmate-channel-messages-loading">
              {t('jiuwenmate.workspace.loadingChannelMessages')}
            </p>
          ) : null}
          {!channelNotice && channelRecords === 'empty' ? (
            <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noChannelMessages')}</p>
          ) : null}
          {channelRecords === 'records'
            ? channelMessages.map((message) => (
            <article key={message.id} data-testid="jiuwenmate-channel-message" data-variant={message.id} data-own={message.fromColleague ? 'true' : 'false'}>
              <header>
                {message.fromColleague ? (
                  <ColleagueAvatar colleague={colleague} size="small" />
                ) : (
                  <span className="jiuwenmate-letter-avatar jiuwenmate-letter-avatar--round" data-variant={message.sender}>
                    {message.sender.slice(0, 1)}
                  </span>
                )}
                <strong>{message.fromColleague ? colleague.name : message.sender}</strong>
                {message.receivedAt ? (
                  <time dateTime={message.receivedAt}>{new Date(message.receivedAt).toLocaleString()}</time>
                ) : null}
              </header>
              <p>
                {message.mentionColleague ? <span className="jiuwenmate-mention">@{colleague.name}</span> : null}
                {message.mentionColleague ? ' ' : null}
                {message.status === 'recalled' ? t('jiuwenmate.workspace.messageRecalled') : message.content}
              </p>
              {message.attachments?.map((attachment, index) => (
                <div className="jiuwenmate-attachment-card" key={`${message.id}:attachment:${index}`}>
                  <img src={documentIcon} alt="" aria-hidden />
                  <span>
                    <strong>
                      {attachment.title || t('jiuwenmate.workspace.attachment', { count: index + 1 })}
                    </strong>
                    <small>{attachment.kind === 'file' ? t('jiuwenmate.workspace.attachmentKind') : attachment.kind}</small>
                  </span>
                </div>
              ))}
            </article>
          ))
            : null}
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
    return sharingConversation || privateChat.loading || privateChat.requests.length > 0 ? renderSession() : renderWelcome();
  };

  return (
    <div
      className={`jiuwenmate-workspace${state.recordsCollapsed ? ' jiuwenmate-workspace--collapsed' : ''}${!board && !activeGroupId && rightPanel ? ' jiuwenmate-workspace--panel-open' : ''}${activeGroupId && !board ? ' jiuwenmate-workspace--group' : ''}`}
      data-testid="jiuwenmate-colleague-workspace"
    >
      {!state.recordsCollapsed ? (
        <aside className="jiuwenmate-records" data-testid="jiuwenmate-record-panel">
          <header>
            <h2>{t('jiuwenmate.nav.messages')}</h2>
          </header>
          {groupChat ? (
            <button
              type="button"
              className="jiuwenmate-records__create-group"
              onClick={groupChat.directory.openCreate}
              data-testid="jiuwenmate-create-group-chat"
            >
              <SquarePlus aria-hidden />
              <span>{t('groupChat.create')}</span>
            </button>
          ) : null}
          {renderRecordList()}
        </aside>
      ) : null}
      <main className="jiuwenmate-workspace__main">
        {state.recordsCollapsed ? (
          <button
            type="button"
            className="jiuwenmate-icon-button jiuwenmate-workspace__expand"
            aria-label={t('jiuwenmate.workspace.expandRecords')}
            onClick={() => {
              if (compactWorkspace()) setRightPanel(null);
              setState((current) => setRecordsCollapsed(current, false));
            }}
          >
            <PanelLeftOpen aria-hidden />
          </button>
        ) : null}
        {board && onSaveSchedule && onUpdateSchedule && onSetScheduleEnabled && onDeleteSchedule && onLoadScheduleHistory ? (
          <ScheduleBoard
            schedules={schedules}
            colleagues={colleagues}
            activity={activity}
            saving={savingSchedule}
            onCreate={onSaveSchedule}
            onUpdate={onUpdateSchedule}
            onSetEnabled={onSetScheduleEnabled}
            onDelete={onDeleteSchedule}
            onLoadHistory={onLoadScheduleHistory}
          />
        ) : activeGroupId && groupChat ? (
          <GroupChatWorkspace key={activeGroupId} groupId={activeGroupId} directory={groupChat.directory}
            onBack={() => groupChat.onSelectGroup()} />
        ) : renderMain()}
      </main>
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
      {!board && !activeGroupId && rightPanel ? (
        <aside className="jiuwenmate-side-panel" data-testid={`jiuwenmate-${rightPanel}-panel`}>
          {rightPanel === 'management' && state.managementChannelId && selectedChannel ? (
            renderChannel(selectedChannel, true)
          ) : (
          <>
          {rightPanel === 'artifacts' && visibleArtifact ? (
            <header className="jiuwenmate-side-panel__preview-header">
              <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.backToArtifacts')} onClick={() => setSelectedArtifact(null)}><ArrowLeft aria-hidden /></button>
              <strong>{visibleArtifact.name}</strong>
            </header>
          ) : (
            <header className="jiuwenmate-side-panel__header">
              <h2>{t(rightPanel === 'management' ? 'jiuwenmate.workspace.management' : 'jiuwenmate.workspace.artifacts')}</h2>
              <button type="button" className="jiuwenmate-icon-button" aria-label={t('jiuwenmate.workspace.closePanel')} onClick={() => { setRightPanel(null); setSelectedArtifact(null); setState((current) => clearManagementChannel(closeChannelTrace(current))); }}>
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
                    {channelGroups(channels.filter((channel) => channel.name.toLocaleLowerCase().includes(managementSearch.trim().toLocaleLowerCase()))).map((group) => (
                      <section key={group.platform} className="jiuwenmate-channel-group" data-platform={group.platform}>
                        <h3>{platformLabel(group.platform)}</h3>
                        {group.items.map((channel) => (
                          <button key={channel.id} type="button" className="jiuwenmate-side-panel__item" aria-current={channel.id === selectedChannel?.id && (state.managementChannelId === channel.id || state.recordTab === 'channels') ? 'true' : undefined} data-testid="jiuwenmate-management-channel-item" data-variant={channel.id} onClick={() => {
                            setState((current) => selectManagementChannel(current, channel.id));
                          }}>
                            <span className="jiuwenmate-letter-avatar" data-variant={channel.id}>{channel.name.slice(0, 1)}</span>
                            <span>{channel.name}</span>
                            <em className="jiuwenmate-platform-badge">{platformLabel(channel.platform)}</em>
                          </button>
                        ))}
                      </section>
                    ))}
                  </>
                ) : (
                  <>
                    <input className="jiuwenmate-side-panel__search" value={managementSearch} onChange={(event) => setManagementSearch(event.target.value)} placeholder={t('jiuwenmate.workspace.searchKeyword')} aria-label={t('jiuwenmate.workspace.searchKeyword')} />
                    <div className="jiuwenmate-side-panel__schedule-heading">
                      <strong>{t('jiuwenmate.workspace.tabs.schedules')}</strong>
                      <button
                        type="button"
                        className="jiuwenmate-icon-button"
                        aria-label={t('jiuwenmate.schedule.createTitle')}
                        onClick={() => onCreateSchedule(colleague.id)}
                      >
                        <Plus aria-hidden />
                      </button>
                    </div>
                    {colleaguePlans.length === 0 ? <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSchedules')}</p> : null}
                    {colleaguePlans.filter((plan) => plan.name.toLocaleLowerCase().includes(managementSearch.trim().toLocaleLowerCase())).map((plan) => (
                      <div key={plan.id} className="jiuwenmate-side-panel__schedule-item">
                        <span className="jiuwenmate-side-panel__schedule-name">{plan.name}</span>
                        <ActionMenu
                          label={t('jiuwenmate.workspace.more')}
                          items={[
                            { key: 'edit', label: t('jiuwenmate.schedule.editTask'), icon: PencilLine, onSelect: () => setScheduleEditor(plan) },
                            { key: 'delete', label: t('jiuwenmate.schedule.delete'), icon: Trash2, onSelect: () => void onDeleteSchedule?.(plan.id) },
                          ]}
                        />
                      </div>
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
          </>
          )}
        </aside>
      ) : null}
    </div>
  );
}
