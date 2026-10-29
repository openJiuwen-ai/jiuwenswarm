import {
  normalizeSubagentActivityEvent,
  normalizeSubagentStatusEvent,
} from "../../../channels/web/frontend/src/features/subagent/subagentNormalizer";
import { useSubagentStore } from "../../../channels/web/frontend/src/stores/subagentStore";

/**
 * Apply a Core Agent subagent frame carried by a voice task to the chat's subagent panel,
 * using the same normalization and store as typed `chat.subtask_update` / `chat.subagent_activity`.
 */
export function applyDuplexSubagentEvent(sessionId: string, raw: unknown): void {
  if (!sessionId || !raw || typeof raw !== "object") return;
  // The task snapshot was already authorized for this conversation. Managed tasks run in
  // their own Core session, so bind the frame to the visible conversation it belongs to.
  const scoped = {
    ...(raw as Record<string, unknown>),
    session_id: sessionId,
    parent_session_id: sessionId,
  };
  const store = useSubagentStore.getState();
  const status = normalizeSubagentStatusEvent(scoped);
  if (status) {
    store.applyEvent(sessionId, status);
    return;
  }
  const event = normalizeSubagentActivityEvent(scoped);
  if (!event) return;
  store.applyEvent(sessionId, event);
  const { subagent_id: subagentId, task_id: taskId, at_ms: atMs } = event.activity;
  const description =
    store.getRuntime(sessionId)?.subagentsById[subagentId]?.task_description ?? "";
  if (description.trim())
    store.applyTurn(sessionId, subagentId, taskId, description, atMs);
}
