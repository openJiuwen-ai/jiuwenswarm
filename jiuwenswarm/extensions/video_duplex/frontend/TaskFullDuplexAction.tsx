import { useCallback, useEffect, useState } from "react";
import {
  AudioWaveform,
  LoaderCircle,
  Mic,
  MicOff,
  MonitorOff,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import type { ApplicationPluginTaskInputActionProps } from "../../../channels/web/frontend/src/applicationPlugins/types";
import {
  useTaskFullDuplexEnabled,
  setTaskFullDuplexEnabled,
} from "../../../channels/web/frontend/src/features/taskFullDuplex/featureFlag";
import { setTaskAsrEnabled } from "../../../channels/web/frontend/src/features/taskAsr/featureFlag";
import { webRequest } from "../../../channels/web/frontend/src/services/webClient";
import {
  startTaskFullDuplex,
  stopTaskFullDuplex,
  useTaskFullDuplexRuntime,
  toggleTaskDuplexMicrophone,
  toggleTaskDuplexSpeaker,
  stopTaskDuplexScreenSharing,
} from "./taskFullDuplexRuntimeStore";
import "./TaskFullDuplexAction.css";

function parseEnabled(value: unknown): boolean {
  return value === true || String(value).trim().toLowerCase() === "true";
}

export function TaskFullDuplexAction({
  fallback,
  eligible,
  sessionId,
  ensureSession,
  labels,
  hasTextDraft = false,
}: ApplicationPluginTaskInputActionProps) {
  const { t } = useTranslation();
  const enabled = useTaskFullDuplexEnabled();
  const { state: runtimeState, sessionId: runtimeSessionId, error } = useTaskFullDuplexRuntime();
  const state = runtimeSessionId === sessionId ? runtimeState : "idle";

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;
    const load = () => {
      void webRequest<Record<string, unknown>>("config.get", {})
        .then((config) => {
          if (!cancelled) {
            setTaskFullDuplexEnabled(
              parseEnabled(config.task_full_duplex_enabled),
            );
            setTaskAsrEnabled(
              config.task_asr_enabled !== undefined &&
                parseEnabled(config.task_asr_enabled),
            );
          }
        })
        .catch(() => {
          if (!cancelled) retryTimer = window.setTimeout(load, 2_000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, []);

  const handleClick = useCallback(async () => {
    if (state !== "idle") {
      stopTaskFullDuplex();
      return;
    }
    // Leave the title empty so Jiuwen can name it from the first real user turn.
    const readySessionId = await ensureSession("");
    if (!readySessionId) return;
    await startTaskFullDuplex(readySessionId);
  }, [ensureSession, state]);

  const showAction = enabled && eligible;
  const title =
    error ||
    (state === "starting"
      ? labels.starting
      : state === "active"
        ? labels.stop
        : labels.start);

  return (
    <>
      {showAction && !hasTextDraft ? (
        <button
          type="button"
          onClick={() => void handleClick()}
          disabled={!sessionId}
          className={`chat-input-btn chat-input-btn--send task-full-duplex-action is-${state}`}
          title={title}
          aria-label={title}
          aria-pressed={state === "active"}
          data-testid="chat-panel-input-full-duplex"
        >
          {state === "starting" ? (
            <LoaderCircle
              className="chat-input-btn-icon task-full-duplex-action__spinner"
              aria-hidden
            />
          ) : state === "active" ? (
            <X className="chat-input-btn-icon" aria-hidden />
          ) : (
            <AudioWaveform className="chat-input-btn-icon" aria-hidden />
          )}
        </button>
      ) : (
        fallback
      )}
    </>
  );
}

// Mounted with the app runtime, independently of any conversation composer.
export function TaskFullDuplexControls() {
  const { t } = useTranslation();
  const { state, microphoneMuted, speakerMuted, screenSharing } = useTaskFullDuplexRuntime();
  const running = state !== 'idle';
  return (
    <>
      {running
        ? createPortal(
            <div
              className="task-full-duplex-orb"
              data-testid="chat-panel-duplex-controls"
              data-variant={state}
            >
              <div
                className="task-full-duplex-orb__sphere"
                role="status"
                aria-label={t(
                  state === "starting"
                    ? "chat.duplexControls.connecting"
                    : "chat.duplexControls.listening",
                )}
                data-testid="chat-panel-duplex-orb"
              />
              <div className="task-full-duplex-orb__controls">
                <button
                  type="button"
                  onClick={toggleTaskDuplexMicrophone}
                  disabled={state !== "active"}
                  aria-pressed={microphoneMuted}
                  aria-label={t(
                    microphoneMuted
                      ? "chat.duplexControls.unmuteMic"
                      : "chat.duplexControls.muteMic",
                  )}
                  title={t(
                    microphoneMuted
                      ? "chat.duplexControls.unmuteMic"
                      : "chat.duplexControls.muteMic",
                  )}
                  data-testid="chat-panel-duplex-microphone"
                >
                  {microphoneMuted ? (
                    <MicOff aria-hidden />
                  ) : (
                    <Mic aria-hidden />
                  )}
                </button>
                <button
                  type="button"
                  onClick={stopTaskFullDuplex}
                  aria-label={t("chat.duplexControls.exit")}
                  title={t("chat.duplexControls.exit")}
                  data-testid="chat-panel-duplex-exit"
                >
                  <X aria-hidden />
                </button>
                <button
                  type="button"
                  onClick={toggleTaskDuplexSpeaker}
                  disabled={state !== "active"}
                  aria-pressed={speakerMuted}
                  aria-label={t(
                    speakerMuted
                      ? "chat.duplexControls.unmuteSpeaker"
                      : "chat.duplexControls.muteSpeaker",
                  )}
                  title={t(
                    speakerMuted
                      ? "chat.duplexControls.unmuteSpeaker"
                      : "chat.duplexControls.muteSpeaker",
                  )}
                  data-testid="chat-panel-duplex-speaker"
                >
                  {speakerMuted ? (
                    <VolumeX aria-hidden />
                  ) : (
                    <Volume2 aria-hidden />
                  )}
                </button>
                {screenSharing ? (
                  <button
                    type="button"
                    onClick={stopTaskDuplexScreenSharing}
                    aria-label={t("chat.duplexControls.stopSharing")}
                    title={t("chat.duplexControls.stopSharing")}
                    data-testid="chat-panel-duplex-stop-sharing"
                  >
                    <MonitorOff aria-hidden />
                  </button>
                ) : null}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
