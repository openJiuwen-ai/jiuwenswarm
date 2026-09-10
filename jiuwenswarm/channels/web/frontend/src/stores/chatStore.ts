/**
 * 聊天状态管理（多 session 版本）
 *
 * 所有对话运行态按 session 隔离存储在 runtimes 中。
 * 组件通过 activeSessionId 读取当前会话的运行态。
 */

import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import {
  Message,
  ToolCall,
  ToolResult,
  ToolExecution,
  ToolExecutionStatus,
  InterruptResultPayload,
  SubtaskUpdatePayload,
  AskUserQuestionPayload,
  EvolutionStatusPayload,
  UsageSummary,
  FileDownloadItem,
  ContextCompressionRuntime,
  ContextCompressionSummary,
  TodoItem,
} from '../types';
import { useTodoStore } from './todoStore';
import {
  mergeToolResultProgress,
  shouldDropToolResult,
} from './toolResultLifecycle';
import { mergeFileDownloadItems } from '../utils/fileDownloadDedup';
import { parseTimestampToMs } from '../utils/timestamp';

const TOOL_TIMEOUT_MS = 12_000_000;
const EVOLUTION_STATUS_END_VISIBLE_MS = 3_000;

let reasoningSegmentSeq = 0;

function createReasoningSegmentId(): string {
  reasoningSegmentSeq += 1;
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `rsn-${crypto.randomUUID()}`;
  }
  return `rsn-${Date.now()}-${reasoningSegmentSeq}-${Math.random().toString(36).slice(2, 10)}`;
}

function computeTimeoutAt(baseIso: string): string {
  return new Date(Date.parse(baseIso) + TOOL_TIMEOUT_MS).toISOString();
}

function resolveExecutionStatus(result: ToolResult): ToolExecutionStatus {
  if (result.timedOut) {
    return 'timeout';
  }
  return result.success ? 'completed' : 'error';
}

/**
 * 子任务状态
 */
export interface SubtaskState {
  task_id: string;
  description: string;
  status: string;
  index: number;
  total: number;
  tool_name?: string;
  tool_count: number;
  message?: string;
  is_parallel: boolean;
}

interface TaskItem {
  id: string;
  content: string;
  timestamp: number;
}

export interface HistoryPagerMeta {
  loadedPages: number;
  totalPages: number;
}

/**
 * 单个 session 的对话运行态。
 * 原全局字段全部迁移到这里，按 session 隔离。
 */
export interface ReasoningSegment {
  id: string;
  text: string;
  startedAt: number;
  closed: boolean;
  /** 收尾时刻；用于延迟折进 streak。历史可省略。 */
  closedAt?: number;
}

export interface ChatRuntime {
  messages: Message[];
  isProcessing: boolean;
  executionError: string | null;
  isThinking: boolean;
  isLoadingHistory: boolean;
  historyPagerMeta: HistoryPagerMeta | null;
  evolutionStatus: EvolutionStatusPayload | null;
  isPaused: boolean;
  pausedTask: string | null;
  interruptResult: InterruptResultPayload | null;
  switchingMode: boolean;
  isNewSession: boolean;
  currentStreamContent: string;
  currentStreamId: string | null;
  /** 本轮是否已按工具边界分段（chat.final 去重）。 */
  assistantStreamSplit: boolean;
  reasoningSegments: ReasoningSegment[];
  /** 「思考中」耗时锚点：仅在可见文字产出时前移。 */
  thinkingAnchorAt: number;
  messageRenderKeySeq: number;
  /** 最近一次 chat.error 的错误信息，用于会话列表展示异常标记 */
  error: string | null;
  streamBuffers: Map<string, string>;
  activeSubtasks: Map<string, SubtaskState>;
  toolExecutions: Map<string, ToolExecution>;
  toolExecutionOrder: string[];
  orphanResults: Map<string, ToolResult>;
  contextCompressionRuntime?: ContextCompressionRuntime;
  contextCompressionSummary?: ContextCompressionSummary;
  toolMetrics: {
    toolCallDedupDropped: number;
    toolResultDedupDropped: number;
  };
  taskQueue: TaskItem[];
  queuePaused: boolean;
  pendingQuestion: AskUserQuestionPayload | null;
  inputValue: string;
  /** evolutionStatus 自动清除定时器，按 session 隔离 */
  evolutionStatusClearTimer: ReturnType<typeof setTimeout> | null;
  /** interruptResult 自动清除定时器，按 session 隔离 */
  interruptResultClearTimer: ReturnType<typeof setTimeout> | null;
}

