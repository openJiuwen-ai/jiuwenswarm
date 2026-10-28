import type { JiuwenMateTraceRow } from './types';
import type { JiuwenMateConversation } from './jiuwenmateApi';

export type JiuwenMateWorkspaceConversation = JiuwenMateConversation & { agentId: string };

export function mergeWorkspaceConversations(
  allConversations: readonly JiuwenMateWorkspaceConversation[],
  activeAgentId: string,
  activeConversations: readonly JiuwenMateConversation[],
  activeLoaded: boolean,
): JiuwenMateWorkspaceConversation[] {
  const activeRecords = activeLoaded
    ? activeConversations.map((conversation) => ({ ...conversation, agentId: activeAgentId }))
    : allConversations.filter((conversation) => conversation.agentId === activeAgentId);
  return [
    ...allConversations.filter((conversation) => conversation.agentId !== activeAgentId),
    ...activeRecords,
  ].sort(
    (left, right) =>
      Number(Boolean(right.pinned)) - Number(Boolean(left.pinned)) || right.updatedAt.localeCompare(left.updatedAt),
  );
}

export type JiuwenMateRecordTab = 'conversations' | 'channels' | 'schedules';
export type JiuwenMateTraceMetric = 'duration' | 'tokens';
export type JiuwenMateTraceFilter = 'none' | 'turn' | 'tool';

export interface JiuwenMateWorkspaceState {
  selectedColleagueId: string;
  selectedChannelId: string | null;
  managementChannelId: string | null;
  recordTab: JiuwenMateRecordTab;
  recordsCollapsed: boolean;
  traceOpen: boolean;
  traceMetric: JiuwenMateTraceMetric;
  traceFilter: JiuwenMateTraceFilter;
  expandedTurns: readonly number[];
  selectedTraceRowId: string | null;
}

export function createWorkspaceState(
  selectedColleagueId: string,
  selectedChannelId: string | null,
): JiuwenMateWorkspaceState {
  return {
    selectedColleagueId,
    selectedChannelId,
    managementChannelId: null,
    recordTab: 'conversations',
    recordsCollapsed: false,
    traceOpen: false,
    traceMetric: 'duration',
    traceFilter: 'none',
    expandedTurns: [],
    selectedTraceRowId: null,
  };
}

const RESET_TRACE = { traceOpen: false, expandedTurns: [], selectedTraceRowId: null } as const;

export function selectColleague(state: JiuwenMateWorkspaceState, selectedColleagueId: string): JiuwenMateWorkspaceState {
  return { ...state, ...RESET_TRACE, selectedColleagueId };
}

export function selectRecordTab(state: JiuwenMateWorkspaceState, recordTab: JiuwenMateRecordTab): JiuwenMateWorkspaceState {
  return { ...state, ...RESET_TRACE, recordTab, managementChannelId: null };
}

export function selectChannel(state: JiuwenMateWorkspaceState, selectedChannelId: string): JiuwenMateWorkspaceState {
  return { ...state, ...RESET_TRACE, selectedChannelId, managementChannelId: null };
}

export function selectManagementChannel(state: JiuwenMateWorkspaceState, managementChannelId: string): JiuwenMateWorkspaceState {
  return { ...state, ...RESET_TRACE, managementChannelId };
}

export function clearManagementChannel(state: JiuwenMateWorkspaceState): JiuwenMateWorkspaceState {
  return state.managementChannelId ? { ...state, managementChannelId: null } : state;
}

export type ChannelRecordView = 'loading' | 'empty' | 'records';

export function channelRecordView(
  channelKey: string | null,
  settledChannelKey: string | null,
  messageCount: number,
): ChannelRecordView {
  if (channelKey && channelKey !== settledChannelKey) return 'loading';
  if (messageCount === 0) return 'empty';
  return 'records';
}

export function setRecordsCollapsed(state: JiuwenMateWorkspaceState, recordsCollapsed: boolean): JiuwenMateWorkspaceState {
  return { ...state, recordsCollapsed };
}

