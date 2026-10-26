import { normalizeFinalContent } from "./final-content.js";
import type { EventFrame } from "./protocol.js";
import type { HistoryItem, InfoMeta, JsonValue, MediaItem, ToolCallDisplay } from "./types.js";

/** 合并同一次流式请求内多条 assistant 片段的正文；若末条为完整 `chat.final` 且等于前文拼接则去重。 */
export function mergeAssistantFragmentContents(parts: string[]): string {
  const p = parts.map((x) => String(x ?? ""));
  if (p.length === 0) {
    return "";
  }
  if (p.length === 1) {
    return p[0]!;
  }
  const withoutLast = p.slice(0, -1).join("");
  const last = p[p.length - 1]!;
  if (last === withoutLast) {
    return last;
  }
  return p.join("");
}

/**
 * 按 `at` 时间升序对同一 turn 的 assistant 片段排序，同毫秒时间戳时保持相对稳定性。
 * AgentServer 为了分页优先返回最新页，`_handle_history_get_stream` 会把整条历史 `list(reversed(raw))`
 * 再流式下发；CLI 侧每条 `history.message` 单独转成 assistant 条目，若不按时间重排，
 * 合并得到的就是「final + 倒序 delta」这种乱序正文。
 */
function sortAssistantGroupByTime(
  group: Extract<HistoryItem, { kind: "assistant" }>[],
): Extract<HistoryItem, { kind: "assistant" }>[] {
  return group
    .map((entry, originalIndex) => ({ entry, originalIndex, ts: Date.parse(entry.at) }))
    .sort((a, b) => {
      const ta = Number.isNaN(a.ts) ? 0 : a.ts;
      const tb = Number.isNaN(b.ts) ? 0 : b.ts;
      if (ta !== tb) return ta - tb;
      return a.originalIndex - b.originalIndex;
    })
    .map((item) => item.entry);
}

function sameAssistantTurn(
  a: Extract<HistoryItem, { kind: "assistant" }>,
  b: Extract<HistoryItem, { kind: "assistant" }>,
): boolean {
  if (a.sessionId !== b.sessionId) {
    return false;
  }
  const ar = a.requestId?.trim();
  const br = b.requestId?.trim();
  if (ar && br) {
    return ar === br;
  }
  // history.json 中 id 常为 `{request_id}:assistant`，流式多段共用同一 id
  if (a.id && a.id === b.id) {
    return true;
  }
  return false;
}

/**
 * 将流式恢复产生的多条 `kind: assistant`、同一次模型请求（requestId 或同源 id）的连续条目合并为一条。
 * 覆盖网关 history.get：本地 ack 无 `payload.messages`、正文仅靠 `history.message` 事件注入的路径。
 *
 * 策略（优先级从高到低）：
 *   1. 组内存在 `chat.final` 片段：直接采用最晚一条 final 的正文（权威完整答复，天然避免与 delta 拼接时的重复）。
 *   2. 全部为 delta：按 `at` 时间升序拼接，修复 AgentServer 分页导致的倒序问题。
 *   3. 无 eventType 元数据（老链路兜底）：退化到 `mergeAssistantFragmentContents` 原逻辑。
 */
export function coalesceAssistantHistoryEntries(entries: HistoryItem[]): HistoryItem[] {
  const out: HistoryItem[] = [];
  let i = 0;
  while (i < entries.length) {
    const e = entries[i]!;
    if (e.kind === "assistant" && !e.mediaItems?.length && !e.streaming) {
      const group: Extract<HistoryItem, { kind: "assistant" }>[] = [e];
      let j = i + 1;
      while (j < entries.length) {
        const n = entries[j]!;
        if (
          n.kind === "assistant" &&
          !n.mediaItems?.length &&
          !n.streaming &&
          sameAssistantTurn(e, n)
        ) {
          group.push(n);
          j++;
        } else {
          break;
        }
      }
      if (group.length === 1) {
        out.push(e);
      } else {
        out.push(mergeAssistantGroup(group));
      }
      i = j;
    } else {
      out.push(e);
      i++;
    }
  }
  return out;
}

