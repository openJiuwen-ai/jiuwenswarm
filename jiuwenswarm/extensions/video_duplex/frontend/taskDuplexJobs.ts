import type { SearchJobPayload } from "./VideoLivePanel/types";

export interface ConversationJob {
  sessionId: string;
  payload: SearchJobPayload;
  delivered: boolean;
}

/** Conversation-owned work. Media stop/reconnect must never reset this registry. */
export class TaskDuplexJobs {
  private owners = new Map<string, string>();
  private jobs = new Map<string, ConversationJob>();

  bind(sessionId: string): string {
    if (!sessionId || sessionId === "new")
      throw new Error("A saved Jiuwen conversation is required");
    const searchSessionId = `task-duplex:${sessionId}`;
    this.owners.set(searchSessionId, sessionId);
    return searchSessionId;
  }

  accept(payload: SearchJobPayload, onRejected?: (reason: string) => void): ConversationJob | null {
    const reject = (reason: string): null => {
      onRejected?.(reason);
      return null;
    };
    const jobId = payload.job_id?.trim();
    const searchSessionId = payload.search_session_id?.trim();
    const sessionId = searchSessionId
      ? this.owners.get(searchSessionId)
      : undefined;
    if (!jobId || !sessionId) return reject("missing_job_or_unbound_session");
    const previous = this.jobs.get(jobId);
    if (previous && previous.sessionId !== sessionId) return reject("session_mismatch");
    if (previous && payload.sequence != null && previous.payload.sequence != null
        && payload.sequence < previous.payload.sequence) return reject("older_sequence");
    if (previous?.payload.status === "cancelled") return reject("already_cancelled");
    if (
      previous && (previous.payload.queue_version ?? -1) >= 2 ** 48 &&
      (payload.queue_version ?? previous.payload.queue_version ?? -1) <
        (previous.payload.queue_version ?? -1)
    )
      return reject("older_queue_version");
    if (
      previous?.payload.status === "cancelling" &&
      ["queued", "running"].includes(payload.status || "") &&
      payload.queue_version == null
    )
      return reject("cancelling_without_queue_version");
    // An in-flight status request or submission acknowledgement may arrive after completion.
    if (previous && isTerminal(previous.payload) && !isTerminal(payload))
      return reject("terminal_state_regression");
    const job = {
      sessionId,
      payload: { ...previous?.payload, ...payload, replay: payload.replay ?? false },
      delivered: previous?.delivered || (payload.replay === true && isTerminal(payload)),
    };
    this.jobs.set(jobId, job);
    return job;
  }

  pending(): ConversationJob[] {
    return [...this.jobs.values()].filter(
      (job) => !isTerminal(job.payload) || !job.delivered,
    );
  }

  takeResult(jobId: string): string | null {
    const job = this.jobs.get(jobId);
    if (!job || job.delivered || !isTerminal(job.payload)) return null;
    const payload = job.payload;
    const text =
      payload.status === "cancelled"
        ? `已停止任务：${payload.question || payload.query || "Core Agent 任务"}。已完成的操作不会自动撤销。`
        : payload.status === "failed"
          ? `Jiuwen Core Agent 未能完成任务：${payload.error?.trim() || "未知错误"}`
          : payload.display_result?.trim() || payload.result?.trim();
    if (!text) return null;
    job.delivered = true;
    return text;
  }
}

function isTerminal(payload: SearchJobPayload): boolean {
  return (
    payload.status === "completed" ||
    payload.status === "failed" ||
    payload.status === "cancelled"
  );
}
