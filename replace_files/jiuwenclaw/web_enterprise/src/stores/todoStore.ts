/**
 * Todo 状态管理
 */

import { create } from 'zustand';
import { TodoItem, TodoStatus } from '../types';

interface TodoState {
  todos: TodoItem[];

  // Actions
  setTodos: (todos: TodoItem[]) => void;
  addTodo: (todo: TodoItem) => void;
  updateTodo: (id: string, updates: Partial<TodoItem>) => void;
  updateTodoStatus: (id: string, status: TodoStatus) => void;
  removeTodo: (id: string) => void;
  clearTodos: () => void;
}

export const useTodoStore = create<TodoState>((set) => ({
  todos: [],

  setTodos: (todos) => {
    set({ todos });
  },

  addTodo: (todo) => {
    set((state) => ({
      todos: [...state.todos, todo],
    }));
  },

  updateTodo: (id, updates) => {
    set((state) => ({
      todos: state.todos.map((todo) =>
        todo.id === id || todo.id.startsWith(id)
          ? { ...todo, ...updates, updatedAt: new Date().toISOString() }
          : todo
      ),
    }));
  },

  updateTodoStatus: (id, status) => {
    set((state) => ({
      todos: state.todos.map((todo) =>
        todo.id === id || todo.id.startsWith(id)
          ? { ...todo, status, updatedAt: new Date().toISOString() }
          : todo
      ),
    }));
  },

  removeTodo: (id) => {
    set((state) => ({
      todos: state.todos.filter((todo) => todo.id !== id && !todo.id.startsWith(id)),
    }));
  },

  clearTodos: () => {
    set({ todos: [] });
  },
}));