function mergeAssistantGroup(
  group: Extract<HistoryItem, { kind: "assistant" }>[],
): Extract<HistoryItem, { kind: "assistant" }> {
  const finals = group.filter((g) => g.eventType === "chat.final" && g.content);
  if (finals.length > 0) {
    const sortedFinals = sortAssistantGroupByTime(finals);
    const chosen = sortedFinals[sortedFinals.length - 1]!;
    return { ...chosen };
  }

  const hasEventTypeMeta = group.some((g) => typeof g.eventType === "string" && g.eventType);
  const sorted = hasEventTypeMeta ? sortAssistantGroupByTime(group) : group;
  const last = sorted[sorted.length - 1]!;
  const content = mergeAssistantFragmentContents(sorted.map((g) => g.content));
  return { ...last, content };
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : undefined;
}

function pickFirstString(input: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) {
        return trimmed;
      }
    }
  }
  return undefined;
}

function parseArguments(raw: unknown): Record<string, unknown> | undefined {
  if (raw && typeof raw === "object") return raw as Record<string, unknown>;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // Ignore parse failures.
    }
  }
  return undefined;
}

function parseStructuredText(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  const text = raw.trim();
  if (!text) return raw;
  try {
    return JSON.parse(text);
  } catch {
    return raw;
  }
}

function isFalseLike(value: unknown): boolean {
  return (
    value === false ||
    (typeof value === "string" && ["false", "0", "no"].includes(value.trim().toLowerCase()))
  );
}

function isTrueLike(value: unknown): boolean {
  return (
    value === true ||
    (typeof value === "string" && ["true", "1", "yes"].includes(value.trim().toLowerCase()))
  );
}

function nonzeroExit(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return undefined;
  if (typeof value === "number" && Number.isFinite(value)) return value !== 0;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim());
    return Number.isInteger(parsed) ? parsed !== 0 : undefined;
  }
  return undefined;
}

function inferToolResultError(value: unknown): boolean | undefined {
  const parsed = parseStructuredText(value);
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      if (inferToolResultError(item)) return true;
    }
    return undefined;
  }
  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    if ("success" in record) {
      if (isFalseLike(record.success)) return true;
      if (isTrueLike(record.success)) return false;
    }
    if (isTrueLike(record.is_error) || isTrueLike(record.isError)) return true;
    if (
      typeof record.status === "string" &&
      ["error", "failed", "failure"].includes(record.status.trim().toLowerCase())
    ) {
      return true;
    }
    for (const key of ["exit_code", "exitCode", "returncode", "return_code"]) {
      const failed = nonzeroExit(record[key]);
      if (failed !== undefined) return failed;
    }
    for (const key of ["data", "raw_output", "rawOutput", "result"]) {
      const nested = record[key];
      if (nested && typeof nested === "object") {
        const nestedError = inferToolResultError(nested);
        if (nestedError !== undefined) return nestedError;
      }
    }
    return undefined;
  }
  if (typeof parsed === "string") {
    if (/\bsuccess\s*[:=]\s*False\b/i.test(parsed)) return true;
    if (parsed.trimStart().startsWith("[ERROR]")) return true;
    const exitMatch = parsed.match(
      /\b(?:exit(?:[_ ]?code)?|returncode|return[_ ]code)\s*[:= ]\s*(-?\d+)\b/i,
    );
    if (exitMatch) return Number(exitMatch[1]) !== 0;
  }
  return undefined;
}

function resolveToolPayload(
  payload: Record<string, unknown>,
  key: "tool_call" | "tool_result",
): Record<string, unknown> {
  return asRecord(payload[key]) ?? payload;
}

function resolveToolCallId(
  payload: Record<string, unknown>,
  fallback?: Record<string, unknown>,
): string | undefined {
  return (
    asString(payload.id) ??
    asString(payload.tool_call_id) ??
    asString(payload.toolCallId) ??
    asString(fallback?.tool_call_id) ??
    asString(fallback?.toolCallId)
  );
}

