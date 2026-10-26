/**
 * Todo 状态管理（多 session 版本）
 *
 * Todo 列表按 session 隔离存储在 runtimes 中。
 */

import { create } from 'zustand';
import { TodoItem, TodoStatus } from '../types';

interface TodoRuntime {
  todos: TodoItem[];
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
      if (!runtime) return state;
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

  addTodo: (sessionId, todo) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
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
      if (!runtime) return state;
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
      if (!runtime) return state;
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
      if (!runtime) return state;
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
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, todos: [] },
        },
      };
    });
  },
}));
