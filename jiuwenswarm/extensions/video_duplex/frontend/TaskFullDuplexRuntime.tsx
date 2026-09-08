import { useCallback, useEffect, useRef } from 'react';

import type { ApplicationPluginTaskRuntimeProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { useTaskFullDuplexEnabled } from '../../../channels/web/frontend/src/features/taskFullDuplex/featureFlag';
import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import { VideoLivePanel, type VideoLivePanelHandle } from './VideoLivePanel';
import type { SearchJobPayload, SearchProgressEntry } from './VideoLivePanel/types';
import {
  registerTaskFullDuplexController,
  setTaskFullDuplexRuntimeError,
  setTaskFullDuplexRuntimeState,
  stopTaskFullDuplex,
} from './taskFullDuplexRuntimeStore';

type PersistedTimelineEvent = Record<string, unknown> & {
  kind: 'user' | 'assistant' | 'reasoning' | 'tool_call' | 'tool_result';
  timestamp: number;
};

const historyQueues = new Map<string, Promise<void>>();

function timelineEventId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function timestampSeconds(value?: string | number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value / 1_000;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed / 1_000;
  }
  return Date.now() / 1_000;
}

function persistTimelineEvent(sessionId: string, event: PersistedTimelineEvent): void {
  if (!sessionId || sessionId === 'new') return;
  const previous = historyQueues.get(sessionId) ?? Promise.resolve();
  const next = previous
    .catch(() => undefined)
    .then(async () => {
      await webRequest('video.conversation.append', {
        session_id: sessionId,
        event_id: timelineEventId(),
        ...event,
      });
    })
    .catch((error) => {
      console.warn('Failed to persist Full-duplex timeline event:', error);
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
}: ApplicationPluginTaskRuntimeProps) {
  const enabled = useTaskFullDuplexEnabled();
  const panelRef = useRef<VideoLivePanelHandle | null>(null);
  const runtimeSessionIdRef = useRef<string | null>(null);
  const previousSessionIdRef = useRef(sessionId);
  const processedCoreProgressRef = useRef<Map<string, Set<number>>>(new Map());
  const coreToolIdsRef = useRef<Map<string, Map<string, string>>>(new Map());
  const persistedAssistantStreamsRef = useRef<Set<string>>(new Set());
  const pendingReasoningRef = useRef<{
    sessionId: string;
    content: string;
    startedAt: number;
    updatedAt: number;
  } | null>(null);

  const emitReasoning = useCallback(
    (targetSessionId: string, content: string, atMs = Date.now()) => {
      onReasoning(targetSessionId, content, atMs);
      const pending = pendingReasoningRef.current;
      if (pending?.sessionId === targetSessionId) {
        pending.content += content;
        pending.updatedAt = atMs;
      } else {
        pendingReasoningRef.current = {
          sessionId: targetSessionId,
          content,
          startedAt: atMs,
          updatedAt: atMs,
        };
      }
    },
    [onReasoning],
  );

  const closeReasoning = useCallback(
    (targetSessionId: string, atMs = Date.now()) => {
      onReasoningClose(targetSessionId, atMs);
      const pending = pendingReasoningRef.current;
      if (!pending || pending.sessionId !== targetSessionId) return;
      pendingReasoningRef.current = null;
      if (!pending.content.trim()) return;
      persistTimelineEvent(targetSessionId, {
        kind: 'reasoning',
        content: pending.content,
        timestamp: pending.startedAt / 1_000,
        started_at: pending.startedAt,
        updated_at: Math.max(pending.updatedAt, atMs),
      });
    },
    [onReasoningClose],
  );

  const emitToolCall = useCallback(
    (targetSessionId: string, toolCall: Parameters<typeof onToolCall>[1], startedAt?: string) => {
      onToolCall(targetSessionId, toolCall, startedAt);
      persistTimelineEvent(targetSessionId, {
        kind: 'tool_call',
        timestamp: timestampSeconds(startedAt),
        tool_call: toolCall,
      });
    },
    [onToolCall],
  );

  const emitToolResult = useCallback(
    (targetSessionId: string, toolResult: Parameters<typeof onToolResult>[1], updatedAt?: string) => {
      onToolResult(targetSessionId, toolResult, updatedAt);
      persistTimelineEvent(targetSessionId, {
        kind: 'tool_result',
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
    return typeof entry.timestamp === 'number' && Number.isFinite(entry.timestamp)
      ? entry.timestamp * 1_000
      : Date.now();
  }, []);

  const progressAtIso = useCallback(
    (entry: SearchProgressEntry): string => {
      return new Date(progressAtMs(entry)).toISOString();
    },
    [progressAtMs],
  );

  const toolArguments = useCallback((value: unknown): Record<string, unknown> => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
    if (typeof value === 'string') {
      try {
        const parsed = JSON.parse(value);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          return parsed as Record<string, unknown>;
        }
      } catch {
        return value ? { input: value } : {};
      }
    }
    return value == null ? {} : { input: value };
  }, []);

  const toolResultText = useCallback((value: unknown, fallback = ''): string => {
    if (typeof value === 'string') return value;
    if (value == null) return fallback;
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }, []);

  const handleCoreAgentProgress = useCallback(
    (event: 'started' | 'progress' | 'completed' | 'failed', payload: SearchJobPayload) => {
      const targetSessionId = runtimeSessionIdRef.current || sessionId;
      const jobId = payload.job_id?.trim();
      if (!targetSessionId || !jobId) return;
      const entries = payload.progress_history?.length
        ? payload.progress_history
        : payload.progress
          ? [payload.progress]
          : [];
      const processed = processedCoreProgressRef.current.get(jobId) || new Set<number>();
      processedCoreProgressRef.current.set(jobId, processed);
      const toolIds = coreToolIdsRef.current.get(jobId) || new Map<string, string>();
      coreToolIdsRef.current.set(jobId, toolIds);

      entries
        .slice()
        .sort((left, right) => left.sequence - right.sequence)
        .forEach((entry) => {
          if (processed.has(entry.sequence)) return;
          processed.add(entry.sequence);
          const atMs = progressAtMs(entry);

          if (entry.stage === 'reasoning' && entry.content) {
            emitReasoning(targetSessionId, entry.content, atMs);
            return;
          }
          if (entry.stage === 'tool_call') {
            closeReasoning(targetSessionId, atMs);
            const rawId = entry.tool_call_id?.trim() || `sequence-${entry.sequence}`;
            const id = `full-duplex-core-${jobId}-${rawId}`;
            toolIds.set(rawId, id);
            emitToolCall(
              targetSessionId,
              {
                id,
                name: entry.tool_name?.trim() || 'unknown',
                arguments: toolArguments(entry.tool_arguments),
                description: entry.tool_description?.trim() || undefined,
                formatted_args: entry.tool_formatted_args?.trim() || undefined,
                display_name: entry.tool_display_name?.trim() || undefined,
              },
              progressAtIso(entry),
            );
            return;
          }
          if (entry.stage === 'tool_result') {
            const rawId = entry.tool_call_id?.trim() || '';
            const id = toolIds.get(rawId) || `full-duplex-core-${jobId}-${rawId || `sequence-${entry.sequence}`}`;
            emitToolResult(
              targetSessionId,
              {
                toolName: entry.tool_name?.trim() || 'unknown',
                toolCallId: id,
                result: toolResultText(entry.tool_result, entry.detail || ''),
                success: entry.tool_success !== false && entry.status !== 'failed',
                summary: entry.tool_summary?.trim() || undefined,
              },
              progressAtIso(entry),
            );
          }
        });

      if (event === 'completed' || event === 'failed') {
        closeReasoning(targetSessionId);
      }
    },
    [
      closeReasoning,
      emitReasoning,
      emitToolCall,
      emitToolResult,
      progressAtIso,
      progressAtMs,
      sessionId,
      toolArguments,
      toolResultText,
    ],
  );

  const setPanelRef = useCallback((panel: VideoLivePanelHandle | null) => {
    panelRef.current = panel;
    registerTaskFullDuplexController(panel, (targetSessionId) => {
      runtimeSessionIdRef.current = targetSessionId;
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
    if (runtimeSessionIdRef.current && runtimeSessionIdRef.current !== sessionId) {
      closeReasoning(runtimeSessionIdRef.current);
      stopTaskFullDuplex();
      runtimeSessionIdRef.current = null;
      processedCoreProgressRef.current.clear();
      coreToolIdsRef.current.clear();
      persistedAssistantStreamsRef.current.clear();
    }
    previousSessionIdRef.current = sessionId;
  }, [closeReasoning, sessionId]);

  return (
    <VideoLivePanel
      ref={setPanelRef}
      headless
      onConversationItem={(role, text) => {
        const targetSessionId = runtimeSessionIdRef.current || sessionId;
        if (!targetSessionId) return;
        onConversationItem(targetSessionId, role, text);
        persistTimelineEvent(targetSessionId, {
          kind: role,
          content: text,
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
          const oldest = persistedAssistantStreamsRef.current.values().next().value;
          if (oldest) persistedAssistantStreamsRef.current.delete(oldest);
        }
        persistTimelineEvent(targetSessionId, {
          kind: 'assistant',
          content: update.content,
          timestamp: Date.now() / 1_000,
        });
      }}
      onRuntimeState={(state) => {
        if (state === 'starting') persistedAssistantStreamsRef.current.clear();
        if (state === 'starting' || state === 'active') {
          runtimeSessionIdRef.current ||= sessionId;
        }
        if (state === 'idle') runtimeSessionIdRef.current = null;
        setTaskFullDuplexRuntimeState(state);
      }}
      onError={setTaskFullDuplexRuntimeError}
      onCoreAgentProgress={handleCoreAgentProgress}
    />
  );
}
