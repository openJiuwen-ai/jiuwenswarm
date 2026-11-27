/**
 * SkillDev 状态管理
 *
 * 管理 SkillDev 模式的完整状态
 */

import { create } from 'zustand';
import type {
  SkillDevStage,
  SkillDevTaskMode,
  SkillDevTodo,
  SkillDevArtifact,
  SkillDevPlan,
  SkillDevEvalResult,
  SkillDevReport,
  ConfirmRequest,
  FileTreeNode,
  ClarifyQuestion,
  SkillDevRestoreResult,
} from '../types/skilldev';
import type { ToolCall, ToolResult, ToolExecution, MediaItem, AskUserQuestionPayload } from '../types';

interface SkillDevStateStore {
  // ========== 核心状态 ==========
  taskId: string | null;
  stage: SkillDevStage;
  mode: SkillDevTaskMode;
  iteration: number;
  query: string;
  isProcessing: boolean;
  isSuspended: boolean;
  error: string | null;

  // ========== UI 状态 ==========
  todos: SkillDevTodo[];
  artifacts: SkillDevArtifact[];
  messages: SkillDevMessage[];
  toolExecutions: Map<string, ToolExecution>;
  toolExecutionOrder: string[];
  orphanResults: Map<string, ToolResult>;
  _thinkingBreakPending: boolean;
  fileTree: FileTreeNode[];
  currentFile: { path: string; content: string } | null;
  // 问题澄清（内联展示，不弹窗）
  clarifyQuestions: ClarifyQuestion[] | null;
  isClarifySubmitted: boolean;
  // Agent ask_user_question（内联卡片）
  pendingQuestion: AskUserQuestionPayload | null;

  // ========== 中间产物 ==========
  plan: SkillDevPlan | null;
  evalResult: SkillDevEvalResult | null;
  report: SkillDevReport | null;

  // ========== Actions ==========
  reset: () => void;
  setTaskId: (id: string | null) => void;
  setStage: (stage: SkillDevStage) => void;
  setMode: (mode: SkillDevTaskMode) => void;
  setIteration: (iteration: number) => void;
  setQuery: (query: string) => void;
  setProcessing: (processing: boolean) => void;
  setSuspended: (suspended: boolean) => void;
  setError: (error: string | null) => void;

  setTodos: (todos: SkillDevTodo[]) => void;
  updateTodoStatus: (id: string, status: SkillDevTodo['status']) => void;

  setArtifacts: (artifacts: SkillDevArtifact[]) => void;
  addArtifact: (artifact: SkillDevArtifact) => void;

  setClarifyQuestions: (questions: ClarifyQuestion[] | null) => void;
  setClarifySubmitted: (submitted: boolean) => void;
  setPendingQuestion: (question: AskUserQuestionPayload | null) => void;
  /** 终止任务时清理挂起的交互（审批卡片 / ask_user_question） */
  dismissPendingInteraction: () => void;

  addMessage: (message: SkillDevMessage) => void;
  addThinkingMessage: (delta: string, options?: { timestamp?: string; id?: string }) => void;
  addOutputMessage: (delta: string, options?: { timestamp?: string; id?: string }) => void;
  clearMessages: () => void;
  updateMessage: (id: string, updates: Partial<SkillDevMessage>) => void;
  appendStreamContent: (messageId: string, content: string) => void;
  appendToAgentRunCard: (messageId: string, delta: string) => void;
  finalizeAgentRunCard: (messageId: string, status: 'success' | 'error' | 'timeout') => void;
  addToolCall: (toolCall: ToolCall, options?: { timestamp?: string }) => void;
  addToolResult: (toolResult: ToolResult, options?: { timestamp?: string; updatedAt?: string }) => void;

  // 审批相关 Action
  addConfirmRequestMessage: (request: ConfirmRequest, options?: { timestamp?: string; id?: string }) => void;
  resolveConfirmMessage: (
    messageId: string,
    action: string,
    feedback?: string,
    options?: { resolvedAt?: string; isSuspended?: boolean; isProcessing?: boolean }
  ) => void;

