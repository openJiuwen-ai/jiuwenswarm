import { useCallback, useEffect } from 'react';
import { AudioWaveform, LoaderCircle, Square } from 'lucide-react';

import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { useTaskFullDuplexEnabled, setTaskFullDuplexEnabled } from '../../../channels/web/frontend/src/features/taskFullDuplex/featureFlag';
import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import {
  startTaskFullDuplex,
  stopTaskFullDuplex,
  useTaskFullDuplexRuntime,
} from './taskFullDuplexRuntimeStore';
import './TaskFullDuplexAction.css';

function parseEnabled(value: unknown): boolean {
  return value === true || String(value).trim().toLowerCase() === 'true';
}

export function TaskFullDuplexAction({
  fallback,
  eligible,
  sessionId,
  ensureSession,
  labels,
}: ApplicationPluginTaskInputActionProps) {
  const enabled = useTaskFullDuplexEnabled();
  const { state, error } = useTaskFullDuplexRuntime();

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;
    const load = () => {
      void webRequest<Record<string, unknown>>('config.get', {})
        .then((config) => {
          if (!cancelled) setTaskFullDuplexEnabled(parseEnabled(config.task_full_duplex_enabled));
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
    if (state !== 'idle') {
      stopTaskFullDuplex();
      return;
    }
    const readySessionId = await ensureSession('Full-duplex conversation');
    if (!readySessionId) return;
    await startTaskFullDuplex(readySessionId);
  }, [ensureSession, state]);

  const showAction = enabled && eligible;
  const title = error || (state === 'starting'
    ? labels.starting
    : state === 'active'
      ? labels.stop
      : labels.start);

  return (
    <>
      {showAction ? (
        <button
          type="button"
          onClick={() => void handleClick()}
          disabled={!sessionId}
          className={`chat-input-btn chat-input-btn--send task-full-duplex-action is-${state}`}
          title={title}
          aria-label={title}
          aria-pressed={state === 'active'}
          data-testid="chat-panel-input-full-duplex"
        >
          {state === 'starting' ? (
            <LoaderCircle className="chat-input-btn-icon task-full-duplex-action__spinner" aria-hidden="true" />
          ) : state === 'active' ? (
            <Square className="chat-input-btn-icon" fill="currentColor" strokeWidth={1.8} aria-hidden="true" />
          ) : (
            <AudioWaveform className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
          )}
        </button>
      ) : fallback}
    </>
  );
}
