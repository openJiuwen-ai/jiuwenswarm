import type { ToolCallDisplay } from "../../../core/types.js";
import { summarize } from "../../rendering/text.js";

export function summarizePath(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.slice(-3).join("/");
}

export function summarizeToolArguments(name: string, args: unknown): string | undefined {
  if (!args || typeof args !== "object") {
    return typeof args === "string" ? summarize(args, 72) : undefined;
  }
  const obj = args as Record<string, unknown>;
  const normalized = name.toLowerCase();
  if (normalized.includes("read") || normalized.includes("view")) {
    return summarizePath(obj.path ?? obj.file_path ?? obj.file);
  }
  if (normalized.includes("edit") || normalized.includes("write") || normalized.includes("patch")) {
    return summarizePath(obj.path ?? obj.file_path ?? obj.file) ?? "content change";
  }
  if (normalized.includes("fetch") || normalized.includes("webpage")) {
    return typeof obj.url === "string" ? summarize(obj.url, 72) : undefined;
  }
  if (normalized.includes("search") || normalized.includes("grep") || normalized.includes("glob")) {
    const pattern =
      typeof obj.pattern === "string"
        ? obj.pattern
        : typeof obj.query === "string"
          ? obj.query
          : undefined;
    const root = summarizePath(obj.path ?? obj.cwd ?? obj.root);
    return [pattern, root].filter(Boolean).join(" in ") || undefined;
  }
  if (
    normalized.includes("exec") ||
    normalized.includes("bash") ||
    normalized.includes("shell") ||
    normalized.includes("command")
  ) {
    return typeof obj.cmd === "string"
      ? summarize(obj.cmd, 72)
      : typeof obj.command === "string"
        ? summarize(obj.command, 72)
        : undefined;
  }
  if (typeof obj.path === "string") return summarizePath(obj.path);
  const keys = Object.keys(obj).filter((key) => obj[key] !== undefined);
  return keys.length > 0 ? `${keys.length} field${keys.length === 1 ? "" : "s"}` : undefined;
}

export function countLogicalLines(text: string): number {
  if (!text) return 0;
  return text.split("\n").length;
}

export function isCodeLikePath(path: string | undefined): boolean {
  if (!path) return false;
  return /\.(ts|tsx|js|jsx|py|rs|go|java|c|cc|cpp|h|hpp|json|yaml|yml|toml|md|sh|sql|css|scss|html)$/i.test(
    path,
  );
}

export function getToolFilePath(args: Record<string, unknown>): string | undefined {
  const value = args.path ?? args.file_path ?? args.file ?? args.dir_path;
  return typeof value === "string" && value ? (summarizePath(value) ?? value) : undefined;
}

export function getStringArg(args: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

export function getNumericArg(
  args: Record<string, unknown>,
  ...keys: string[]
): number | undefined {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

export function isReadTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "read" ||
    normalized === "read_file" ||
    normalized === "read_text_file" ||
    normalized === "read_memory" ||
    normalized === "memory_get" ||
    normalized === "view"
  );
}

export function isListTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return normalized === "ls" || normalized === "list_files" || normalized === "list_dir";
}

export function isWriteTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "write" ||
    normalized === "write_file" ||
    normalized === "write_text_file" ||
    normalized === "write_memory"
  );
}

export function isEditTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "edit" ||
    normalized === "edit_file" ||
    normalized === "edit_memory" ||
    normalized === "search_replace"
  );
}

export function isRunTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "bash" ||
    normalized === "shell" ||
    normalized === "sh" ||
    normalized === "powershell" ||
    normalized === "command" ||
    normalized === "exec" ||
    normalized === "run" ||
    normalized === "mcp_exec_command" ||
    normalized === "create_terminal"
  );
}

export function isGlobTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return normalized === "glob" || normalized === "glob_files" || normalized === "glob_file_search";
}

export function isSearchTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "search" ||
    normalized === "grep" ||
    normalized === "rg" ||
    normalized === "ripgrep" ||
    normalized === "memory_search" ||
    normalized === "mcp_free_search" ||
    normalized === "mcp_paid_search"
  );
}

export function isFetchTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return (
    normalized === "fetch" || normalized === "fetch_webpage" || normalized === "mcp_fetch_webpage"
  );
}

export function isMcpTool(name: string): boolean {
  const normalized = name.toLowerCase();
  return normalized.startsWith("mcp_") || normalized.startsWith("mcp.");
}

export function isToolRunning(tool: ToolCallDisplay): boolean {
  return tool.status === "running" && !tool.result;
}

export function toolDisplayName(tool: ToolCallDisplay): string {
  const name = tool.name.toLowerCase();
  if (name in { bash: true, shell: true, sh: true, powershell: true, command: true, exec: true }) {
    return "Run";
  }
  if (name in { mcp_exec_command: true, create_terminal: true }) return "Run";
  if (name in { glob: true, glob_files: true, glob_file_search: true }) return "Glob";
  if (name in { fetch: true, fetch_webpage: true, mcp_fetch_webpage: true }) return "Fetch";
  if (name.startsWith("mcp_")) {
    return `Query ${name
      .split("_")
      .slice(1)
      .filter(Boolean)
      .map((part) => part[0]?.toUpperCase() + part.slice(1))
      .join(" ")}`;
  }
  if (name.startsWith("mcp.")) {
    return `Query ${name
      .split(".")
      .slice(1)
      .filter(Boolean)
      .map((part) => part[0]?.toUpperCase() + part.slice(1))
      .join(" ")}`;
  }
  if (
    name in
    {
      read: true,
      read_file: true,
      read_memory: true,
      memory_get: true,
      view: true,
      read_text_file: true,
    }
  ) {
    return "Read";
  }
  if (name in { ls: true, list_files: true, list_dir: true }) return "List";
  if (name === "memory_search") return "Search memories";
  if (name in { mcp_free_search: true, mcp_paid_search: true }) return "Search web";
  if (name in { write: true, write_file: true, write_memory: true, write_text_file: true }) {
    return "Write";
  }
  if (name in { edit: true, edit_file: true, edit_memory: true, search_replace: true }) {
    return "Edit";
  }
  if (name === "session") return "Run session";
  return tool.name
    .split(/[_\-.]+/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}
