import type { TodoItem } from "../../core/types.js";
import { padToWidth } from "../rendering/text.js";
import { chalk, palette } from "../theme.js";

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

function todoLabel(todo: TodoItem, animationPhase: number = 0): string {
  const spinner = ["◐", "◓", "◑", "◒"][animationPhase % 4]!;
  const text = normalizeTodoText(todo.activeForm || todo.content);
  if (todo.status === "completed") {
    return `${palette.status.success("✓")} ${chalk.strikethrough(palette.text.dim(text))}`;
  }
  if (todo.status === "error") {
    return `${palette.status.error("✗")} ${text}`;
  }
  const prefix =
    todo.status === "in_progress" ? palette.status.info(spinner) : palette.status.warning("○");
  return `${prefix} ${text}`;
}

function todoLine(todo: TodoItem, index: number, width: number, animationPhase: number): string {
  const prefix = index === 0 ? palette.text.accent("→ ") : "  ";
  return padToWidth(prefix + todoLabel(todo, animationPhase), width);
}

export function renderTodoList(
  todos: TodoItem[],
  width: number,
  collapsed: boolean = false,
  animationPhase: number = 0,
): string[] {
  if (todos.length === 0) {
    return [];
  }

  const pendingCount = todos.filter((t) => t.status === "pending").length;
  const inProgressCount = todos.filter((t) => t.status === "in_progress").length;
  const completedCount = todos.filter((t) => t.status === "completed").length;
  const errorCount = todos.filter((t) => t.status === "error").length;

  const statusText = [
    inProgressCount > 0 ? `${inProgressCount} in progress` : "",
    errorCount > 0 ? `${errorCount} error` : "",
    pendingCount > 0 ? `${pendingCount} pending` : "",
    completedCount > 0 ? `${completedCount} completed` : "",
  ]
    .filter(Boolean)
    .join(", ");

  const headerLine = collapsed
    ? `Todo [${statusText}] ▸`
    : palette.text.secondary("Todo") + ` [${statusText}]`;

  if (collapsed) {
    return [padToWidth(headerLine, width), " ".repeat(width)];
  }

  const ordered = [
    ...todos.filter((todo) => todo.status === "in_progress"),
    ...todos.filter((todo) => todo.status === "error"),
    ...todos.filter((todo) => todo.status === "pending"),
    ...todos.filter((todo) => todo.status === "completed"),
  ];

  const visibleTodos = ordered.slice(0, 8);
  const todoLines = visibleTodos.map((todo, index) => todoLine(todo, index, width, animationPhase));
  if (ordered.length > visibleTodos.length) {
    todoLines.push(padToWidth(palette.text.dim(`  (1-${visibleTodos.length}/${ordered.length})`), width));
  }

  return [padToWidth(headerLine, width), ...todoLines, " ".repeat(width)];
}