  // 思考消息处理
  endThinkingStream: () => void;

  setFileTree: (tree: FileTreeNode[]) => void;
  setCurrentFile: (file: { path: string; content: string } | null) => void;

  setPlan: (plan: SkillDevPlan | null) => void;
  setEvalResult: (result: SkillDevEvalResult | null) => void;
  setReport: (report: SkillDevReport | null) => void;
  hydrateFromSkillDevRestore: (result: SkillDevRestoreResult) => void;
}

/** SkillDev 消息类型 - 扩展支持审批卡片和思考块 */
export type SkillDevMessageType =
  | 'text'
  | 'thinking'
  | 'thinking_block'      // 可折叠思考块
  | 'progress'
  | 'test_progress'
  | 'agent_run_card'      // TEST_RUN 阶段每个测试用例的独立执行卡片
  | 'skill_name_ready'
  | 'validate_result'
  | 'eval_ready'
  | 'desc_opt_ready'
  | 'error'
  | 'tool_call'
  | 'tool_result'
  | 'confirm_request'     // 审批请求卡片
  | 'confirm_resolved';   // 审批已完成卡片

/** SkillDev 消息 metadata 扩展字段 */
export interface SkillDevMessageMetadata {
  // 现有字段
  stage?: string;
  benchmark?: unknown;
  iteration?: number;
  valid?: boolean;
  message?: string;
  before?: string;
  after?: string;
  skill_name?: string;
  best_score?: string;
  iterations_run?: number;
  history?: unknown[];

  // 审批相关
  confirmType?: ConfirmRequest['confirm_type'];
  confirmTitle?: string;
  confirmMessage?: string;
  confirmActions?: ConfirmRequest['actions'];
  confirmData?: ConfirmRequest['data'];
  resolvedAction?: string;
  resolvedAt?: string;
  resolvedFeedback?: string;

  // 思考块
  collapsed?: boolean;
  summary?: string;

  // agent_run_card（TEST_RUN / EVALUATE 每个测试用例）
  caseKey?: string;       // "test_run/{case_name}/{variant}" 或 "evaluate_grader/..."
  caseName?: string;
  caseVariant?: string;
  caseStatus?: 'running' | 'success' | 'error' | 'timeout';
  caseOutput?: string;    // 累积的流式输出
  casePrompt?: string;    // 测试用例的 query（展示在卡片顶部）
}

/** SkillDev 消息类型 - 完全兼容 ChatPanel 的 Message 特性 */
export interface SkillDevMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  type: SkillDevMessageType;
  timestamp: string;
  isStreaming?: boolean;
  toolCall?: ToolCall;
  toolResult?: ToolResult;
  audioBase64?: string;
  audioMime?: string;
  mediaItems?: MediaItem[];
  metadata?: SkillDevMessageMetadata;
}

const initialState = {
  taskId: null,
  stage: 'init' as SkillDevStage,
  mode: 'create' as SkillDevTaskMode,
  iteration: 0,
  query: '',
  isProcessing: false,
  isSuspended: false,
  error: null,

  todos: [],
  artifacts: [],
  messages: [],
  toolExecutions: new Map<string, ToolExecution>(),
  toolExecutionOrder: [],
  orphanResults: new Map<string, ToolResult>(),
  _thinkingBreakPending: false,
  fileTree: [],
  currentFile: null,
  clarifyQuestions: null as ClarifyQuestion[] | null,
  isClarifySubmitted: false,
  pendingQuestion: null as AskUserQuestionPayload | null,

  plan: null,
  evalResult: null,
  report: null,
};

/**
 * 将最后一条流式 thinking 消息收口为 isStreaming: false。
 * 在插入非 thinking 消息（普通消息、工具调用/结果、审批卡片）之前调用，
 * 使当前 thinking 停止流式光标，但保留 type: 'thinking' 直到 endThinkingStream 折叠。
 */
