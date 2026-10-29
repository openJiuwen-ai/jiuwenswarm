import { useSyncExternalStore } from "react";
import { toast } from "../../../channels/web/frontend/src/components/ui/Toast/toastStore";

import type { VideoLivePanelHandle } from "./VideoLivePanel";
import { isTaskDuplexScreenSharingEnabled } from "../../../channels/web/frontend/src/features/taskFullDuplex/mediaPreferences";
import { setTaskFullDuplexSessionId } from "../../../channels/web/frontend/src/features/taskFullDuplex/duplexSession";
import { describeDuplexError } from "./duplexErrorMessage";

export type TaskFullDuplexRuntimeState = "idle" | "starting" | "active";

interface TaskFullDuplexRuntimeSnapshot {
  state: TaskFullDuplexRuntimeState;
  sessionId: string | null;
  error: string;
  microphoneMuted: boolean;
  speakerMuted: boolean;
  screenSharing: boolean;
}

let controller: VideoLivePanelHandle | null = null;
let bindSession: ((sessionId: string) => string | Promise<string>) | null = null;
let cancelSessionTasks: ((sessionId: string) => Promise<void>) | null = null;
let startGeneration = 0;
// What the media panel last reported, and which start it belonged to. The panel
// re-sends its state on every parent render, so only a change moves the run;
// an idle that ends a run the store already stopped must not clear a newer
// start and its sessionId (#5825).
let panelState: TaskFullDuplexRuntimeState = "idle";
let panelGeneration = 0;
let snapshot: TaskFullDuplexRuntimeSnapshot = {
  state: "idle",
  sessionId: null,
  error: "",
  microphoneMuted: false,
  speakerMuted: false,
  screenSharing: false,
};
const listeners = new Set<() => void>();
let errorToast: number | null = null;

function publish(update: Partial<TaskFullDuplexRuntimeSnapshot>): void {
  const next = { ...snapshot, ...update };
  if (next.error) next.error = describeDuplexError(next.error);
  if (
    Object.keys(next).every(
      (key) =>
        next[key as keyof typeof next] ===
        snapshot[key as keyof typeof snapshot],
    )
  )
    return;
  if (next.error && next.error !== snapshot.error) {
    if (errorToast !== null) toast.close(errorToast);
    const shownError = next.error;
    const key = toast.open({
      content: shownError,
      variant: "error",
      duration: 0,
      wide: true,
      // Dismissing the toast also retires the error, so it no longer lingers
      // as the duplex button tooltip (#5288).
      onClose: () => {
        if (errorToast !== key) return;
        errorToast = null;
        if (snapshot.error === shownError) publish({ error: "" });
      },
    });
    errorToast = key;
  }
  snapshot = next;
  setTaskFullDuplexSessionId(next.state === "idle" ? null : next.sessionId);
  listeners.forEach((listener) => listener());
}

export function registerTaskFullDuplexController(
  next: VideoLivePanelHandle | null,
  nextBindSession?: ((sessionId: string) => string | Promise<string>) | null,
  nextCancelSessionTasks?: ((sessionId: string) => Promise<void>) | null,
): void {
  controller = next;
  bindSession = next ? (nextBindSession ?? null) : null;
  cancelSessionTasks = next ? (nextCancelSessionTasks ?? null) : null;
}

const IDLE_RUN = {
  state: "idle",
  sessionId: null,
  microphoneMuted: false,
  speakerMuted: false,
  screenSharing: false,
} as const;

export function setTaskFullDuplexRuntimeState(
  state: TaskFullDuplexRuntimeState,
): void {
  if (state === panelState) return;
  panelState = state;
  if (state !== "idle") {
    panelGeneration = startGeneration;
    publish({ state });
  } else if (panelGeneration === startGeneration) {
    publish(IDLE_RUN);
  }
}

export function setTaskFullDuplexRuntimeError(error: string): void {
  publish({ error });
}

/** Drops an idle session's error and its toast, e.g. when another conversation opens. */
export function clearTaskFullDuplexError(): void {
  if (snapshot.state !== "idle" || !snapshot.error) return;
  if (errorToast !== null) toast.close(errorToast);
  publish({ error: "" });
}

export async function startTaskFullDuplex(sessionId?: string): Promise<void> {
  if (!controller) {
    publish({ state: "idle", error: "全双工运行时尚未就绪，请稍后重试。" });
    return;
  }
  // End the old media run before binding the replacement conversation.
  if (snapshot.state !== "idle") stopTaskFullDuplex();
  const generation = ++startGeneration;
  publish({ state: "starting", sessionId: sessionId ?? null, error: "" });
  try {
    const searchSessionId = sessionId ? await bindSession?.(sessionId) : undefined;
    if (generation !== startGeneration) return;
    const started = await controller.startScreenDuplex(
      searchSessionId,
      isTaskDuplexScreenSharingEnabled(),
    );
    if (generation !== startGeneration) return;
    if (!started) publish(IDLE_RUN);
  } catch (error) {
    if (generation !== startGeneration) return;
    publish({
      state: "idle",
      sessionId: null,
      error: error instanceof Error ? error.message : "无法启动全双工会话。",
    });
  }
}

export function stopTaskFullDuplex(): void {
  startGeneration += 1;
  controller?.stop();
  publish({ ...IDLE_RUN, error: "" });
}

// User exit also cancels this conversation's tasks; internal restarts use stopTaskFullDuplex (#5527).
export function exitTaskFullDuplex(): void {
  const sessionId = snapshot.sessionId;
  const cancel = cancelSessionTasks;
  stopTaskFullDuplex();
  if (!sessionId || !cancel) return;
  void cancel(sessionId).catch((error: unknown) => {
    publish({ error: error instanceof Error ? error.message : "无法停止本会话的任务。" });
  });
}

export function toggleTaskDuplexMicrophone(): void {
  if (!controller || snapshot.state !== "active") return;
  const muted = !snapshot.microphoneMuted;
  controller?.setMicrophoneMuted(muted);
  publish({ microphoneMuted: muted });
}

export function toggleTaskDuplexSpeaker(): void {
  if (!controller || snapshot.state !== "active") return;
  const muted = !snapshot.speakerMuted;
  controller?.setSpeakerMuted(muted);
  publish({ speakerMuted: muted });
}

export function setTaskDuplexScreenSharing(sharing: boolean): void {
  publish({ screenSharing: sharing });
}

export function startTaskDuplexScreenSharing(): void {
  if (!controller || snapshot.state !== "active" || snapshot.screenSharing) return;
  void controller.startScreenSharing();
}

export function stopTaskDuplexScreenSharing(): void {
  controller?.stopScreenSharing();
}

export function useTaskFullDuplexRuntime(): TaskFullDuplexRuntimeSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}
