import { useCallback, useEffect, useRef } from "react";

import type { ApplicationPluginTaskRuntimeProps } from "../../../channels/web/frontend/src/applicationPlugins/types";
import { useTaskFullDuplexEnabled } from "../../../channels/web/frontend/src/features/taskFullDuplex/featureFlag";
import {
  webClient,
  webRequest,
} from "../../../channels/web/frontend/src/services/webClient";
import {
  registerApplicationTaskController,
  useApplicationTaskStore,
} from "../../../channels/web/frontend/src/applicationPlugins/taskProgressStore";
import { VideoLivePanel, type VideoLivePanelHandle } from "./VideoLivePanel";
import type {
  SearchJobPayload,
  SearchProgressEntry,
} from "./VideoLivePanel/types";
import { TaskDuplexJobs } from "./taskDuplexJobs";
import {
  registerTaskFullDuplexController,
  setTaskFullDuplexRuntimeError,
  setTaskFullDuplexRuntimeState,
  stopTaskFullDuplex,
} from "./taskFullDuplexRuntimeStore";

type PersistedTimelineEvent = Record<string, unknown> & {
  kind:
    "user" | "assistant" | "reasoning" | "tool_call" | "tool_result" | "file";
  timestamp: number;
};

const historyQueues = new Map<string, Promise<void>>();

function timelineEventId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  );
}

function timestampSeconds(value?: string | number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value / 1_000;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed / 1_000;
  }
  return Date.now() / 1_000;
}

function persistTimelineEvent(
  sessionId: string,
  event: PersistedTimelineEvent,
): void {
  if (!sessionId || sessionId === "new") return;
  const previous = historyQueues.get(sessionId) ?? Promise.resolve();
  const next = previous
    .catch(() => undefined)
    .then(async () => {
      await webRequest("video.conversation.append", {
        session_id: sessionId,
        event_id: timelineEventId(),
        ...event,
      });
    })
    .catch((error) => {
      console.warn("Failed to persist Full-duplex timeline event:", error);
    });
  historyQueues.set(sessionId, next);
  void next.finally(() => {
    if (historyQueues.get(sessionId) === next) historyQueues.delete(sessionId);
  });
}