function resolveToolName(
  payload: Record<string, unknown>,
  fallback?: Record<string, unknown>,
): string {
  return (
    asString(payload.name) ??
    asString(payload.tool_name) ??
    asString(fallback?.tool_name) ??
    "unknown"
  );
}

function normalizeHistoryRole(rawRole: unknown): "user" | "assistant" | "tool" | "system" {
  if (typeof rawRole !== "string") return "assistant";
  const role = rawRole.trim().toLowerCase();
  if (role === "user" || role === "human") return "user";
  if (role === "assistant" || role === "ai" || role === "bot") return "assistant";
  if (role === "tool" || role === "tool_call" || role === "tool_result") return "tool";
  if (role === "system") return "system";
  return "assistant";
}

function historyRecordEventType(rec: Record<string, unknown>): string {
  const et = rec.event_type ?? rec.type;
  return typeof et === "string" ? et.trim() : "";
}

function isReasoningStreamRecord(rec: Record<string, unknown>): boolean {
  if (historyRecordEventType(rec) === "chat.reasoning") {
    return true;
  }
  const sct = rec.source_chunk_type;
  return typeof sct === "string" && sct.trim().toLowerCase() === "llm_reasoning";
}

/**
 * AgentServer 在流式输出时为每个 `chat.delta` 追加一条 history 记录；恢复会话时若逐条渲染，
 * 会把正文拆成「一字/一词一行」。此处合并同一次请求内的 delta，并在已有 `chat.final` 时丢弃冗余 delta。
 */
export function mergeHistoryMessagesForRestore(messages: unknown[]): Record<string, unknown>[] {
  const list: Record<string, unknown>[] = [];
  for (const m of messages) {
    const rec = asRecord(m);
    if (rec) {
      list.push(rec);
    }
  }

  const requestIdsWithFinal = new Set<string>();
  for (const rec of list) {
    if (normalizeHistoryRole(rec.role) !== "assistant") {
      continue;
    }
    if (historyRecordEventType(rec) !== "chat.final") {
      continue;
    }
    const rid = typeof rec.request_id === "string" ? rec.request_id.trim() : "";
    if (rid) {
      requestIdsWithFinal.add(rid);
    }
  }

  const filtered: Record<string, unknown>[] = [];
  for (const rec of list) {
    const role = normalizeHistoryRole(rec.role);
    const et = historyRecordEventType(rec);
    const rid = typeof rec.request_id === "string" ? rec.request_id.trim() : "";
    if (role === "assistant" && isReasoningStreamRecord(rec)) {
      continue;
    }
    if (
      role === "assistant" &&
      et === "chat.reasoning"
    ) {
      continue;
    }
    if (
      role === "assistant" &&
      et === "chat.delta" &&
      !isReasoningStreamRecord(rec) &&
      rid &&
      requestIdsWithFinal.has(rid)
    ) {
      continue;
    }
    filtered.push(rec);
  }

  const merged: Record<string, unknown>[] = [];
  let i = 0;
  while (i < filtered.length) {
    const rec = filtered[i]!;
    const role = normalizeHistoryRole(rec.role);
    const et = historyRecordEventType(rec);
    const rid = typeof rec.request_id === "string" ? rec.request_id.trim() : "";
    if (role === "assistant" && et === "chat.delta" && !isReasoningStreamRecord(rec) && rid) {
      const parts: string[] = [];
      let j = i;
      while (j < filtered.length) {
        const r = filtered[j]!;
        const rrole = normalizeHistoryRole(r.role);
        const ret = historyRecordEventType(r);
        const rrid = typeof r.request_id === "string" ? r.request_id.trim() : "";
        if (
          rrole === "assistant" &&
          ret === "chat.delta" &&
          !isReasoningStreamRecord(r) &&
          rrid === rid
        ) {
          const c = r.content;
          parts.push(typeof c === "string" ? c : c != null ? String(c) : "");
          j++;
        } else {
          break;
        }
      }
      merged.push({
        ...rec,
        event_type: "chat.final",
        content: parts.join(""),
      });
      i = j;
    } else {
      merged.push(rec);
      i++;
    }
  }

  return merged;
}

