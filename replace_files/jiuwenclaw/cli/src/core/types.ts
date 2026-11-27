export enum StreamingState {
  Idle = "idle",
  Responding = "responding",
  Paused = "paused",
  WaitingForConfirmation = "waiting_for_confirmation",
}

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export interface JsonObject {
  [key: string]: JsonValue;
}

export interface MediaItem {
  type: "image" | "audio" | "video" | "document";
  mimeType: string;
  filename: string;
  base64Data?: string;
  url?: string;
}

export interface SystemMeta {
  eventType?: "chat.media" | "chat.file" | "notice";
  rawPayload?: JsonObject;
  fileName?: string;
  filePath?: string;
}

export interface InfoMeta {
  view?: "help" | "list" | "kv";
  title?: string;
  items?: Array<{ label: string; value?: string; description?: string }>;
}

export interface ToolCallDisplay {
  callId: string;
  name: string;
  arguments?: unknown;
  description?: string;
  formattedArgs?: string;
  status: "running" | "completed" | "error" | "timeout";
  result?: string;
  summary?: string;
  isError?: boolean;
}

export type ToolExecutionStatus = "pending" | "timeout" | "completed" | "error";

export interface ToolExecution {
  toolCallId: string;
  sessionId: string;
  requestId?: string;
  tool: ToolCallDisplay;
  startedAt: string;
  updatedAt: string;
  timeoutAt: string;
  timedOutAt?: string;
  resultArrivedAfterTimeout?: boolean;
}

export type SubtaskStatus = "starting" | "tool_call" | "tool_result" | "completed" | "error";

export interface SubtaskState {
  task_id: string;
  description: string;
  status: SubtaskStatus;
  index: number;
  total: number;
  tool_name?: string;
  tool_count: number;
  message?: string;
  is_parallel: boolean;
}

export interface ContextCompressionStats {
  rate: number;
  beforeCompressed: number | null;
  afterCompressed: number | null;
}

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoItem {
  id: string;
  content: string;
  activeForm: string;
  status: TodoStatus;
  createdAt: string;
  updatedAt: string;
}

export interface TeamMemberEvent {
  id: string;
  type: string;
  teamId: string;
  memberId: string;
  oldStatus?: string;
  newStatus?: string;
  reason?: string;
  restartCount?: number;
  force?: boolean;
  timestamp: number;
}

export interface TeamTaskEvent {
  id: string;
  type: string;
  teamId: string;
  taskId: string;
  status?: string;
  timestamp: number;
}

export interface TeamMessageEvent {
  id: string;
  type: string;
  teamId: string;
  messageId?: string;
  fromMember: string;
  toMember?: string;
  content: string;
  timestamp: number;
}

export interface Hunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: string[];
}

export interface FileDiff {
  filePath: string;
  hunks: Hunk[];
  isNewFile: boolean;
  linesAdded: number;
  linesRemoved: number;
  lastEditTime?: string;
}

export interface TurnDiff {
  turnIndex: number;
  userPromptPreview: string;
  timestamp: string;
  files: Record<string, FileDiff>;
  stats: {
    filesChanged: number;
    linesAdded: number;
    linesRemoved: number;
  };
}

export interface DiffMeta {
  turns: TurnDiff[];
}

export type HistoryItem =
  | { kind: "user"; id: string; sessionId: string; content: string; at: string }
  | {
      kind: "assistant";
      id: string;
      sessionId: string;
      content: string;
      mediaItems?: MediaItem[];
      audioBase64?: string;
      audioMime?: string;
      streaming?: boolean;
      requestId?: string;
      at: string;
    }
  | { kind: "thinking"; id: string; sessionId: string; content: string; at: string }
  | {
      kind: "tool_group";
      id: string;
      sessionId: string;
      requestId?: string;
      tools: ToolCallDisplay[];
      at: string;
    }
  | {
      kind: "collapsed_tool_group";
      id: string;
      sessionId: string;
      requestId?: string;
      tools: ToolCallDisplay[];
      at: string;
      latestHint?: string;
      counts: {
        read: number;
        search: number;
        list: number;
        fetch: number;
      };
    }
  | {
      kind: "system";
      id: string;
      sessionId: string;
      content: string;
      meta?: SystemMeta;
      at: string;
    }
  | { kind: "error"; id: string; sessionId: string; content: string; at: string }
  | {
      kind: "info";
      id: string;
      sessionId: string;
      content: string;
      icon?: string;
      meta?: InfoMeta;
      mediaItems?: MediaItem[];
      at: string;
    }
  | {
      kind: "diff";
      id: string;
      sessionId: string;
      content: string;
      meta: DiffMeta;
      at: string;
    };
