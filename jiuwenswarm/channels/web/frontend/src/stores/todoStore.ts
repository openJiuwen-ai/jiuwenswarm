/**
 * Todo 状态管理（多 session 版本）
 *
 * Todo 列表按 session 隔离存储在 runtimes 中。
 */

import { create } from 'zustand';
import { TodoItem, TodoStatus } from '../types';
import { decideTodoSnapshot, parseTodoMeta, todoFingerprint, TodoSnapshotMeta } from './todoSnapshot';

interface TodoRuntime {
  todos: TodoItem[];
  snapshotMeta?: TodoSnapshotMeta;
  snapshotFingerprint?: string;
  needsSync?: boolean;
  recoveryRequestId?: string;
  recoveryBase?: string;
  recoveryAttempts?: number;
}

function createEmptyRuntime(): TodoRuntime {
  return { todos: [] };
}

interface TodoState {
  runtimes: Record<string, TodoRuntime>;

  ensureRuntime: (sessionId: string) => TodoRuntime;
  getRuntime: (sessionId: string | null) => TodoRuntime | undefined;
  removeRuntime: (sessionId: string) => void;

  setTodos: (sessionId: string, todos: TodoItem[]) => void;
  applySnapshot: (sessionId: string, payload: Record<string, unknown>, requestId?: string) => boolean;
  resetRecovery: () => void;
  beginRecovery: (sessionId: string) => string | undefined;
  finishRecovery: (sessionId: string, requestId: string) => void;
  setActiveForm: (sessionId: string, id: string, activeForm: string) => void;
  addTodo: (sessionId: string, todo: TodoItem) => void;
  updateTodo: (sessionId: string, id: string, updates: Partial<TodoItem>) => void;
  updateTodoStatus: (sessionId: string, id: string, status: TodoStatus) => void;
  removeTodo: (sessionId: string, id: string) => void;
  clearTodos: (sessionId: string) => void;
}

export const useTodoStore = create<TodoState>((set, get) => ({
  runtimes: {},

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

  removeRuntime: (sessionId) => {
    set((state) => {
      const next = { ...state.runtimes };
      delete next[sessionId];
      return { runtimes: next };
    });
  },

  setTodos: (sessionId, todos) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      const prevById = new Map<string, TodoItem>();
      runtime.todos.forEach((todo) => {
        if (!prevById.has(todo.id)) prevById.set(todo.id, todo);
      });
      // 后端快照不含 updatedAt：在转入 in_progress 时记一个本地基准，
      // 维持 in_progress 时保留既有基准，避免整盘重发把进度计时清零。
      const mergedTodos = todos.map((todo) => {
        const prev = prevById.get(todo.id);
        const wasInProgress = prev?.status === 'in_progress';
        if (todo.status === 'in_progress' && !wasInProgress) {
          return { ...todo, updatedAt: new Date().toISOString() };
        }
        if (todo.status === 'in_progress' && prev?.updatedAt) {
          return { ...todo, updatedAt: prev.updatedAt };
        }
        return todo;
      });
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, todos: mergedTodos },
        },
      };
    });
  },

  applySnapshot: (sessionId, payload, requestId) => {
    let accepted = false;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const meta = parseTodoMeta(payload.todo_meta);
      const raw = payload.todos;
      const valid = Array.isArray(raw) && raw.every((item) => item && typeof item === 'object' &&
        typeof item.id === 'string' && typeof item.content === 'string' &&
        ['pending', 'in_progress', 'completed'].includes(item.status));
      if (!valid || (payload.todo_meta != null && !meta)) {
        return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, needsSync: true } } };
      }
      if (meta && meta.view !== 'display') return state;
      const todos = raw as TodoItem[];
      const fingerprint = todoFingerprint(todos);
      const authority = Boolean(requestId && requestId === runtime.recoveryRequestId &&
        runtime.recoveryBase === JSON.stringify(runtime.snapshotMeta));
      const decision = decideTodoSnapshot(runtime.snapshotMeta, meta, runtime.snapshotFingerprint ?? '', fingerprint, authority);
      if (decision === 'stale') return state;
      if (decision === 'resync') {
        return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, needsSync: true } } };
      }
      if (decision === 'duplicate') {
        return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, needsSync: false, recoveryAttempts: 0 } } };
      }
      accepted = true;
      const samePlan = runtime.snapshotMeta?.store_id === meta?.store_id && runtime.snapshotMeta?.plan_id === meta?.plan_id;
      const previous = new Map((samePlan ? runtime.todos : []).map((todo) => [todo.id, todo]));
      const merged = todos.map((todo) => {
        const before = previous.get(todo.id);
        return todo.status === 'in_progress' ? { ...todo, updatedAt: before?.status === 'in_progress' && before.updatedAt
          ? before.updatedAt : new Date().toISOString() } : todo;
      });
      return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, todos: merged, snapshotMeta: meta,
        snapshotFingerprint: fingerprint, needsSync: false, recoveryAttempts: 0 } } };
    });
    return accepted;
  },

  resetRecovery: () => set((state) => ({ runtimes: Object.fromEntries(Object.entries(state.runtimes).map(([id, runtime]) =>
    [id, { ...runtime, recoveryAttempts: 0, recoveryRequestId: undefined, recoveryBase: undefined,
      needsSync: runtime.needsSync || Boolean(runtime.snapshotMeta) }])) })),

  beginRecovery: (sessionId) => {
    const runtime = get().runtimes[sessionId];
    if (!runtime?.needsSync || runtime.recoveryRequestId || (runtime.recoveryAttempts ?? 0) >= 3) return undefined;
    const nonce = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const requestId = `todo-recovery-${nonce}`;
    set((state) => ({ runtimes: { ...state.runtimes, [sessionId]: { ...runtime, recoveryRequestId: requestId,
      recoveryBase: JSON.stringify(runtime.snapshotMeta), recoveryAttempts: (runtime.recoveryAttempts ?? 0) + 1 } } }));
    return requestId;
  },

  finishRecovery: (sessionId, requestId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.recoveryRequestId !== requestId) return state;
      return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, recoveryRequestId: undefined, recoveryBase: undefined } } };
    });
  },

  setActiveForm: (sessionId, id, activeForm) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime,
        todos: runtime.todos.map((todo) => todo.id === id ? { ...todo, activeForm } : todo) } } };
    });
  },

  addTodo: (sessionId, todo) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, todos: [...runtime.todos, todo] },
        },
      };
    });
  },

  updateTodo: (sessionId, id, updates) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            todos: runtime.todos.map((todo) =>
              todo.id === id || todo.id.startsWith(id)
                ? { ...todo, ...updates, updatedAt: new Date().toISOString() }
                : todo
            ),
          },
        },
      };
    });
  },

  updateTodoStatus: (sessionId, id, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            todos: runtime.todos.map((todo) =>
              todo.id === id || todo.id.startsWith(id)
                ? { ...todo, status, updatedAt: new Date().toISOString() }
                : todo
            ),
          },
        },
      };
    });
  },

  removeTodo: (sessionId, id) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            todos: runtime.todos.filter((todo) => todo.id !== id && !todo.id.startsWith(id)),
          },
        },
      };
    });
  },

  clearTodos: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime || runtime.snapshotMeta) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, todos: [] },
        },
      };
    });
  },
}));
