import type { HistoryItem } from "../types.js";
import type { AccentColorName, ThemeName } from "../../ui/theme.js";
import type { PendingQuestionItem, UserAnswer } from "../event-handlers.js";
import type { FileAttachment } from "../protocol.js";
import type { ConfigItemSchema } from "./builtins/config.js";

export type ConnectionStatus = "idle" | "connecting" | "connected" | "reconnecting" | "auth_failed";

export enum CommandKind {
  BUILT_IN = "built-in",
}

export interface CommandSuggestion {
  value: string;
  description?: string;
  usage?: string;
  example?: string;
}

export interface CommandContext {
  sendEventOnly: (method: string, params: Record<string, unknown>) => string;
  request: <T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs?: number,
  ) => Promise<T>;
  askQuestions: (questions: PendingQuestionItem[], source?: string) => Promise<UserAnswer[]>;
  sendMessage: (
    content: string,
    attachments?: FileAttachment[],
    mode?: "agent.plan" | "agent.fast" | "code.plan" | "code.normal" | "team",
  ) => string | null;
  sessionId: string;
  entries: HistoryItem[];
  themeName: ThemeName;
  accentColor: AccentColorName;
  updateSession: (id: string) => void;
  addItem: (item: HistoryItem) => void;
  clearEntries: () => void;
  restoreHistory: (sessionId: string) => Promise<void>;
  exitApp: () => void;
  isProcessing: boolean;
  connectionStatus: ConnectionStatus;
  mode: "agent.plan" | "agent.fast" | "code.plan" | "code.normal" | "team";
  setMode: (mode: "agent.plan" | "agent.fast" | "code.plan" | "code.normal" | "team") => void;
  setThemeName: (theme: ThemeName) => void;
  setAccentColor: (color: AccentColorName) => void;
  transcriptMode: "compact" | "detailed";
  setTranscriptMode: (mode: "compact" | "detailed") => void;
  transcriptFoldMode: "none" | "tools" | "thinking" | "all";
  setTranscriptFoldMode: (mode: "none" | "tools" | "thinking" | "all") => void;
  collapsedToolGroupCount: number;
  collapseToolGroups: (scope: "last" | "all") => void;
  expandToolGroups: (scope: "last" | "all") => void;
  sessionTitle: string;
  setSessionTitle: (title: string) => void;
  getWorkspaceDir: () => string;
  setWorkspaceDir: (path: string) => void;
  enterConfigEditor?: (
    focusKey?: string,
    configPayload?: Record<string, unknown> & { schema?: ConfigItemSchema[] },
  ) => void;
}

export interface SlashCommand {
  name: string;
  altNames?: string[];
  description: string;
  usage?: string;
  example?: string;
  hidden?: boolean;
  isSafeConcurrent?: boolean;
  kind: CommandKind;
  action: (ctx: CommandContext, args: string) => void | Promise<void>;
  completion?: (ctx: CommandContext, partial: string) => string[] | Promise<string[]>;
  takesArgs?: boolean;
  subCommands?: SlashCommand[];
}

export type SlashCommandListProvider = () => readonly SlashCommand[];
