import { SelectList } from "@mariozechner/pi-tui";
import type { TodoItem } from "../../core/types.js";
import { padToWidth } from "../rendering/text.js";
import { palette, selectListTheme } from "../theme.js";

function normalizeTodoText(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) {
    return trimmed;
  }
  return trimmed
    .replace(/^Executing\s+/i, "")
    .replace(/^Running\s+/i, "")
    .replace(/^Calling\s+/i, "")
    .replace(/^Using\s+/i, "")
    .replace(/^Working on\s+/i, "")
    .replace(/^Processing\s+/i, "")
    .replace(/^Reading\s+/i, "")
    .replace(/^Searching\s+/i, "")
    .replace(/^Fetching\s+/i, "")
    .replace(/^Writing\s+/i, "")
    .replace(/^Editing\s+/i, "")
    .replace(/^正在调用\s+/u, "")
    .replace(/^正在/u, "")
    .replace(/(?:\.\.\.|…)\s*$/u, "");
}

function todoLabel(todo: TodoItem): string {
  const prefix = todo.status === "in_progress" ? "●" : todo.status === "completed" ? "✓" : "○";
  return `${prefix} ${normalizeTodoText(todo.activeForm || todo.content)}`;
}

export function renderTodoList(todos: TodoItem[], width: number): string[] {
  if (todos.every((todo) => todo.status === "completed")) {
    return [];
  }

  const ordered = [
    ...todos.filter((todo) => todo.status === "in_progress"),
    ...todos.filter((todo) => todo.status === "pending"),
    ...todos.filter((todo) => todo.status === "completed"),
  ];

  const list = new SelectList(
    ordered.map((todo) => ({
      value: todo.id,
      label: todoLabel(todo),
    })),
    Math.min(Math.max(ordered.length, 1), 8),
    selectListTheme,
  );

  return [
    padToWidth(palette.text.secondary("Todo"), width),
    ...list.render(width),
    " ".repeat(width),
  ];
}