function createEmptyRuntime(): ChatRuntime {
  return {
    messages: [],
    isProcessing: false,
    executionError: null,
    isThinking: false,
    isLoadingHistory: false,
    historyPagerMeta: null,
    evolutionStatus: null,
    isPaused: false,
    pausedTask: null,
    interruptResult: null,
    switchingMode: false,
    isNewSession: false,
    currentStreamContent: '',
    currentStreamId: null,
    assistantStreamSplit: false,
    reasoningSegments: [],
    thinkingAnchorAt: Date.now(),
    messageRenderKeySeq: 0,
    error: null,
    streamBuffers: new Map(),
    activeSubtasks: new Map(),
    toolExecutions: new Map(),
    toolExecutionOrder: [],
    orphanResults: new Map(),
    contextCompressionRuntime: undefined,
    contextCompressionSummary: undefined,
    toolMetrics: {
      toolCallDedupDropped: 0,
      toolResultDedupDropped: 0,
    },
    taskQueue: [],
    queuePaused: false,
    pendingQuestion: null,
    inputValue: '',
    evolutionStatusClearTimer: null,
    interruptResultClearTimer: null,
  };
}

function assignMessageRenderKeys(
  runtime: ChatRuntime,
  messages: Message[]
): { messages: Message[]; messageRenderKeySeq: number } {
  let messageRenderKeySeq = runtime.messageRenderKeySeq;
  return {
    messages: messages.map((message) => {
      if (message.renderKey) {
        return message;
      }
      messageRenderKeySeq += 1;
      return {
        ...message,
        renderKey: `message-${messageRenderKeySeq}`,
      };
    }),
    messageRenderKeySeq,
  };
}

interface ChatState {
  runtimes: Record<string, ChatRuntime>;
  activeSessionId: string | null;
  /** Gateway broadcasts this status without a session id, so it is intentionally app-wide. */
  globalTaskRunning: boolean;

  ensureRuntime: (sessionId: string) => ChatRuntime;
  getRuntime: (sessionId: string | null) => ChatRuntime | undefined;
  setActiveSessionId: (sessionId: string | null) => void;
  setGlobalTaskRunning: (running: boolean) => void;
  removeRuntime: (sessionId: string) => void;

  addMessage: (sessionId: string, message: Message) => void;
  replaceHistoryMessages: (sessionId: string, messages: Message[]) => void;
  updateMessage: (sessionId: string, id: string, updates: Partial<Message>) => void;
  appendStreamContent: (sessionId: string, content: string, streamKey?: string) => void;
  appendReasoning: (sessionId: string, content: string, options?: { atMs?: number }) => void;
  closeReasoning: (sessionId: string, options?: { atMs?: number }) => void;
  restoreReasoningSegments: (sessionId: string, items: { at: string; text: string }[]) => void;
  startStreaming: (sessionId: string, messageId: string, streamKey?: string) => void;
  stopStreaming: (sessionId: string, streamKey?: string) => void;
  finalizeStreamSegment: (sessionId: string, streamKey?: string) => void;
  finalizeTeamLeaderSegment: (sessionId: string) => void;
  clearStreamSplit: (sessionId: string) => void;
  collapseTurnFinal: (
    sessionId: string,
    opts: { kind: 'agent' | 'team'; content: string; finalId: string; timestampIso: string }
  ) => void;
  bumpThinkingAnchor: (sessionId: string) => void;
  setExecutionError: (sessionId: string, error: string | null) => void;
  setProcessing: (sessionId: string, status: boolean) => void;
  setThinking: (sessionId: string, status: boolean) => void;
  setLoadingHistory: (sessionId: string, status: boolean) => void;
  setHistoryPagerMeta: (sessionId: string, meta: HistoryPagerMeta | null) => void;
  setEvolutionStatus: (sessionId: string, status: EvolutionStatusPayload | null) => void;
  setPaused: (sessionId: string, paused: boolean, task?: string | null) => void;
  setQueuePaused: (sessionId: string, paused: boolean) => void;
  setInterruptResult: (sessionId: string, result: InterruptResultPayload | null) => void;
  setSwitchingMode: (sessionId: string, switching: boolean) => void;
  setNewSession: (sessionId: string, isNew: boolean) => void;
  addToolCall: (sessionId: string, toolCall: ToolCall, options?: { startedAt?: string; requestId?: string }) => void;
  updateToolProgress: (sessionId: string, toolCallId: string, progress: Partial<ToolResult>) => void;
  addToolResult: (sessionId: string, toolResult: ToolResult, options?: { updatedAt?: string }) => void;
  markTimedOutExecutions: (sessionId: string) => void;
  /** 打断（pause/cancel）时把仍 pending 的工具行标记为 timeout，并注入 "Interrupted by user" 合成结果。 */
  markInterruptedExecutions: (sessionId: string) => void;
  /** 历史回放常只有 tool_call、无 tool_result：把仍 pending 的工具按 startedAt 结算，避免超时巡检用 now 污染耗时 */
  settleHistoricalToolExecutions: (sessionId: string) => void;
  /** 企业版 HTTP/SSE 实时任务收到终止状态时，兜底结算仍未收到结果的工具。 */
  settlePendingToolExecutions: (sessionId: string) => void;
  updateSubtask: (sessionId: string, payload: SubtaskUpdatePayload) => void;
  clearSubtasks: (sessionId: string) => void;
  clearMessages: (sessionId: string) => void;
  clearCurrentTurnData: (sessionId: string, requestId?: string) => void;
  prependMessages: (sessionId: string, olderFirst: Message[]) => void;
  addToTaskQueue: (sessionId: string, content: string) => void;
  clearTaskQueue: (sessionId: string) => void;
  removeFromTaskQueue: (sessionId: string, id: string) => void;
  reorderTaskQueue: (sessionId: string, fromIndex: number, toIndex: number) => void;
  setPendingQuestion: (sessionId: string, question: AskUserQuestionPayload | null) => void;
  setInputValue: (sessionId: string, value: string) => void;
  setSessionError: (sessionId: string, error: string | null) => void;
  setUsageSummary: (sessionId: string, messageId: string, usage: UsageSummary) => void;
  addFileItems: (
    sessionId: string,
    files: FileDownloadItem[],
    options?: { timestampIso?: string }
  ) => void;
  setContextCompressionStatus: (
    sessionId: string,
    runtime?: ContextCompressionRuntime,
    summary?: ContextCompressionSummary
  ) => void;
}