function recordTimestampIso(record: Record<string, unknown>): string {
  const ts = record.timestamp;
  if (typeof ts === "number" && Number.isFinite(ts)) {
    const millis = ts > 1_000_000_000_000 ? ts : ts * 1000;
    const d = new Date(millis);
    if (!Number.isNaN(d.getTime())) {
      return d.toISOString();
    }
  }
  if (typeof ts === "string") {
    const parsed = Date.parse(ts);
    if (!Number.isNaN(parsed)) {
      return new Date(parsed).toISOString();
    }
  }
  return new Date().toISOString();
}

function buildEventPayloadForRecord(record: Record<string, unknown>): Record<string, unknown> {
  // 历史记录中 tool_call/tool_result 可能直接在 record 顶层，也可能在 event_payload 里
  const eventPayload = asRecord(record.event_payload);
  const base = eventPayload ? { ...eventPayload } : {};

  // 复制顶层字段（如果有）
  if (typeof record.content === "string" && typeof base.content !== "string") {
    base.content = record.content;
  }
  if (typeof record.request_id === "string" && typeof base.request_id !== "string") {
    base.request_id = record.request_id;
  }
  if (typeof record.session_id === "string" && typeof base.session_id !== "string") {
    base.session_id = record.session_id;
  }
  // 关键：历史记录中 tool_call 和 tool_result 直接在 record 顶层
  if (record.tool_call && typeof record.tool_call === "object" && !base.tool_call) {
    base.tool_call = record.tool_call;
  }
  if (record.tool_result && typeof record.tool_result === "object" && !base.tool_result) {
    base.tool_result = record.tool_result;
  }
  // tool_name / tool_call_id 也可能在顶层（tool_result 场景）
  if (typeof record.tool_name === "string" && typeof base.tool_name !== "string") {
    base.tool_name = record.tool_name;
  }
  if (typeof record.tool_call_id === "string" && typeof base.tool_call_id !== "string") {
    base.tool_call_id = record.tool_call_id;
  }
  if (typeof record.result === "string" && typeof base.result !== "string") {
    base.result = record.result;
  }
  return base;
}

export function isHistoryDonePayload(payload: Record<string, unknown>): boolean {
  const status = typeof payload.status === "string" ? payload.status.trim().toLowerCase() : "";
  if (status === "done") {
    return true;
  }
  const content = typeof payload.content === "string" ? payload.content.trim().toLowerCase() : "";
  return content === "done";
}

function summarizePath(value: string): string {
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.length <= 3 ? value : parts.slice(-3).join("/");
}

function inferMediaType(
  explicitType: string | undefined,
  mimeType: string | undefined,
): MediaItem["type"] {
  const normalizedType = explicitType?.trim().toLowerCase();
  const normalizedMime = mimeType?.trim().toLowerCase();
  if (normalizedType === "image" || normalizedMime?.startsWith("image/")) return "image";
  if (normalizedType === "audio" || normalizedMime?.startsWith("audio/")) return "audio";
  if (normalizedType === "video" || normalizedMime?.startsWith("video/")) return "video";
  return "document";
}

export function extractMediaItems(payload: Record<string, unknown>): MediaItem[] {
  const files = Array.isArray(payload.files) ? payload.files : [];
  const mediaItems = Array.isArray(payload.media_items) ? payload.media_items : [];
  const rawItems = [...mediaItems, ...files].filter((item): item is Record<string, unknown> =>
    Boolean(item && typeof item === "object"),
  );

  return rawItems.map((item, index) => {
    const mimeType =
      pickFirstString(item, ["mimeType", "mime_type"]) ??
      (Array.isArray(payload.files) ? "application/octet-stream" : "application/octet-stream");
    const filename =
      pickFirstString(item, ["filename", "file_name", "name", "title"]) ??
      pickFirstString(item, ["path", "url"]) ??
      `item-${index + 1}`;
    const url = pickFirstString(item, ["url", "path", "fullPath", "full_path"]);
    const base64Data = pickFirstString(item, ["base64Data", "base64_data", "data"]);
    return {
      type: inferMediaType(pickFirstString(item, ["type"]), mimeType),
      mimeType,
      filename,
      ...(base64Data ? { base64Data } : {}),
      ...(url ? { url } : {}),
    };
  });
}

