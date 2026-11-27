/**
 * 聊天状态管理
 */

import { create } from 'zustand';
import {
  Message,
  ToolCall,
  ToolResult,
  ToolExecution,
  ToolExecutionStatus,
  InterruptResultPayload,
  SubtaskUpdatePayload,
  AskUserQuestionPayload,
  UsageSummary,
} from '../types';
import { useTodoStore } from './todoStore';

const TOOL_TIMEOUT_MS = 12_000_000;

function computeTimeoutAt(baseIso: string): string {
  return new Date(Date.parse(baseIso) + TOOL_TIMEOUT_MS).toISOString();
}

function resolveExecutionStatus(result: ToolResult): ToolExecutionStatus {
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

interface ChatState {
  messages: Message[];
  isProcessing: boolean;
  isThinking: boolean;  // 思考中状态（显示闪烁动画）
  isPaused: boolean;    // 任务是否暂停
  pausedTask: string | null;  // 暂停的任务描述
  interruptResult: InterruptResultPayload | null;  // 最近的中断结果
  currentStreamContent: string;
  currentStreamId: string | null;
  streamBuffers: Map<string, string>;
  activeSubtasks: Map<string, SubtaskState>;  // 活跃的子任务
  toolExecutions: Map<string, ToolExecution>;
  toolExecutionOrder: string[];
  orphanResults: Map<string, ToolResult>;
  toolMetrics: {
    toolCallDedupDropped: number;
    toolResultDedupDropped: number;
  };
  // 任务队列
  taskQueue: TaskItem[];
  // 用户问题相关
  pendingQuestion: AskUserQuestionPayload | null;  // 待回答的问题
  // 输入框内容
  inputValue: string;

  // Actions
  addMessage: (message: Message) => void;
  updateMessage: (id: string, updates: Partial<Message>) => void;
  appendStreamContent: (content: string, streamKey?: string) => void;
  startStreaming: (messageId: string, streamKey?: string) => void;
  stopStreaming: (streamKey?: string) => void;
  setProcessing: (status: boolean) => void;
  setThinking: (status: boolean) => void;
  setPaused: (paused: boolean, task?: string | null) => void;
  setInterruptResult: (result: InterruptResultPayload | null) => void;
  addToolCall: (toolCall: ToolCall, options?: { startedAt?: string }) => void;
  addToolResult: (toolResult: ToolResult, options?: { updatedAt?: string }) => void;
  markTimedOutExecutions: () => void;
  updateSubtask: (payload: SubtaskUpdatePayload) => void;
  clearSubtasks: () => void;
  clearMessages: () => void;
  /** 在列表头部插入更早的历史消息（数组内建议时间升序） */
  prependMessages: (olderFirst: Message[]) => void;
  // 任务队列相关
  addToTaskQueue: (content: string) => void;
  clearTaskQueue: () => void;
  removeFromTaskQueue: (id: string) => void;
  // 用户问题相关
  setPendingQuestion: (question: AskUserQuestionPayload | null) => void;
  // 输入框相关
  setInputValue: (value: string) => void;
  // Usage summary
  setUsageSummary: (messageId: string, usage: UsageSummary) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  isProcessing: false,
  isThinking: false,
  isPaused: false,
  pausedTask: null,
  interruptResult: null,
  currentStreamContent: '',
  currentStreamId: null,
  streamBuffers: new Map(),
  activeSubtasks: new Map(),
  toolExecutions: new Map(),
  toolExecutionOrder: [],
  orphanResults: new Map(),
  toolMetrics: {
    toolCallDedupDropped: 0,
    toolResultDedupDropped: 0,
  },
  taskQueue: [],
  pendingQuestion: null,
  inputValue: '',

  addMessage: (message) => {
    set((state) => ({
      messages: [...state.messages, message],
    }));
  },

  updateMessage: (id, updates) => {
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === id ? { ...msg, ...updates } : msg
      ),
    }));
  },

  appendStreamContent: (content, streamKey = 'default') => {
    const { currentStreamId } = get();
    if (!currentStreamId) return;

    const existingBuffer = get().streamBuffers.get(streamKey) || '';
    const nextContent = existingBuffer + content;

    set((state) => ({
      currentStreamContent: nextContent,
      streamBuffers: new Map(state.streamBuffers).set(streamKey, nextContent),
      messages: state.messages.map((msg) =>
        msg.id === currentStreamId
          ? { ...msg, content: nextContent }
          : msg
      ),
    }));
  },

  startStreaming: (messageId, streamKey = 'default') => {
    set((state) => ({
      currentStreamId: messageId,
      currentStreamContent: '',
      streamBuffers: new Map(state.streamBuffers).set(streamKey, ''),
    }));
  },

  stopStreaming: (streamKey = 'default') => {
    const { currentStreamId } = get();
    if (currentStreamId) {
      set((state) => ({
        messages: state.messages.map((msg) =>
          msg.id === currentStreamId ? { ...msg, isStreaming: false } : msg
        ),
        currentStreamId: null,
        currentStreamContent: '',
        streamBuffers: new Map(state.streamBuffers).set(streamKey, ''),
      }));
    }
  },

  setProcessing: (status) => {
    set({ isProcessing: status });
  },

  setThinking: (status) => {
    set({ isThinking: status });
  },

  setPaused: (paused, task = null) => {
    set({ isPaused: paused, pausedTask: task ?? null });
  },

  setInterruptResult: (result) => {
    set({ interruptResult: result });
    // 3 秒后自动清除中断结果提示
    if (result) {
      setTimeout(() => {
        set((state) => {
          // 只有当前结果没有变化时才清除
          if (state.interruptResult === result) {
            return { interruptResult: null };
          }
          return {};
        });
      }, 3000);
    }
  },

  addToolCall: (toolCall, options) => {
    set((state) => {
      if (!toolCall.id) {
        const nextDropped = state.toolMetrics.toolCallDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolCallDedupDropped', {
            count: nextDropped,
            reason: 'missing toolCallId',
          });
        }
        return {
          ...state,
          toolMetrics: {
            ...state.toolMetrics,
            toolCallDedupDropped: nextDropped,
          },
        };
      }
      if (state.toolExecutions.has(toolCall.id)) {
        const nextDropped = state.toolMetrics.toolCallDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolCallDedupDropped', {
            count: nextDropped,
            reason: 'toolCallId execution hit',
          });
        }
        return {
          ...state,
          toolMetrics: {
            ...state.toolMetrics,
            toolCallDedupDropped: nextDropped,
          },
        };
      }
      const nowIso = new Date().toISOString();
      const startedAt =
        typeof options?.startedAt === 'string' && options.startedAt.trim()
          ? options.startedAt.trim()
          : nowIso;
      const orphanResult = state.orphanResults.get(toolCall.id);
      const nextExecutions = new Map(state.toolExecutions);
      const nextOrphanResults = new Map(state.orphanResults);
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
      });

      const nextOrder = [...state.toolExecutionOrder, toolCall.id];
      return {
        toolExecutions: nextExecutions,
        toolExecutionOrder: nextOrder,
        orphanResults: nextOrphanResults,
      };
    });
  },

  addToolResult: (toolResult, options) => {
    set((state) => {
      const incomingToolCallId = toolResult.toolCallId;
      if (!incomingToolCallId) {
        const nextDropped = state.toolMetrics.toolResultDedupDropped + 1;
        if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
          console.debug('[ws][metrics] toolResultDedupDropped', {
            count: nextDropped,
            reason: 'missing toolCallId',
          });
        }
        return {
          ...state,
          toolMetrics: {
            ...state.toolMetrics,
            toolResultDedupDropped: nextDropped,
          },
        };
      }
      const nowIso = new Date().toISOString();
      const updatedAt =
        typeof options?.updatedAt === 'string' && options.updatedAt.trim()
          ? options.updatedAt.trim()
          : nowIso;
      const existingExecution = state.toolExecutions.get(incomingToolCallId);

      if (!existingExecution) {
        const nextOrphanResults = new Map(state.orphanResults);
        const duplicatedOrphan = nextOrphanResults.get(incomingToolCallId);
        if (
          duplicatedOrphan &&
          duplicatedOrphan.result === toolResult.result &&
          duplicatedOrphan.success === toolResult.success &&
          (duplicatedOrphan.summary || '') === (toolResult.summary || '')
        ) {
          const nextDropped = state.toolMetrics.toolResultDedupDropped + 1;
          if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
            console.debug('[ws][metrics] toolResultDedupDropped', {
              count: nextDropped,
              reason: 'orphan duplicate',
            });
          }
          return {
            ...state,
            toolMetrics: {
              ...state.toolMetrics,
              toolResultDedupDropped: nextDropped,
            },
          };
        }
        nextOrphanResults.set(incomingToolCallId, toolResult);
        return {
          orphanResults: nextOrphanResults,
        };
      }

      if (existingExecution.result) {
        const duplicated =
          existingExecution.result.result === toolResult.result &&
          existingExecution.result.success === toolResult.success &&
          (existingExecution.result.summary || '') === (toolResult.summary || '');
        if (duplicated) {
          const nextDropped = state.toolMetrics.toolResultDedupDropped + 1;
          if (import.meta.env.DEV && (nextDropped === 1 || nextDropped % 10 === 0)) {
            console.debug('[ws][metrics] toolResultDedupDropped', {
              count: nextDropped,
              reason: 'execution duplicate',
            });
          }
          return {
            ...state,
            toolMetrics: {
              ...state.toolMetrics,
              toolResultDedupDropped: nextDropped,
            },
          };
        }
      }

      const nextExecutions = new Map(state.toolExecutions);
      const nextStatus = resolveExecutionStatus(toolResult);
      nextExecutions.set(incomingToolCallId, {
        ...existingExecution,
        result: toolResult,
        status: nextStatus,
        updatedAt,
        resultArrivedAfterTimeout:
          existingExecution.status === 'timeout' ? true : existingExecution.resultArrivedAfterTimeout,
      });
      return {
        toolExecutions: nextExecutions,
      };
    });
  },

  markTimedOutExecutions: () => {
    const now = Date.now();
    set((state) => {
      let changed = false;
      const nextExecutions = new Map(state.toolExecutions);
      for (const [toolCallId, execution] of nextExecutions) {
        if (execution.status !== 'pending') {
          continue;
        }
        const timeoutTs = Date.parse(execution.timeoutAt);
        if (Number.isNaN(timeoutTs) || timeoutTs > now) {
          continue;
        }
        changed = true;
        nextExecutions.set(toolCallId, {
          ...execution,
          status: 'timeout',
          timedOutAt: new Date(now).toISOString(),
          updatedAt: new Date(now).toISOString(),
        });
      }
      if (!changed) {
        return state;
      }
      return {
        ...state,
        toolExecutions: nextExecutions,
      };
    });
  },

  updateSubtask: (payload: SubtaskUpdatePayload) => {
    set((state) => {
      const newSubtasks = new Map(state.activeSubtasks);
      
      if (payload.status === 'completed' || payload.status === 'error') {
        // 任务完成或出错，从活跃列表中移除
        newSubtasks.delete(payload.task_id);
      } else {
        // 更新或添加子任务状态
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
      
      return { activeSubtasks: newSubtasks };
    });

    // 同时更新 todoStore 中对应任务的 activeForm（如果能匹配）
    const todoState = useTodoStore.getState();
    const { todos, setTodos } = todoState;
    
    // 尝试匹配子任务描述和 todo 内容
    const matchingTodo = todos.find(
      (todo) =>
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
        activeForm = '';  // 清除
      }
      
      if (activeForm || payload.status === 'completed') {
        const updatedTodos = todos.map((todo) =>
          todo.id === matchingTodo.id
            ? { ...todo, activeForm }
            : todo
        );
        setTodos(updatedTodos);
      }
    }
  },

  clearSubtasks: () => {
    set({ activeSubtasks: new Map() });
  },

  prependMessages: (olderFirst) => {
    if (!olderFirst.length) {
      return;
    }
    set((state) => ({
      messages: [...olderFirst, ...state.messages],
    }));
  },

  clearMessages: () => {
    set({
      messages: [],
      currentStreamContent: '',
      currentStreamId: null,
      streamBuffers: new Map(),
      isPaused: false,
      pausedTask: null,
      interruptResult: null,
      activeSubtasks: new Map(),
      toolExecutions: new Map(),
      toolExecutionOrder: [],
      orphanResults: new Map(),
      toolMetrics: {
        toolCallDedupDropped: 0,
        toolResultDedupDropped: 0,
      },
      taskQueue: [],
      pendingQuestion: null,
    });
  },

  addToTaskQueue: (content) => {
    set((state) => ({
      taskQueue: [
        ...state.taskQueue,
        {
          id: Date.now().toString() + Math.random().toString(36).substr(2, 9),
          content,
          timestamp: Date.now(),
        },
      ],
    }));
  },

  clearTaskQueue: () => {
    set({ taskQueue: [] });
  },

  removeFromTaskQueue: (id) => {
    set((state) => ({
      taskQueue: state.taskQueue.filter((task) => task.id !== id),
    }));
  },

  setPendingQuestion: (question) => {
    set({ pendingQuestion: question });
  },
  
  setInputValue: (value) => {
    set({ inputValue: value });
  },

  setUsageSummary: (messageId, usage) => {
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === messageId ? { ...msg, usageSummary: usage } : msg
      ),
    }));
  },
}));