export function TaskFullDuplexRuntime({
  sessionId,
  onConversationItem,
  onAssistantStream,
  onReasoning,
  onReasoningClose,
  onToolCall,
  onToolResult,
  onFileItems,
}: ApplicationPluginTaskRuntimeProps) {
  const enabled = useTaskFullDuplexEnabled();
  const panelRef = useRef<VideoLivePanelHandle | null>(null);
  const runtimeSessionIdRef = useRef<string | null>(null);
  const previousSessionIdRef = useRef(sessionId);
  const processedCoreProgressRef = useRef<Map<string, Set<number>>>(new Map());
  const conversationJobsRef = useRef(new TaskDuplexJobs());
  const pollingJobsRef = useRef(new Set<string>());
  const coreToolIdsRef = useRef<Map<string, Map<string, string>>>(new Map());
  const persistedAssistantStreamsRef = useRef<Set<string>>(new Set());
  const pendingReasoningRef = useRef(
    new Map<
      string,
      {
        content: string;
        startedAt: number;
        updatedAt: number;
      }
    >(),
  );

  const emitReasoning = useCallback(
    (targetSessionId: string, content: string, atMs = Date.now()) => {
      onReasoning(targetSessionId, content, atMs);
      const pending = pendingReasoningRef.current.get(targetSessionId);
      if (pending) {
        pending.content += content;
        pending.updatedAt = atMs;
      } else {
        pendingReasoningRef.current.set(targetSessionId, {
          content,
          startedAt: atMs,
          updatedAt: atMs,
        });
      }
    },
    [onReasoning],
  );

  const closeReasoning = useCallback(
    (targetSessionId: string, atMs = Date.now()) => {
      onReasoningClose(targetSessionId, atMs);
      const pending = pendingReasoningRef.current.get(targetSessionId);
      if (!pending) return;
      pendingReasoningRef.current.delete(targetSessionId);
      if (!pending.content.trim()) return;
      persistTimelineEvent(targetSessionId, {
        kind: "reasoning",
        content: pending.content,
        timestamp: pending.startedAt / 1_000,
        started_at: pending.startedAt,
        updated_at: Math.max(pending.updatedAt, atMs),
      });
    },
    [onReasoningClose],
  );

  const emitToolCall = useCallback(
    (
      targetSessionId: string,
      toolCall: Parameters<typeof onToolCall>[1],
      startedAt?: string,
    ) => {
      onToolCall(targetSessionId, toolCall, startedAt);
      persistTimelineEvent(targetSessionId, {
        kind: "tool_call",
        timestamp: timestampSeconds(startedAt),
        tool_call: toolCall,
      });
    },
    [onToolCall],
  );

  const emitToolResult = useCallback(
    (
      targetSessionId: string,
      toolResult: Parameters<typeof onToolResult>[1],
      updatedAt?: string,
    ) => {
      onToolResult(targetSessionId, toolResult, updatedAt);
      persistTimelineEvent(targetSessionId, {
        kind: "tool_result",
        timestamp: timestampSeconds(updatedAt),
        tool_result: {
          ...toolResult,
          tool_name: toolResult.toolName,
          tool_call_id: toolResult.toolCallId,
        },
      });
    },
    [onToolResult],
  );

  const progressAtMs = useCallback((entry: SearchProgressEntry): number => {
    return typeof entry.timestamp === "number" &&
      Number.isFinite(entry.timestamp)
      ? entry.timestamp * 1_000
      : Date.now();
  }, []);

  const progressAtIso = useCallback(
    (entry: SearchProgressEntry): string => {
      return new Date(progressAtMs(entry)).toISOString();
    },
    [progressAtMs],
  );

  const toolArguments = useCallback(
    (value: unknown): Record<string, unknown> => {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        return value as Record<string, unknown>;
      }
      if (typeof value === "string") {
        try {
          const parsed = JSON.parse(value);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            return parsed as Record<string, unknown>;
          }
        } catch {
          return value ? { input: value } : {};
        }
      }
      return value == null ? {} : { input: value };
    },
    [],
  );

  const toolResultText = useCallback(
    (value: unknown, fallback = ""): string => {
      if (typeof value === "string") return value;
      if (value == null) return fallback;
      try {
        return JSON.stringify(value);
      } catch {
        return String(value);
      }
    },
    [],
  );

  const handleCoreAgentProgress = useCallback(
    (
      event: "started" | "progress" | "completed" | "failed" | "cancelled",
      incoming: SearchJobPayload,
    ) => {
      const job = conversationJobsRef.current.accept({
        ...incoming,
        status:
          incoming.status ||
          (event === "completed" || event === "failed" ? event : "running"),
      });
      if (!job) return;
      const { sessionId: targetSessionId, payload } = job;
      const jobId = payload.job_id!;
      const entries = payload.progress_history?.length
        ? payload.progress_history
        : payload.progress
          ? [payload.progress]
          : [];
      const latest = entries.reduce<SearchProgressEntry | undefined>(
        (last, entry) =>
          !last || entry.sequence > last.sequence ? entry : last,
        undefined,
      );
      const plan = [...entries].reverse().find((entry) => entry.todos)?.todos;
      const status =
        payload.status ||
        (event === "completed" || event === "failed" ? event : "running");
      const previousTask = useApplicationTaskStore
        .getState()
        .sessions[targetSessionId]?.find((task) => task.id === jobId);
      useApplicationTaskStore.getState().upsert(targetSessionId, {
        id: jobId,
        pluginId: "video-duplex",
        title:
          payload.question?.trim() ||
          payload.query?.trim() ||
          previousTask?.title ||
          "Jiuwen Core Agent",
        status,
        sequence: latest?.sequence || 0,
        detail:
          payload.error ||
          [latest?.title, latest?.detail].filter(Boolean).join("\n"),
        createdAt: (entries[0]?.timestamp || Date.now() / 1000) * 1000,
        steps: plan,
        searchSessionId: payload.search_session_id,
        queuePosition: payload.queue_position ?? previousTask?.queuePosition,
        queueVersion: payload.queue_version ?? previousTask?.queueVersion,
      });
      const processed =
        processedCoreProgressRef.current.get(jobId) || new Set<number>();
      processedCoreProgressRef.current.set(jobId, processed);
      const toolIds =
        coreToolIdsRef.current.get(jobId) || new Map<string, string>();
      coreToolIdsRef.current.set(jobId, toolIds);

      entries
        .slice()
        .sort((left, right) => left.sequence - right.sequence)
        .forEach((entry) => {
          if (processed.has(entry.sequence)) return;
          processed.add(entry.sequence);
          const atMs = progressAtMs(entry);

          if (entry.stage === "file" && entry.files?.length) {
            onFileItems(targetSessionId, entry.files, progressAtIso(entry));
            persistTimelineEvent(targetSessionId, {
              kind: "file",
              files: entry.files,
              timestamp: atMs / 1_000,
            });
            return;
          }

          if (entry.stage === "reasoning" && entry.content) {
            emitReasoning(targetSessionId, entry.content, atMs);
            return;
          }
          if (entry.stage === "tool_call") {
            closeReasoning(targetSessionId, atMs);
            const rawId =
              entry.tool_call_id?.trim() || `sequence-${entry.sequence}`;
            const id = `full-duplex-core-${jobId}-${rawId}`;
            toolIds.set(rawId, id);
            emitToolCall(
              targetSessionId,
              {
                id,
                name: entry.tool_name?.trim() || "unknown",
                arguments: toolArguments(entry.tool_arguments),
                description: entry.tool_description?.trim() || undefined,
                formatted_args: entry.tool_formatted_args?.trim() || undefined,
                display_name: entry.tool_display_name?.trim() || undefined,
              },
              progressAtIso(entry),
            );
            return;
          }
          if (entry.stage === "tool_result") {
            const rawId = entry.tool_call_id?.trim() || "";
            const id =
              toolIds.get(rawId) ||
              `full-duplex-core-${jobId}-${rawId || `sequence-${entry.sequence}`}`;
            emitToolResult(
              targetSessionId,
              {
                toolName: entry.tool_name?.trim() || "unknown",
                toolCallId: id,
                result: toolResultText(entry.tool_result, entry.detail || ""),
                success:
                  entry.tool_success !== false && entry.status !== "failed",
                summary: entry.tool_summary?.trim() || undefined,
              },
              progressAtIso(entry),
            );
          }
        });

      if (
        status === "completed" ||
        status === "failed" ||
        status === "cancelled"
      ) {
        closeReasoning(targetSessionId);
        const result = conversationJobsRef.current.takeResult(jobId);
        if (result) {
          onConversationItem(
            targetSessionId,
            "assistant",
            result,
            "tool_result",
          );
          persistTimelineEvent(targetSessionId, {
            kind: "assistant",
            content: result,
            presentation: "tool_result",
            timestamp: Date.now() / 1_000,
          });
          // Display/persistence is authoritative. Playback is optional and connection-scoped.
          if (runtimeSessionIdRef.current === targetSessionId)
            panelRef.current?.deliverToolResult(payload);
        }
      }
    },
    [
      closeReasoning,
      emitReasoning,
      emitToolCall,
      emitToolResult,
      progressAtIso,
      progressAtMs,
      onConversationItem,
      onFileItems,
      toolArguments,
      toolResultText,
    ],
  );

  const acceptQueue = useCallback(
    (snapshot: {
      search_session_id: string;
      queue_version: number;
      jobs: SearchJobPayload[];
    }) => {
      for (const payload of snapshot.jobs) {
        const existing = Object.values(
          useApplicationTaskStore.getState().sessions,
        )
          .flat()
          .find((task) => task.id === payload.job_id);
        if (existing && (existing.queueVersion ?? -1) > snapshot.queue_version)
          continue;
        handleCoreAgentProgress("progress", payload);
        const entry = Object.entries(
          useApplicationTaskStore.getState().sessions,
        ).find(([, tasks]) => tasks.some((item) => item.id === payload.job_id));
        const task = entry?.[1].find((item) => item.id === payload.job_id);
        if (task && entry)
          useApplicationTaskStore.getState().upsert(entry[0], {
            ...task,
            queueVersion: snapshot.queue_version,
            queuePosition: payload.queue_position,
          });
      }
    },
    [handleCoreAgentProgress],
  );

  useEffect(() => {
    const off = webClient.on<{
      search_session_id: string;
      queue_version: number;
      jobs: SearchJobPayload[];
    }>("video.search.queue", ({ payload }) => acceptQueue(payload));
    const unregister = registerApplicationTaskController(
      "video-duplex",
      async (task, action, beforeId) => {
        const result = await webRequest<{
          search_session_id: string;
          queue_version: number;
          jobs: SearchJobPayload[];
        }>(
          "video.search.control",
          {
            job_id: task.id,
            search_session_id: task.searchSessionId,
            action,
            queue_version: task.queueVersion,
            before_job_id: beforeId,
          },
          { timeoutMs: 30_000 },
        );
        acceptQueue(result);
      },
    );
    return () => {
      off();
      unregister();
    };
  }, [acceptQueue]);

  // Subscribe and recover both progress and final answers independently of media lifetime.
  useEffect(() => {
    let disposed = false;
    const unsubscribe = (
      ["started", "progress", "completed", "failed", "cancelled"] as const
    ).map((event) =>
      webClient.on<SearchJobPayload>(`video.search.${event}`, ({ payload }) =>
        handleCoreAgentProgress(event, payload),
      ),
    );
    const timer = window.setInterval(() => {
      for (const { payload } of conversationJobsRef.current.pending()) {
        const jobId = payload.job_id!;
        if (pollingJobsRef.current.has(jobId)) continue;
        pollingJobsRef.current.add(jobId);
        void webRequest<SearchJobPayload>(
          "video.search.status",
          {
            job_id: jobId,
            search_session_id: payload.search_session_id,
          },
          { timeoutMs: 5000 },
        )
          .then((payload) => {
            if (!disposed) handleCoreAgentProgress("progress", payload);
          })
          .catch(() => undefined)
          .finally(() => pollingJobsRef.current.delete(jobId));
      }
    }, 3000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      unsubscribe.forEach((off) => off());
    };
  }, [handleCoreAgentProgress]);

  const setPanelRef = useCallback((panel: VideoLivePanelHandle | null) => {
    panelRef.current = panel;
    registerTaskFullDuplexController(panel, (targetSessionId) => {
      runtimeSessionIdRef.current = targetSessionId;
      return conversationJobsRef.current.bind(targetSessionId);
    });
  }, []);

  useEffect(
    () => () => {
      registerTaskFullDuplexController(null);
      panelRef.current?.stop();
    },
    [],
  );

  useEffect(() => {
    if (!enabled) {
      stopTaskFullDuplex();
      runtimeSessionIdRef.current = null;
    }
  }, [enabled]);

  useEffect(() => {
    if (previousSessionIdRef.current === sessionId) return;
    if (
      runtimeSessionIdRef.current &&
      runtimeSessionIdRef.current !== sessionId
    ) {
      closeReasoning(runtimeSessionIdRef.current);
      stopTaskFullDuplex();
      runtimeSessionIdRef.current = null;
    }
    previousSessionIdRef.current = sessionId;
  }, [closeReasoning, sessionId]);

  return (
    <VideoLivePanel
      ref={setPanelRef}
      headless
      onConversationItem={(role, text, presentation) => {
        const targetSessionId = runtimeSessionIdRef.current || sessionId;
        if (!targetSessionId) return;
        onConversationItem(targetSessionId, role, text, presentation);
        persistTimelineEvent(targetSessionId, {
          kind: role,
          content: text,
          presentation,
          timestamp: Date.now() / 1_000,
        });
      }}
      onAssistantStream={(update) => {
        const targetSessionId = runtimeSessionIdRef.current || sessionId;
        if (!targetSessionId) return;
        onAssistantStream(targetSessionId, update);
        if (!update.final || !update.content.trim()) return;
        const persistenceKey = `${targetSessionId}:${update.streamId}`;
        if (persistedAssistantStreamsRef.current.has(persistenceKey)) return;
        persistedAssistantStreamsRef.current.add(persistenceKey);
        if (persistedAssistantStreamsRef.current.size > 64) {
          const oldest = persistedAssistantStreamsRef.current
            .values()
            .next().value;
          if (oldest) persistedAssistantStreamsRef.current.delete(oldest);
        }
        persistTimelineEvent(targetSessionId, {
          kind: "assistant",
          content: update.content,
          timestamp: Date.now() / 1_000,
        });
      }}
      onRuntimeState={(state) => {
        if (state === "starting") persistedAssistantStreamsRef.current.clear();
        if (state === "starting" || state === "active") {
          runtimeSessionIdRef.current ||= sessionId;
        }
        if (state === "idle") runtimeSessionIdRef.current = null;
        setTaskFullDuplexRuntimeState(state);
      }}
      onError={setTaskFullDuplexRuntimeError}
      onCoreAgentProgress={handleCoreAgentProgress}
    />
  );
}