function buildAttachmentItems(payload: Record<string, unknown>): NonNullable<InfoMeta["items"]> {
  return extractMediaItems(payload).map((item) => {
    return {
      label: summarizePath(item.filename),
      value: item.url ? summarizePath(item.url) : undefined,
      description: item.type === "document" ? item.mimeType : item.type,
    };
  });
}

function summarizeAttachmentHeadline(
  mediaItems: MediaItem[],
  effectiveEvent: "chat.media" | "chat.file",
): string {
  if (mediaItems.length === 0) {
    return effectiveEvent === "chat.file" ? "Attached file" : "Added media";
  }

  const counts = {
    image: mediaItems.filter((item) => item.type === "image").length,
    audio: mediaItems.filter((item) => item.type === "audio").length,
    video: mediaItems.filter((item) => item.type === "video").length,
    document: mediaItems.filter((item) => item.type === "document").length,
  };

  const parts: string[] = [];
  if (counts.image > 0) parts.push(`${counts.image} image${counts.image === 1 ? "" : "s"}`);
  if (counts.audio > 0) parts.push(`${counts.audio} audio`);
  if (counts.video > 0) parts.push(`${counts.video} video${counts.video === 1 ? "" : "s"}`);
  if (counts.document > 0) parts.push(`${counts.document} file${counts.document === 1 ? "" : "s"}`);
  return `${effectiveEvent === "chat.file" ? "Attached" : "Added"} ${parts.join(", ")}`;
}

export function createAttachmentInfoEntry(
  payload: Record<string, unknown>,
  sessionId: string,
  effectiveEvent: "chat.media" | "chat.file",
  at = new Date().toISOString(),
): Extract<HistoryItem, { kind: "info" }> | null {
  const mediaItems = extractMediaItems(payload);
  const items = buildAttachmentItems(payload);
  const content =
    pickFirstString(payload, ["content", "text", "message"]) ??
    summarizeAttachmentHeadline(mediaItems, effectiveEvent);

  if (items.length === 0 && !pickFirstString(payload, ["content", "text", "message"])) {
    return null;
  }

  return {
    kind: "info",
    id: pickFirstString(payload, ["id", "message_id", "msg_id"]) ?? `media-${Date.now()}`,
    sessionId,
    content,
    at,
    ...(mediaItems.length > 0 ? { mediaItems } : {}),
    meta: {
      view: "list",
      title: content,
      items,
    },
  };
}

export function createToolCallDisplay(payload: Record<string, unknown>): ToolCallDisplay {
  const toolPayload = resolveToolPayload(payload, "tool_call");
  return {
    callId: resolveToolCallId(toolPayload, payload) ?? `tool-${Date.now()}`,
    name: resolveToolName(toolPayload, payload),
    arguments: parseArguments(toolPayload.arguments),
    description: asString(toolPayload.description),
    formattedArgs: asString(toolPayload.formatted_args),
    status: "running",
  };
}

export function applyToolResult(
  tool: ToolCallDisplay,
  payload: Record<string, unknown>,
): ToolCallDisplay {
  const toolPayload = resolveToolPayload(payload, "tool_result");
  const result =
    typeof toolPayload.result === "string"
      ? toolPayload.result
      : toolPayload.data !== undefined
        ? stringifyJson(toolPayload.data as JsonValue)
        : typeof toolPayload.error === "string"
          ? toolPayload.error
          : payload.content !== undefined
            ? stringifyJson(payload.content as JsonValue)
            : undefined;
  const status = asString(toolPayload.status);
  const success = typeof toolPayload.success === "boolean" ? toolPayload.success : undefined;
  const isError =
    (success !== undefined ? !success : undefined) ??
    (status ? status === "error" : undefined) ??
    asBoolean(payload.is_error) ??
    inferToolResultError(toolPayload) ??
    inferToolResultError(payload) ??
    inferToolResultError(result) ??
    false;
  return {
    ...tool,
    status: isError ? "error" : "completed",
    result,
    summary: asString(toolPayload.summary),
    isError,
  };
}

