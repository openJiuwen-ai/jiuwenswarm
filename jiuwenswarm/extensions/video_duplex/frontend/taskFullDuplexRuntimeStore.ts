import { useSyncExternalStore } from "react";
import { toast } from "../../../channels/web/frontend/src/components/ui/Toast/toastStore";

import type { VideoLivePanelHandle } from "./VideoLivePanel";
import { isTaskDuplexScreenSharingEnabled } from "../../../channels/web/frontend/src/features/taskFullDuplex/mediaPreferences";
import { describeDuplexError } from "./duplexErrorMessage";

export type TaskFullDuplexRuntimeState = "idle" | "starting" | "active";

interface TaskFullDuplexRuntimeSnapshot {
  state: TaskFullDuplexRuntimeState;
  error: string;
  microphoneMuted: boolean;
  speakerMuted: boolean;
  screenSharing: boolean;
}

let controller: VideoLivePanelHandle | null = null;
let bindSession: ((sessionId: string) => string) | null = null;
let snapshot: TaskFullDuplexRuntimeSnapshot = {
  state: "idle",
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
  listeners.forEach((listener) => listener());
}

export function registerTaskFullDuplexController(
  next: VideoLivePanelHandle | null,
  nextBindSession?: ((sessionId: string) => string) | null,
): void {
  controller = next;
  bindSession = next ? (nextBindSession ?? null) : null;
}

export function setTaskFullDuplexRuntimeState(
  state: TaskFullDuplexRuntimeState,
): void {
  publish(
    state === "idle"
      ? {
          state,
          microphoneMuted: false,
          speakerMuted: false,
          screenSharing: false,
        }
      : { state },
  );
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

export async function startTaskFullDuplex(sessionId?: string, visualSource?: 'screen' | 'camera'): Promise<void> {
  if (!controller) {
    publish({ state: "idle", error: "全双工运行时尚未就绪，请稍后重试。" });
    return;
  }
  const searchSessionId = sessionId ? bindSession?.(sessionId) : undefined;
  publish({ state: "starting", error: "" });
  try {
    const started = await controller.startScreenDuplex(
      searchSessionId,
      isTaskDuplexScreenSharingEnabled(),
      visualSource,
    );
    if (!started) publish({ state: "idle" });
  } catch (error) {
    publish({
      state: "idle",
      error: error instanceof Error ? error.message : "无法启动全双工会话。",
    });
  }
}

export function stopTaskFullDuplex(): void {
  controller?.stop();
  publish({
    state: "idle",
    error: "",
    microphoneMuted: false,
    speakerMuted: false,
    screenSharing: false,
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