function finalizeLatestThinking(messages: SkillDevMessage[]): SkillDevMessage[] {
  if (messages.length === 0) return messages;
  const last = messages[messages.length - 1];
  if (last.type === 'thinking' && last.isStreaming) {
    return [
      ...messages.slice(0, -1),
      { ...last, isStreaming: false },
    ];
  }
  return messages;
}

/**
 * 将最后一条流式 text 输出消息收口为 isStreaming: false。
 * 在插入 thinking / 工具调用/结果 / 审批卡片之前调用，使当前输出气泡停止流式光标。
 */
function finalizeLatestOutput(messages: SkillDevMessage[]): SkillDevMessage[] {
  if (messages.length === 0) return messages;
  const last = messages[messages.length - 1];
  if (last.type === 'text' && last.role === 'assistant' && last.isStreaming) {
    return [
      ...messages.slice(0, -1),
      { ...last, isStreaming: false },
    ];
  }
  return messages;
}

export const useSkillDevStore = create<SkillDevStateStore>((set, _get) => ({
  ...initialState,

  reset: () => set(initialState),

  setTaskId: (id) => set({ taskId: id }),
  setStage: (stage) => set({ stage }),
  setMode: (mode) => set({ mode }),
  setIteration: (iteration) => set({ iteration }),
  setQuery: (query) => set({ query }),
  setProcessing: (isProcessing) => set({ isProcessing }),
  setSuspended: (isSuspended) => set({ isSuspended }),
  setError: (error) => set({ error }),

  setTodos: (todos) => set({ todos }),
  updateTodoStatus: (id, status) =>
    set((state) => ({
      todos: state.todos.map((todo) =>
        todo.id === id ? { ...todo, status } : todo
      ),
    })),

  setArtifacts: (artifacts) => set({ artifacts }),
  addArtifact: (artifact) =>
    set((state) => ({
      artifacts: [...state.artifacts, artifact],
    })),

  setClarifyQuestions: (clarifyQuestions) => set({ clarifyQuestions }),
  setClarifySubmitted: (isClarifySubmitted) => set({ isClarifySubmitted }),
  setPendingQuestion: (pendingQuestion) => set({ pendingQuestion }),

  dismissPendingInteraction: () =>
    set((state) => {
      const now = new Date().toISOString();
      const messages = state.messages.map((msg) => {
        if (msg.type !== 'confirm_request') {
          return msg;
        }
        return {
          ...msg,
          type: 'confirm_resolved' as const,
          content: `${msg.content}\n\n（任务已终止）`,
          metadata: {
            ...msg.metadata,
            resolvedAction: 'cancelled',
            resolvedAt: now,
          },
        };
      });
      return {
        messages,
        pendingQuestion: null,
        clarifyQuestions: null,
        isClarifySubmitted: false,
        isSuspended: false,
      };
    }),

  addMessage: (message) =>
    set((state) => ({
      messages: [...finalizeLatestThinking(finalizeLatestOutput(state.messages)), message],
    })),

  addThinkingMessage: (delta, options) =>
    set((state) => {
      const ts = options?.timestamp ?? new Date().toISOString();
      const forcedId = options?.id;
      // 先收口任何活跃的输出消息，再检查 thinking 状态
      const messages = finalizeLatestOutput([...state.messages]);
      const lastMsg = messages[messages.length - 1];
      const canAppend =
        !state._thinkingBreakPending &&
        lastMsg &&
        lastMsg.type === 'thinking' &&
        lastMsg.role === 'assistant';

      if (canAppend) {
        // 追加内容，保持 isStreaming: true
        return {
          _thinkingBreakPending: false,
          messages: [
            ...messages.slice(0, -1),
            { ...lastMsg, content: lastMsg.content + delta, isStreaming: true },
          ],
        };
      } else {
        // 创建新的思考消息，标记为流式中
        return {
          _thinkingBreakPending: false,
          messages: [
            ...messages,
            {
              id: forcedId || `thinking-${Date.now()}`,
              role: 'assistant' as const,
              content: delta,
              type: 'thinking' as const,
              isStreaming: true,
              timestamp: ts,
            },
          ],
        };
      }
    }),

  addOutputMessage: (delta, options) =>
    set((state) => {
      const ts = options?.timestamp ?? new Date().toISOString();
      const forcedId = options?.id;
      // 先收口任何活跃的 thinking 消息，再检查输出状态
      const messages = finalizeLatestThinking([...state.messages]);
      const lastMsg = messages[messages.length - 1];
      // 可追加的条件：不处于 thinking 中断挂起、上一条是流式输出气泡
      const canAppend =
        !state._thinkingBreakPending &&
        lastMsg &&
        lastMsg.type === 'text' &&
        lastMsg.role === 'assistant' &&
        lastMsg.isStreaming === true;

      if (canAppend) {
        return {
          _thinkingBreakPending: false,
          messages: [
            ...messages.slice(0, -1),
            { ...lastMsg, content: lastMsg.content + delta },
          ],
        };
      } else {
        return {
          _thinkingBreakPending: false,
          messages: [
            ...messages,
            {
              id: forcedId || `output-${Date.now()}`,
              role: 'assistant' as const,
              content: delta,
              type: 'text' as const,
              isStreaming: true,
              timestamp: ts,
            },
          ],
        };
      }
    }),

  clearMessages: () => set({
    messages: [],
    toolExecutions: new Map<string, ToolExecution>(),
    toolExecutionOrder: [],
    orphanResults: new Map<string, ToolResult>(),
    _thinkingBreakPending: false,
  }),

  updateMessage: (id, updates) =>
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === id ? { ...msg, ...updates } : msg
      ),
    })),

  appendStreamContent: (messageId, content) =>
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === messageId ? { ...msg, content: msg.content + content } : msg
      ),
    })),

  appendToAgentRunCard: (messageId, delta) =>
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === messageId
          ? {
              ...msg,
              metadata: {
                ...msg.metadata,
                caseOutput: (msg.metadata?.caseOutput ?? '') + delta,
              },
            }
          : msg
      ),
    })),

  finalizeAgentRunCard: (messageId, status) =>
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === messageId
          ? {
              ...msg,
              isStreaming: false,
              metadata: {
                ...msg.metadata,
                caseStatus: status,
              },
            }
          : msg
      ),
    })),

  addToolCall: (toolCall, options) =>
    set((state) => {
      if (!toolCall.id || state.toolExecutions.has(toolCall.id)) {
        return state;
      }
      const now = options?.timestamp || new Date().toISOString();
      const nextExecutions = new Map(state.toolExecutions);
      const nextOrphan = new Map(state.orphanResults);
      const orphanResult = nextOrphan.get(toolCall.id);
      if (orphanResult) {
        nextOrphan.delete(toolCall.id);
      }
      nextExecutions.set(toolCall.id, {
        toolCallId: toolCall.id,
        toolCall,
        result: orphanResult,
        status: orphanResult ? (orphanResult.success ? 'completed' : 'error') : 'pending',
        startedAt: now,
        updatedAt: now,
        timeoutAt: now,
      });
      return {
        ...state,
        messages: finalizeLatestOutput(finalizeLatestThinking(state.messages)),
        toolExecutions: nextExecutions,
        toolExecutionOrder: [...state.toolExecutionOrder, toolCall.id],
        orphanResults: nextOrphan,
        _thinkingBreakPending: true,
      };
    }),

  addToolResult: (toolResult, options) =>
    set((state) => {
      const toolCallId = toolResult.toolCallId;
      if (!toolCallId) {
        return state;
      }
      const finalizedMessages = finalizeLatestOutput(finalizeLatestThinking(state.messages));
      const existing = state.toolExecutions.get(toolCallId);
      if (!existing) {
        const nextOrphan = new Map(state.orphanResults);
        nextOrphan.set(toolCallId, toolResult);
        return { ...state, messages: finalizedMessages, orphanResults: nextOrphan, _thinkingBreakPending: true };
      }
      const nextExecutions = new Map(state.toolExecutions);
      nextExecutions.set(toolCallId, {
        ...existing,
        result: toolResult,
        status: toolResult.success ? 'completed' : 'error',
        updatedAt: options?.updatedAt || options?.timestamp || new Date().toISOString(),
      });
      return { ...state, messages: finalizedMessages, toolExecutions: nextExecutions, _thinkingBreakPending: true };
    }),

  // 审批请求消息：插入消息流，不再触发弹窗
  addConfirmRequestMessage: (request, options) =>
    set((state) => ({
      messages: [
        ...finalizeLatestOutput(finalizeLatestThinking(state.messages)),
        {
          id: options?.id || `confirm-${Date.now()}`,
          role: 'system' as const,
          content: request.message,
          type: 'confirm_request' as const,
          timestamp: options?.timestamp || new Date().toISOString(),
          metadata: {
            confirmType: request.confirm_type,
            confirmTitle: request.title,
            confirmMessage: request.message,
            confirmActions: request.actions,
            confirmData: request.data,
          },
        },
      ],
      // 不再设置 confirmRequest，审批完全走消息流内联卡片
      // confirmRequest: null,  // 可选：保留弹窗用于"详情放大查看"功能
    })),

  // 审批完成：更新消息状态而非删除
  resolveConfirmMessage: (messageId, action, feedback, options) =>
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === messageId
          ? {
              ...msg,
              type: 'confirm_resolved',
              content: feedback
                ? `${msg.content}\n\n已选择: ${action}\n反馈: ${feedback}`
                : `${msg.content}\n\n已选择: ${action}`,
              metadata: {
                ...msg.metadata,
                resolvedAction: action,
                resolvedAt: options?.resolvedAt || new Date().toISOString(),
                resolvedFeedback: feedback,
              },
            }
          : msg
      ),
      isSuspended: options?.isSuspended ?? false,
      isProcessing: options?.isProcessing ?? true,
    })),

  // 结束思考流：将所有未完成的 thinking 消息转为 thinking_block，并收口任何活跃的输出流
  endThinkingStream: () =>
    set((state) => {
      let changed = false;
      const messages = state.messages.map((msg) => {
        // 收口活跃的输出气泡
        if (msg.type === 'text' && msg.role === 'assistant' && msg.isStreaming) {
          changed = true;
          return { ...msg, isStreaming: false };
        }
        if (msg.type !== 'thinking' || msg.role !== 'assistant') {
          return msg;
        }
        changed = true;
        const content = msg.content;
        const lines = content.split('\n').filter(Boolean);
        const summary =
          lines.length > 3
            ? lines.slice(0, 3).join('\n') + '...'
            : content.slice(0, 100) + (content.length > 100 ? '...' : '');
        return {
          ...msg,
          type: 'thinking_block' as const,
          isStreaming: false,
          metadata: { ...msg.metadata, summary, collapsed: true },
        };
      });
      if (!changed) return state;
      return { messages };
    }),

  setFileTree: (fileTree) => set({ fileTree }),
  setCurrentFile: (currentFile) => set({ currentFile }),

  setPlan: (plan) => set({ plan }),
  setEvalResult: (evalResult) => set({ evalResult }),
  setReport: (report) => set({ report }),
  hydrateFromSkillDevRestore: (result) =>
    set((state) => {
      const snapshot = result.snapshot;
      const agentStage =
        snapshot.runner === 'agent' && snapshot.status
          ? snapshot.status
          : snapshot.stage;
      return {
        ...initialState,
        taskId: snapshot.task_id,
        stage: agentStage as typeof initialState.stage,
        mode: snapshot.mode ?? state.mode,
        iteration: typeof snapshot.iteration === 'number' ? snapshot.iteration : 0,
        isSuspended: Boolean(snapshot.is_suspended),
        isProcessing: Boolean(snapshot.is_processing),
        todos: Array.isArray(snapshot.todos) ? snapshot.todos : [],
        artifacts: Array.isArray(snapshot.artifacts) ? snapshot.artifacts : [],
        error: typeof snapshot.error === 'string' ? snapshot.error : null,
      };
    }),
}));