export function createSessionResultToolDisplay(
  payload: Record<string, unknown>,
  effectiveEvent = "chat.session_result",
): ToolCallDisplay {
  const sessionId = asString(payload.session_id) ?? "";
  const description = asString(payload.description) ?? "";
  const result = asString(payload.result) ?? "";
  const status = payload.status === "error" ? "error" : "completed";
  const callId = `session-${sessionId || "unknown"}-${typeof payload.index === "number" ? payload.index : Date.now()}`;
  const fullResult = description ? `描述: ${description}\n\n结果: ${result}` : result;

  return {
    callId,
    name: "session",
    arguments: {
      session_id: sessionId,
      description,
      event_type: effectiveEvent,
      status: typeof payload.status === "string" ? payload.status : undefined,
      index: typeof payload.index === "number" ? payload.index : undefined,
      total: typeof payload.total === "number" ? payload.total : undefined,
      is_parallel: payload.is_parallel === true,
    },
    description: description || "会话完成",
    formattedArgs: `会话任务：【${description || "未知任务"}】`,
    status,
    result: fullResult,
    summary: status === "error" ? "失败" : "完成",
    isError: status === "error",
  };
}

export function parseHistoryFrame(frame: EventFrame): HistoryItem | null {
  if (frame.event !== "history.message") return null;

  const outerPayload = frame.payload;
  if (isHistoryDonePayload(outerPayload)) {
    return null;
  }

  const record = asRecord(outerPayload.message) ?? outerPayload;
  const sessionId =
    pickFirstString(record, ["session_id"]) ?? asString(outerPayload.session_id) ?? "";
  const role = normalizeHistoryRole(record.role);
  const at = recordTimestampIso(record);
  const id =
    pickFirstString(record, ["id", "message_id", "msg_id"]) ??
    `hist-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;

  if (role === "user") {
    const content = pickFirstString(record, ["content", "text", "body"]) ?? "";
    if (!content) return null;
    return { kind: "user", id, sessionId, content, at };
  }

  if (role !== "assistant") {
    return null;
  }

  const sourceChunkType = pickFirstString(record, ["source_chunk_type"]) ?? "";
  let eventType = pickFirstString(record, ["event_type", "type"]) ?? "";
  if (!eventType) {
    const rawContent = pickFirstString(record, ["content"]) ?? "";
    if (!rawContent) {
      return null;
    }
    eventType = "chat.final";
  }

  const payload = buildEventPayloadForRecord(record);

  if (eventType === "chat.tool_call") {
    return {
      kind: "tool_group",
      id,
      sessionId,
      requestId: pickFirstString(record, ["request_id"]) ?? asString(payload.request_id),
      tools: [createToolCallDisplay(payload)],
      at,
    };
  }

  if (eventType === "chat.tool_result") {
    return {
      kind: "tool_group",
      id,
      sessionId,
      requestId: pickFirstString(record, ["request_id"]) ?? asString(payload.request_id),
      tools: [applyToolResult(createToolCallDisplay(payload), payload)],
      at,
    };
  }

  if (eventType === "chat.media") {
    const mediaItems = extractMediaItems(payload);
    const content = pickFirstString(payload, ["content", "text", "message"]) ?? "";
    if (!content && mediaItems.length === 0) {
      return null;
    }
    return {
      kind: "assistant",
      id,
      sessionId,
      content,
      ...(mediaItems.length > 0 ? { mediaItems } : {}),
      requestId: pickFirstString(record, ["request_id"]) ?? asString(payload.request_id),
      at,
    };
  }

  if (eventType === "chat.file") {
    return createAttachmentInfoEntry(payload, sessionId, eventType, at);
  }

  if (
    eventType === "chat.reasoning" ||
    (eventType === "chat.delta" && sourceChunkType === "llm_reasoning")
  ) {
    return null;
  }

  if (eventType === "chat.session_result" || eventType === "session_result") {
    return {
      kind: "tool_group",
      id,
      sessionId,
      requestId: pickFirstString(record, ["request_id"]) ?? asString(payload.request_id),
      tools: [createSessionResultToolDisplay(payload, eventType)],
      at,
    };
  }

  if (eventType === "context.compact_boundary") {
    return {
      kind: "info",
      id,
      sessionId,
      content: pickFirstString(record, ["content", "text", "body"]) ?? "Conversation compacted",
      icon: "i",
      meta: {
        view: "compact_boundary",
        title: "Conversation compacted",
      },
      at,
    };
  }

  if (eventType === "context.compact_summary") {
    const content = pickFirstString(record, ["content", "text", "body"]) ?? "";
    if (!content) return null;
    return {
      kind: "info",
      id,
      sessionId,
      content,
      icon: "i",
      transcriptOnly: true,
      meta: {
        view: "compact_summary",
        title: "Compaction summary",
      },
      at,
    };
  }

  if (eventType === "context.rewind_summary") {
    const content = pickFirstString(record, ["content", "text", "body"]) ?? "";
    if (!content) return null;
    return {
      kind: "info",
      id,
      sessionId,
      content,
      icon: "i",
      meta: {
        view: "rewind_summary",
        title: "Summarized conversation",
      },
      at,
    };
  }

  if (
    eventType === "context.usage" ||
    eventType === "chat.subtask_update" ||
    eventType === "chat.evolution_status" ||
    eventType === "chat.processing_status" ||
    eventType === "chat.interrupt_result" ||
    eventType === "chat.ask_user_question" ||
    eventType === "chat.usage_metadata" ||
    eventType === "chat.usage_summary"
  ) {
    return null;
  }

  const content =
    eventType === "chat.final"
      ? normalizeFinalContent(payload)
      : (pickFirstString(payload, ["content"]) ?? "");
  if (!content) return null;
  return {
    kind: "assistant",
    id,
    sessionId,
    content,
    requestId: pickFirstString(record, ["request_id"]) ?? asString(payload.request_id),
    at,
    // 供 `coalesceAssistantHistoryEntries` 在同一 requestId 的片段中优先选用 final，
    // 防止 AgentServer 分页倒序导致 delta 追加在 final 之后出现「镜像」叠加。
    eventType,
  };
}

export function stringifyJson(value: JsonValue): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * 合并历史恢复产生的分散 tool_group 条目。
 *
 * 历史消息中 `chat.tool_call` 和 `chat.tool_result` 分别解析为独立的 tool_group 条目，
 * 需要按 callId 将它们合并为一个完整的工具调用（call 提供 arguments，result 提供 status/result）。
 *
 * 处理情况：
 * - call + result：合并，result 信息覆盖 call
 * - 只有 call：保持 running 状态（可能被中断）
 * - 只有 result：保持独立（孤儿结果）
 */
export function coalesceToolGroupEntries(entries: HistoryItem[]): HistoryItem[] {
  // 收集所有 tool_group 条目及其位置
  const toolGroupIndices: number[] = [];
  const toolGroups: Extract<HistoryItem, { kind: "tool_group" }>[] = [];

  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (entry.kind === "tool_group") {
      toolGroupIndices.push(i);
      toolGroups.push(entry);
    }
  }

  if (toolGroups.length === 0) {
    return entries;
  }

  // 按 callId 分组收集工具信息
  const toolsByCallId = new Map<
    string,
    {
      calls: { tool: ToolCallDisplay; entryIndex: number }[];
      results: { tool: ToolCallDisplay; entryIndex: number }[];
    }
  >();

  for (let entryIndex = 0; entryIndex < toolGroups.length; entryIndex++) {
    const group = toolGroups[entryIndex];
    for (const tool of group.tools) {
      const callId = tool.callId;
      if (!callId) continue;

      let bucket = toolsByCallId.get(callId);
      if (!bucket) {
        bucket = { calls: [], results: [] };
        toolsByCallId.set(callId, bucket);
      }

      // running 状态无 result 表示是 call，否则是 result
      if (tool.status === "running" && !tool.result) {
        bucket.calls.push({ tool, entryIndex });
      } else {
        bucket.results.push({ tool, entryIndex });
      }
    }
  }

  // 构建合并后的工具映射（entryIndex -> merged tools）
  const mergedToolsByEntryIndex = new Map<number, (ToolCallDisplay | null)[]>();
  const usedCallIds = new Set<string>();

  for (const [callId, bucket] of toolsByCallId) {
    if (usedCallIds.has(callId)) continue;
    usedCallIds.add(callId);

    // 取最早的 call 和最早的 result 进行合并
    const earliestCall = bucket.calls.sort((a, b) => a.entryIndex - b.entryIndex)[0];
    const earliestResult = bucket.results.sort((a, b) => a.entryIndex - b.entryIndex)[0];

    if (earliestCall && earliestResult) {
      // 合并：call 提供 arguments，result 提供其他信息
      // 注意：result 通常有 tool_name，call 可能没有 name，所以优先使用 result 的 name
      const effectiveName = earliestResult.tool.name !== "unknown"
        ? earliestResult.tool.name
        : earliestCall.tool.name;
      const mergedTool: ToolCallDisplay = {
        ...earliestResult.tool,
        callId,
        name: effectiveName,
        arguments: earliestCall.tool.arguments ?? earliestResult.tool.arguments,
        description: earliestCall.tool.description ?? earliestResult.tool.description,
        formattedArgs: earliestCall.tool.formattedArgs ?? earliestResult.tool.formattedArgs,
      };
      // 将合并结果放入最早条目的位置
      addToMergedTools(mergedToolsByEntryIndex, earliestCall.entryIndex, mergedTool);
      // result 条目位置标记为空（后续将被移除）
      if (earliestResult.entryIndex !== earliestCall.entryIndex) {
        addToMergedTools(mergedToolsByEntryIndex, earliestResult.entryIndex, null);
      }
    } else if (earliestCall) {
      // 只有 call，保持 running 状态
      addToMergedTools(mergedToolsByEntryIndex, earliestCall.entryIndex, earliestCall.tool);
    } else if (earliestResult) {
      // 只有 result（孤儿），保持独立
      addToMergedTools(mergedToolsByEntryIndex, earliestResult.entryIndex, earliestResult.tool);
    }
  }

  // 重建条目列表
  const result: HistoryItem[] = [];
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (entry.kind !== "tool_group") {
      result.push(entry);
      continue;
    }

    const toolGroupIndex = toolGroupIndices.indexOf(i);
    if (toolGroupIndex === -1) {
      result.push(entry);
      continue;
    }

    const mergedTools = mergedToolsByEntryIndex.get(toolGroupIndex);
    if (!mergedTools) {
      // 未参与合并的工具组，保持原样
      result.push(entry);
      continue;
    }

    // 过滤掉 null（已被合并移除）
    const validTools = mergedTools.filter((t): t is ToolCallDisplay => t !== null);
    if (validTools.length === 0) {
      // 整个条目已被合并移除，跳过
      continue;
    }

    // 用合并后的工具重建条目
    const originalGroup = toolGroups[toolGroupIndex];
    result.push({
      ...originalGroup,
      tools: validTools,
    });
  }

  return result;
}

function addToMergedTools(
  map: Map<number, (ToolCallDisplay | null)[]>,
  entryIndex: number,
  tool: ToolCallDisplay | null,
): void {
  let arr = map.get(entryIndex);
  if (!arr) {
    arr = [];
    map.set(entryIndex, arr);
  }
  arr.push(tool);
}