export const useChatStore = create<ChatState>()(subscribeWithSelector((set, get) => ({
  runtimes: {},
  activeSessionId: null,
  globalTaskRunning: false,

  ensureRuntime: (sessionId) => {
    const existing = get().runtimes[sessionId];
    if (existing) return existing;
    const runtime = createEmptyRuntime();
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: runtime },
    }));
    return runtime;
  },

  getRuntime: (sessionId) => {
    if (!sessionId) return undefined;
    return get().runtimes[sessionId];
  },

  setActiveSessionId: (sessionId) => {
    set({ activeSessionId: sessionId });
  },

  setGlobalTaskRunning: (running) => {
    set({ globalTaskRunning: running });
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (runtime) {
        if (runtime.evolutionStatusClearTimer) clearTimeout(runtime.evolutionStatusClearTimer);
        if (runtime.interruptResultClearTimer) clearTimeout(runtime.interruptResultClearTimer);
      }
      const next = { ...state.runtimes };
      delete next[sessionId];
      return {
        runtimes: next,
        activeSessionId: state.activeSessionId === sessionId ? null : state.activeSessionId,
      };
    });
  },

  addMessage: (sessionId, message) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const { messages, messageRenderKeySeq } = assignMessageRenderKeys(runtime, [message]);
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: [...runtime.messages, ...messages],
            messageRenderKeySeq,
            ...(message.role === 'user' ? { assistantStreamSplit: false, reasoningSegments: [] } : {}),
          },
        },
      };
    });
  },

  replaceHistoryMessages: (sessionId, messages) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.evolutionStatusClearTimer) {
        clearTimeout(runtime.evolutionStatusClearTimer);
      }
      const assigned = assignMessageRenderKeys(runtime, messages);
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: assigned.messages,
            messageRenderKeySeq: assigned.messageRenderKeySeq,
            currentStreamContent: '',
            currentStreamId: null,
            assistantStreamSplit: false,
            reasoningSegments: [],
            streamBuffers: new Map(),
            evolutionStatus: null,
            evolutionStatusClearTimer: null,
            isPaused: false,
            pausedTask: null,
            interruptResult: null,
            switchingMode: false,
            activeSubtasks: new Map(),
            toolExecutions: new Map(),
            toolExecutionOrder: [],
            orphanResults: new Map(),
            contextCompressionRuntime: undefined,
            contextCompressionSummary: undefined,
            toolMetrics: {
              toolCallDedupDropped: 0,
              toolResultDedupDropped: 0,
            },
            taskQueue: [],
            pendingQuestion: null,
          },
        },
      };
    });
  },

  updateMessage: (sessionId, id, updates) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === id ? { ...msg, ...updates } : msg
            ),
          },
        },
      };
    });
  },

  appendStreamContent: (sessionId, content, streamKey = 'default') => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || !runtime.currentStreamId) return state;

      const existingBuffer = runtime.streamBuffers.get(streamKey) || '';
      const nextContent = existingBuffer + content;

      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            currentStreamContent: nextContent,
            streamBuffers: new Map(runtime.streamBuffers).set(streamKey, nextContent),
            messages: runtime.messages.map((msg) =>
              msg.id === runtime.currentStreamId
                ? { ...msg, content: nextContent }
                : msg
            ),
          },
        },
      };
    });
  },

  appendReasoning: (sessionId, content, options) => {
    if (!content) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const segments = runtime.reasoningSegments;
      const last = segments[segments.length - 1];
      const atMs =
        typeof options?.atMs === 'number' && Number.isFinite(options.atMs)
          ? options.atMs
          : Date.now();
      let next: ReasoningSegment[];
      if (last && !last.closed) {
        next = segments.slice(0, -1).concat({ ...last, text: last.text + content });
      } else {
        next = segments.concat({
          id: createReasoningSegmentId(),
          text: content,
          startedAt: atMs,
          closed: false,
        });
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, reasoningSegments: next },
        },
      };
    });
  },

  closeReasoning: (sessionId, options) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const segments = runtime.reasoningSegments;
      const last = segments[segments.length - 1];
      if (!last || last.closed) return state;
      const atMs =
        typeof options?.atMs === 'number' && Number.isFinite(options.atMs)
          ? options.atMs
          : Date.now();
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            reasoningSegments: segments.slice(0, -1).concat({
              ...last,
              closed: true,
              closedAt: atMs,
            }),
          },
        },
      };
    });
  },

  restoreReasoningSegments: (sessionId, items) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const segments: ReasoningSegment[] = [];
      const seen = new Set<string>();
      items.forEach((item, index) => {
        const text = item.text?.trim();
        if (!text || seen.has(text)) return;
        seen.add(text);
        const parsed = parseTimestampToMs(item.at);
        // 历史里思考与同一步 final/tool_call 共用落盘时间；减 1ms 仅补齐缺失的独立时间戳，
        // 使时间线能分出「先思考、后动作」，不做跨步骤重排。
        // 解析失败时跳过该段，勿用 index 当 epoch（会让 startMs≈0，耗时爆炸）
        if (!Number.isFinite(parsed)) {
          return;
        }
        const startedAt = parsed - 1;
        segments.push({
          id: `hist-rsn-${sessionId}-${index}-${createReasoningSegmentId()}`,
          text,
          startedAt,
          closed: true,
          // 历史已结束：closedAt 用 startedAt，立刻 settled，且比魔法 0 更可解释。
          closedAt: startedAt,
        });
      });
      segments.sort((a, b) => a.startedAt - b.startedAt);
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, reasoningSegments: segments },
        },
      };
    });
  },

  startStreaming: (sessionId, messageId, streamKey = 'default') => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            currentStreamId: messageId,
            currentStreamContent: '',
            streamBuffers: new Map(runtime.streamBuffers).set(streamKey, ''),
          },
        },
      };
    });
  },

  stopStreaming: (sessionId, streamKey = 'default') => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || !runtime.currentStreamId) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === runtime.currentStreamId ? { ...msg, isStreaming: false } : msg
            ),
            currentStreamId: null,
            currentStreamContent: '',
            streamBuffers: new Map(runtime.streamBuffers).set(streamKey, ''),
          },
        },
      };
    });
  },

  finalizeStreamSegment: (sessionId, streamKey = 'default') => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || !runtime.currentStreamId) return state;
      const streamingMessage = runtime.messages.find(
        (msg) => msg.id === runtime.currentStreamId
      );
      const hasVisibleText = Boolean(streamingMessage?.content?.trim());
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === runtime.currentStreamId ? { ...msg, isStreaming: false } : msg
            ),
            currentStreamId: null,
            currentStreamContent: '',
            assistantStreamSplit: runtime.assistantStreamSplit || hasVisibleText,
            streamBuffers: new Map(runtime.streamBuffers).set(streamKey, ''),
          },
        },
      };
    });
  },

  finalizeTeamLeaderSegment: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      let latestUserIndex = -1;
      for (let i = runtime.messages.length - 1; i >= 0; i -= 1) {
        if (runtime.messages[i].role === 'user') {
          latestUserIndex = i;
          break;
        }
      }
      let target: Message | undefined;
      for (let i = runtime.messages.length - 1; i > latestUserIndex; i -= 1) {
        const msg = runtime.messages[i];
        if (msg.id.startsWith('team-leader-') && msg.isStreaming) {
          target = msg;
          break;
        }
      }
      if (!target || !target.content?.trim()) return state;
      const targetId = target.id;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === targetId ? { ...msg, isStreaming: false } : msg
            ),
            assistantStreamSplit: true,
          },
        },
      };
    });
  },

  clearStreamSplit: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || !runtime.assistantStreamSplit) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, assistantStreamSplit: false },
        },
      };
    });
  },

  collapseTurnFinal: (sessionId, { kind, content, finalId, timestampIso }) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const msgs = runtime.messages;
      let turnStart = 0;
      for (let i = msgs.length - 1; i >= 0; i -= 1) {
        if (msgs[i].role === 'user') {
          turnStart = i + 1;
          break;
        }
      }
      const isTarget = (m: Message) =>
        kind === 'team'
          ? m.role === 'system' && typeof m.id === 'string' && m.id.startsWith('team-leader-')
          : m.role === 'assistant';
      const kept: Message[] = [];
      let removed = 0;
      for (let i = 0; i < msgs.length; i += 1) {
        if (i >= turnStart && isTarget(msgs[i])) {
          removed += 1;
          continue;
        }
        kept.push(msgs[i]);
      }
      if (removed === 0) return state;
      const displayContent =
        kind === 'team'
          ? `team.leader:${JSON.stringify({ content, timestamp: Date.parse(timestampIso) || Date.now() })}`
          : content;
      kept.push({
        id: finalId,
        role: kind === 'team' ? 'system' : 'assistant',
        content: displayContent,
        timestamp: timestampIso,
        completedAt: timestampIso,
        isStreaming: false,
      });
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: kept,
            assistantStreamSplit: false,
            currentStreamId: null,
            currentStreamContent: '',
          },
        },
      };
    });
  },

  bumpThinkingAnchor: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, thinkingAnchorAt: Date.now() },
        },
      };
    });
  },

  setExecutionError: (sessionId, error) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, executionError: error },
        },
      };
    });
  },

  setProcessing: (sessionId, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      // 新一轮开始（false→true）：把「思考中」耗时锚点归到轮次起点。
      const turnStart = status && !runtime.isProcessing;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            isProcessing: status,
            executionError: status ? null : runtime.executionError,
            ...(status ? { error: null } : {}),
            ...(turnStart ? { thinkingAnchorAt: Date.now() } : {}),
          },
        },
      };
    });
  },

  setSessionError: (sessionId, error) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, error },
        },
      };
    });
  },

  setThinking: (sessionId, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.isThinking === status) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, isThinking: status },
        },
      };
    });
  },

  setLoadingHistory: (sessionId, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, isLoadingHistory: status },
        },
      };
    });
  },

  setHistoryPagerMeta: (sessionId, meta) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, historyPagerMeta: meta },
        },
      };
    });
  },

  setEvolutionStatus: (sessionId, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.evolutionStatusClearTimer) {
        clearTimeout(runtime.evolutionStatusClearTimer);
      }
      const nextRuntime: ChatRuntime = { ...runtime, evolutionStatus: status };
      if (status?.status === 'end') {
        nextRuntime.evolutionStatusClearTimer = setTimeout(() => {
          set((s) => {
            const r = s.runtimes[sessionId];
            if (!r || r.evolutionStatus !== status) return s;
            return {
              runtimes: {
                ...s.runtimes,
                [sessionId]: { ...r, evolutionStatus: null, evolutionStatusClearTimer: null },
              },
            };
          });
        }, EVOLUTION_STATUS_END_VISIBLE_MS);
      } else {
        nextRuntime.evolutionStatusClearTimer = null;
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: nextRuntime,
        },
      };
    });
  },

  setPaused: (sessionId, paused, task = null) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, isPaused: paused, pausedTask: task ?? null },
        },
      };
    });
  },

  setQueuePaused: (sessionId, paused) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, queuePaused: paused },
        },
      };
    });
  },

  setInterruptResult: (sessionId, result) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.interruptResultClearTimer) {
        clearTimeout(runtime.interruptResultClearTimer);
      }
      const nextRuntime: ChatRuntime = { ...runtime, interruptResult: result };
      if (result) {
        nextRuntime.interruptResultClearTimer = setTimeout(() => {
          set((s) => {
            const r = s.runtimes[sessionId];
            if (!r || r.interruptResult !== result) return s;
            return {
              runtimes: {
                ...s.runtimes,
                [sessionId]: { ...r, interruptResult: null, interruptResultClearTimer: null },
              },
            };
          });
        }, 3000);
      } else {
        nextRuntime.interruptResultClearTimer = null;
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: nextRuntime,
        },
      };
    });
  },

  setSwitchingMode: (sessionId, switching) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (switching) {
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              switchingMode: true,
              isProcessing: false,
              isPaused: false,
              pausedTask: null,
              interruptResult: null,
            },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, switchingMode: false },
        },
      };
    });
  },

  setNewSession: (sessionId, isNew) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, isNewSession: isNew },
        },
      };
    });
  },

  addToolCall: (sessionId, toolCall, options) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!toolCall.id) {
        const nextDropped = runtime.toolMetrics.toolCallDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolCallDedupDropped', {
            count: nextDropped,
            reason: 'missing toolCallId',
          });
        }
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              toolMetrics: {
                ...runtime.toolMetrics,
                toolCallDedupDropped: nextDropped,
              },
            },
          },
        };
      }
      if (runtime.toolExecutions.has(toolCall.id)) {
        const nextDropped = runtime.toolMetrics.toolCallDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolCallDedupDropped', {
            count: nextDropped,
            reason: 'toolCallId execution hit',
          });
        }
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              toolMetrics: {
                ...runtime.toolMetrics,
                toolCallDedupDropped: nextDropped,
              },
            },
          },
        };
      }
      const nowIso = new Date().toISOString();
      const startedAt =
        typeof options?.startedAt === 'string' && options.startedAt.trim()
          ? options.startedAt.trim()
          : nowIso;
      const orphanResult = runtime.orphanResults.get(toolCall.id);
      const nextExecutions = new Map(runtime.toolExecutions);
      const nextOrphanResults = new Map(runtime.orphanResults);
      if (orphanResult) {
        nextOrphanResults.delete(toolCall.id);
      }
      const timeoutAt = computeTimeoutAt(startedAt);
      const resultStatus = orphanResult ? resolveExecutionStatus(orphanResult) : 'pending';
      nextExecutions.set(toolCall.id, {
        toolCallId: toolCall.id,
        toolCall,
        result: orphanResult,
        status: resultStatus,
        startedAt,
        updatedAt: startedAt,
        timeoutAt,
        requestId: options?.requestId,
      });

      const nextOrder = [...runtime.toolExecutionOrder, toolCall.id];
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            toolExecutions: nextExecutions,
            toolExecutionOrder: nextOrder,
            orphanResults: nextOrphanResults,
          },
        },
      };
    });
  },

  addToolResult: (sessionId, toolResult, options) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const incomingToolCallId = toolResult.toolCallId;
      if (!incomingToolCallId) {
        const nextDropped = runtime.toolMetrics.toolResultDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolResultDedupDropped', {
            count: nextDropped,
            reason: 'missing toolCallId',
          });
        }
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              toolMetrics: {
                ...runtime.toolMetrics,
                toolResultDedupDropped: nextDropped,
              },
            },
          },
        };
      }
      const nowIso = new Date().toISOString();
      const updatedAt =
        typeof options?.updatedAt === 'string' && options.updatedAt.trim()
          ? options.updatedAt.trim()
          : nowIso;
      const existingExecution = runtime.toolExecutions.get(incomingToolCallId);

      if (!existingExecution) {
        const nextOrphanResults = new Map(runtime.orphanResults);
        const duplicatedOrphan = nextOrphanResults.get(incomingToolCallId);
        if (
          duplicatedOrphan &&
          shouldDropToolResult(
            resolveExecutionStatus(duplicatedOrphan),
            duplicatedOrphan,
            toolResult
          )
        ) {
          const nextDropped = runtime.toolMetrics.toolResultDedupDropped + 1;
          if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
            console.debug('[ws][metrics] toolResultDedupDropped', {
              count: nextDropped,
              reason: 'orphan duplicate',
            });
          }
          return {
            runtimes: {
              ...state.runtimes,
              [sessionId]: {
                ...runtime,
                toolMetrics: {
                  ...runtime.toolMetrics,
                  toolResultDedupDropped: nextDropped,
                },
              },
            },
          };
        }
        nextOrphanResults.set(incomingToolCallId, toolResult);
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, orphanResults: nextOrphanResults },
          },
        };
      }

      const mergedToolResult = mergeToolResultProgress(
        existingExecution.result,
        toolResult
      );
      const nextStatus = resolveExecutionStatus(mergedToolResult);

      if (
        shouldDropToolResult(
          existingExecution.status,
          existingExecution.result,
          mergedToolResult
        )
      ) {
        const nextDropped = runtime.toolMetrics.toolResultDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolResultDedupDropped', {
            count: nextDropped,
            reason: 'execution duplicate',
          });
        }
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              toolMetrics: {
                ...runtime.toolMetrics,
                toolResultDedupDropped: nextDropped,
              },
            },
          },
        };
      }

      const nextExecutions = new Map(runtime.toolExecutions);
      nextExecutions.set(incomingToolCallId, {
        ...existingExecution,
        result: mergedToolResult,
        status: nextStatus,
        updatedAt,
        resultArrivedAfterTimeout:
          existingExecution.status === 'timeout' ? true : existingExecution.resultArrivedAfterTimeout,
      });
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, toolExecutions: nextExecutions },
        },
      };
    });
  },

  updateToolProgress: (sessionId, toolCallId, progress) => {
    if (!toolCallId) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const execution = runtime.toolExecutions.get(toolCallId);
      if (!execution) return state;
      const nextExecutions = new Map(runtime.toolExecutions);
      // 进度更新不得把已完成/失败/超时打回 pending，否则 UI 会误显示「执行中」。
      const keepStatus =
        execution.status === 'completed' ||
        execution.status === 'error' ||
        execution.status === 'timeout'
          ? execution.status
          : 'pending';
      nextExecutions.set(toolCallId, {
        ...execution,
        result: {
          toolName: execution.toolCall.name,
          result: '',
          success: true,
          toolCallId,
          ...execution.result,
          ...progress,
        },
        status: keepStatus,
        updatedAt:
          keepStatus === 'pending' ? new Date().toISOString() : execution.updatedAt,
      });
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, toolExecutions: nextExecutions },
        },
      };
    });
  },

  markTimedOutExecutions: (sessionId) => {
    const now = Date.now();
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      let changed = false;
      const nextExecutions = new Map(runtime.toolExecutions);
      for (const [toolCallId, execution] of nextExecutions) {
        if (execution.status !== 'pending') {
          continue;
        }
        const timeoutTs = Date.parse(execution.timeoutAt);
        if (Number.isNaN(timeoutTs) || timeoutTs > now) {
          continue;
        }
        changed = true;
        // timedOutAt = 巡检发现时刻；updatedAt 保持事件时间（startedAt/原值），避免把「已完成」耗时撑到 now。
        nextExecutions.set(toolCallId, {
          ...execution,
          status: 'timeout',
          timedOutAt: new Date(now).toISOString(),
        });
      }
      if (!changed) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, toolExecutions: nextExecutions },
        },
      };
    });
  },

  markInterruptedExecutions: (sessionId) => {
    const nowIso = new Date().toISOString();
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      let changed = false;
      const nextExecutions = new Map(runtime.toolExecutions);
      for (const [toolCallId, execution] of nextExecutions) {
        if (execution.status !== 'pending') {
          continue;
        }
        changed = true;
        nextExecutions.set(toolCallId, {
          ...execution,
          status: 'timeout',
          timedOutAt: nowIso,
          updatedAt: nowIso,
          result: execution.result ?? {
            toolCallId,
            toolName: execution.toolCall.name,
            result: 'Interrupted by user',
            success: false,
            summary: 'Interrupted',
          },
        });
      }
      if (!changed) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            toolExecutions: nextExecutions,
            activeSubtasks: new Map(),
          },
        },
      };
    });
  },

  settlePendingToolExecutions: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      let changed = false;
      const nextExecutions = new Map(runtime.toolExecutions);
      for (const [toolCallId, execution] of nextExecutions) {
        if (execution.status !== 'pending') continue;
        changed = true;
        const result = execution.result ?? {
          toolName: execution.toolCall.name,
          result: '',
          success: true,
          toolCallId,
          summary: 'Task completed without a separate tool result event',
        };
        nextExecutions.set(toolCallId, {
          ...execution,
          result,
          status: 'completed',
          updatedAt: execution.updatedAt || execution.startedAt,
        });
      }
      if (!changed) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, toolExecutions: nextExecutions },
        },
      };
    });
  },

  settleHistoricalToolExecutions: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.isProcessing) return state;
      let changed = false;
      const nextExecutions = new Map(runtime.toolExecutions);
      for (const [toolCallId, execution] of nextExecutions) {
        // 只结算仍 pending 的历史孤儿 tool_call。
        // timeout/error 必须保留，否则刷新后失败/超时会被抹成「已完成」。
        if (execution.status !== 'pending') {
          continue;
        }
        if (execution.result) {
          const nextStatus = resolveExecutionStatus(execution.result);
          if (nextStatus !== execution.status) {
            changed = true;
            nextExecutions.set(toolCallId, {
              ...execution,
              status: nextStatus,
              updatedAt: execution.updatedAt || execution.startedAt,
            });
          }
          continue;
        }
        // 无真实 result：按调用时刻结算为完成，不引入 Date.now()
        changed = true;
        nextExecutions.set(toolCallId, {
          ...execution,
          status: 'completed',
          updatedAt: execution.startedAt,
          result: {
            toolName: execution.toolCall.name,
            result: '',
            success: true,
            toolCallId,
          } as ToolResult,
        });
      }
      if (!changed) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, toolExecutions: nextExecutions },
        },
      };
    });
  },

  updateSubtask: (sessionId, payload) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const newSubtasks = new Map(runtime.activeSubtasks);

      if (payload.status === 'completed' || payload.status === 'error') {
        newSubtasks.delete(payload.task_id);
      } else {
        newSubtasks.set(payload.task_id, {
          task_id: payload.task_id,
          description: payload.description,
          status: payload.status,
          index: payload.index,
          total: payload.total,
          tool_name: payload.tool_name,
          tool_count: payload.tool_count || 0,
          message: payload.message,
          is_parallel: payload.is_parallel || false,
        });
      }

      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, activeSubtasks: newSubtasks },
        },
      };
    });

    const todoState = useTodoStore.getState();
    const todoRuntime = todoState.getRuntime(sessionId);
    const todos = todoRuntime?.todos ?? [];
    const setTodos = todoState.setTodos;

    const matchingTodo = todos.find(
      (todo: TodoItem) =>
        todo.status === 'in_progress' &&
        (todo.content.includes(payload.description) ||
         payload.description.includes(todo.content.slice(0, 20)))
    );

    if (matchingTodo) {
      let activeForm = '';
      if (payload.status === 'starting') {
        activeForm = `正在${payload.description}...`;
      } else if (payload.status === 'tool_call') {
        activeForm = `正在调用 ${payload.tool_name}...`;
      } else if (payload.status === 'completed') {
        activeForm = '';
      }

      if (activeForm || payload.status === 'completed') {
        const updatedTodos = todos.map((todo: TodoItem) =>
          todo.id === matchingTodo.id
            ? { ...todo, activeForm }
            : todo
        );
        setTodos(sessionId, updatedTodos);
      }
    }
  },

  clearSubtasks: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, activeSubtasks: new Map() },
        },
      };
    });
  },

  clearCurrentTurnData: (sessionId, requestId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (requestId) {
        const nextExecutions = new Map(runtime.toolExecutions);
        const nextOrder: string[] = [];
        for (const id of runtime.toolExecutionOrder) {
          const exec = nextExecutions.get(id);
          if (exec && exec.requestId === requestId) {
            nextExecutions.delete(id);
          } else {
            nextOrder.push(id);
          }
        }
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              toolExecutions: nextExecutions,
              toolExecutionOrder: nextOrder,
              orphanResults: new Map(),
              activeSubtasks: new Map(),
              interruptResult: null,
              pendingQuestion: null,
              toolMetrics: {
                toolCallDedupDropped: 0,
                toolResultDedupDropped: 0,
              },
            },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            toolExecutions: new Map(),
            toolExecutionOrder: [],
            orphanResults: new Map(),
            activeSubtasks: new Map(),
            interruptResult: null,
            pendingQuestion: null,
            toolMetrics: {
              toolCallDedupDropped: 0,
              toolResultDedupDropped: 0,
            },
          },
        },
      };
    });
    useTodoStore.getState().clearTodos(sessionId);
  },

  prependMessages: (sessionId, olderFirst) => {
    if (!olderFirst.length) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const assigned = assignMessageRenderKeys(runtime, olderFirst);
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: [...assigned.messages, ...runtime.messages],
            messageRenderKeySeq: assigned.messageRenderKeySeq,
          },
        },
      };
    });
  },

  clearMessages: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.evolutionStatusClearTimer) {
        clearTimeout(runtime.evolutionStatusClearTimer);
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: [],
            currentStreamContent: '',
            currentStreamId: null,
            streamBuffers: new Map(),
            evolutionStatus: null,
            evolutionStatusClearTimer: null,
            isPaused: false,
            pausedTask: null,
            interruptResult: null,
            switchingMode: false,
            activeSubtasks: new Map(),
            toolExecutions: new Map(),
            toolExecutionOrder: [],
            orphanResults: new Map(),
            contextCompressionRuntime: undefined,
            contextCompressionSummary: undefined,
            toolMetrics: {
              toolCallDedupDropped: 0,
              toolResultDedupDropped: 0,
            },
            taskQueue: [],
            pendingQuestion: null,
          },
        },
      };
    });
  },

  addToTaskQueue: (sessionId, content) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            taskQueue: [
              ...runtime.taskQueue,
              {
                id: Date.now().toString() + Math.random().toString(36).substr(2, 9),
                content,
                timestamp: Date.now(),
              },
            ],
          },
        },
      };
    });
  },

  clearTaskQueue: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, taskQueue: [], queuePaused: false },
        },
      };
    });
  },

  removeFromTaskQueue: (sessionId, id) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            taskQueue: runtime.taskQueue.filter((task) => task.id !== id),
          },
        },
      };
    });
  },

  reorderTaskQueue: (sessionId, fromIndex, toIndex) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const queue = [...runtime.taskQueue];
      if (fromIndex < 0 || fromIndex >= queue.length || toIndex < 0 || toIndex >= queue.length || fromIndex === toIndex) {
        return state;
      }
      const [moved] = queue.splice(fromIndex, 1);
      queue.splice(toIndex, 0, moved);
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, taskQueue: queue },
        },
      };
    });
  },

  setPendingQuestion: (sessionId, question) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, pendingQuestion: question },
        },
      };
    });
  },

  setInputValue: (sessionId, value) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, inputValue: value },
        },
      };
    });
  },

  setUsageSummary: (sessionId, messageId, usage) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === messageId ? { ...msg, usageSummary: usage } : msg
            ),
          },
        },
      };
    });
  },

  addFileItems: (sessionId, files, options) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const lastMessage = runtime.messages[runtime.messages.length - 1];
      // 与历史 restore 一致：优先挂当前流；否则挂最近一条助手消息（不是下一条 final）。
      const targetId =
        runtime.currentStreamId ??
        (lastMessage?.role === 'assistant' ||
        (lastMessage?.role === 'system' && lastMessage.id?.startsWith('team-leader-'))
          ? lastMessage.id
          : null);
      const timestampIso =
        typeof options?.timestampIso === 'string' && options.timestampIso.trim()
          ? options.timestampIso.trim()
          : new Date().toISOString();
      if (!targetId) {
        const msgId = `file-${timestampIso}`;
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              messages: [
                ...runtime.messages,
                {
                  id: msgId,
                  role: 'assistant',
                  content: '',
                  timestamp: timestampIso,
                  fileItems: files,
                },
              ],
            },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            messages: runtime.messages.map((msg) =>
              msg.id === targetId
                ? { ...msg, fileItems: mergeFileDownloadItems(msg.fileItems, files) }
                : msg
            ),
          },
        },
      };
    });
  },

  setContextCompressionStatus: (sessionId, runtime, summary) => {
    set((state) => {
      const r = state.runtimes[sessionId];
      if (!r) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...r,
            contextCompressionRuntime: runtime,
            contextCompressionSummary: summary,
          },
        },
      };
    });
  },
})));