export function openChannelTrace(state: JiuwenMateWorkspaceState): JiuwenMateWorkspaceState {
  return { ...state, recordTab: 'channels', traceOpen: true };
}

export function closeChannelTrace(state: JiuwenMateWorkspaceState): JiuwenMateWorkspaceState {
  return { ...state, ...RESET_TRACE };
}

export function setTraceMetric(state: JiuwenMateWorkspaceState, traceMetric: JiuwenMateTraceMetric): JiuwenMateWorkspaceState {
  return { ...state, traceMetric };
}

export function setTraceFilter(state: JiuwenMateWorkspaceState, traceFilter: JiuwenMateTraceFilter): JiuwenMateWorkspaceState {
  return { ...state, traceFilter, expandedTurns: [] };
}

export function toggleTraceFilter(
  state: JiuwenMateWorkspaceState,
  filter: Exclude<JiuwenMateTraceFilter, 'none'>,
): JiuwenMateWorkspaceState {
  return setTraceFilter(state, state.traceFilter === filter ? 'none' : filter);
}

export function toggleTraceTurn(state: JiuwenMateWorkspaceState, turn: number): JiuwenMateWorkspaceState {
  const expanded = state.expandedTurns.includes(turn);
  const expandedTurns = expanded ? state.expandedTurns.filter((item) => item !== turn) : [...state.expandedTurns, turn];
  if (state.traceFilter === 'turn' && !expanded) {
    return { ...state, traceFilter: 'none', expandedTurns: [] };
  }
  return { ...state, expandedTurns };
}

export function selectTraceRow(state: JiuwenMateWorkspaceState, selectedTraceRowId: string | null): JiuwenMateWorkspaceState {
  return { ...state, selectedTraceRowId: state.selectedTraceRowId === selectedTraceRowId ? null : selectedTraceRowId };
}

export type JiuwenMateTraceItem =
  | { kind: 'row'; row: JiuwenMateTraceRow }
  | { kind: 'collapsed'; id: string; steps: number; tools: number; toolNames: string[]; onlyTools: boolean };

export interface JiuwenMateTraceTurnView {
  turn: number;
  items: JiuwenMateTraceItem[];
}

function toolName(row: JiuwenMateTraceRow): string {
  return row.content.split(/[（(]/)[0].replace(/_/g, ' ').trim();
}

function isVisible(
  row: JiuwenMateTraceRow,
  index: number,
  filter: JiuwenMateTraceFilter,
  expanded: boolean,
): boolean {
  if (filter === 'turn' && !expanded) return index === 0;
  if (row.role === 'tool') return filter === 'tool' || expanded;
  return true;
}

export function traceTurnViews(
  rows: readonly JiuwenMateTraceRow[],
  filter: JiuwenMateTraceFilter,
  expandedTurns: readonly number[],
): JiuwenMateTraceTurnView[] {
  const turns = [...new Set(rows.filter((row) => row.turn > 0).map((row) => row.turn))];
  return turns.map((turn) => {
    const turnRows = rows.filter((row) => row.turn === turn);
    const expanded = expandedTurns.includes(turn);
    const items: JiuwenMateTraceItem[] = [];
    let hidden: JiuwenMateTraceRow[] = [];
    const flush = () => {
      if (hidden.length === 0) return;
      const tools = hidden.filter((row) => row.role === 'tool');
      items.push({
        kind: 'collapsed',
        id: `turn-${turn}-collapsed-${items.length}`,
        steps: hidden.filter((row) => row.role === 'assistant').length,
        tools: tools.length,
        toolNames: tools.map(toolName),
        onlyTools: tools.length === hidden.length,
      });
      hidden = [];
    };
    turnRows.forEach((row, index) => {
      if (isVisible(row, index, filter, expanded)) {
        flush();
        items.push({ kind: 'row', row });
      } else {
        hidden.push(row);
      }
    });
    flush();
    return { turn, items };
  });
}
