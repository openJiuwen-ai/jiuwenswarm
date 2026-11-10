import { useEffect, useRef, useState } from 'react';
import { AudioLines, LoaderCircle, Pause, Play } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import {
  pauseVoiceStreaming,
  publishVoiceStreaming,
  startVoiceStreaming,
  stopVoiceStreaming,
  useVoiceStreamingSnapshot,
} from './streamingStore';

export function VoiceStreamingButton({
  sessionId,
  ensureSession,
  busy,
  disabled,
  onActiveChange,
}: {
  sessionId: string | null;
  ensureSession: (title?: string) => Promise<string | null>;
  busy: boolean;
  disabled: boolean;
  onActiveChange: (active: boolean) => void;
}) {
  const { t } = useTranslation();
  const snapshot = useVoiceStreamingSnapshot();
  const [creating, setCreating] = useState(false);
  const mounted = useRef(true);
  const attempt = useRef(0);
  const owns = snapshot.sessionId === sessionId;
  const state = creating ? 'starting' : owns ? snapshot.state : 'idle';
  const active = state === 'starting' || state === 'listening' || state === 'paused';
  useEffect(() => {
    onActiveChange(active);
    return () => onActiveChange(false);
  }, [active, onActiveChange]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const toggle = async () => {
    if (active) {
      attempt.current++;
      setCreating(false);
      stopVoiceStreaming();
      return;
    }
    const token = ++attempt.current;
    setCreating(true);
    try {
      const id = await ensureSession();
      if (!id || token !== attempt.current || useChatStore.getState().activeSessionId !== id) return;
      await startVoiceStreaming(id);
    } catch (error) {
      publishVoiceStreaming({
        sessionId,
        state: 'failed',
        error: error instanceof Error ? error.message : 'inputStreamFailed',
      });
    } finally {
      if (mounted.current) setCreating(false);
    }
  };
  const label = t(`voiceStreaming.${active ? 'stop' : 'start'}`);
  return (
    <div
      className="inline-flex max-w-full flex-wrap items-center gap-1"
      data-testid="chat-panel-voice-streaming-controls"
      data-variant={state}
    >
      <button
        type="button"
        className={`chat-input-btn${active ? ' chat-input-btn--recording' : ''}`}
        title={label}
        aria-label={label}
        aria-pressed={active}
        aria-busy={state === 'starting'}
        disabled={!active && (busy || disabled)}
        onClick={() => void toggle()}
        data-testid="chat-panel-voice-streaming-toggle"
      >
        {state === 'starting' ? (
          <LoaderCircle className="chat-input-btn-icon animate-spin motion-reduce:animate-none" aria-hidden="true" />
        ) : (
          <AudioLines className="chat-input-btn-icon" aria-hidden="true" />
        )}
      </button>
      {active && (
        <span role="status" className="text-xs text-text-secondary" data-testid="chat-panel-voice-streaming-status">
          {t(`voiceStreaming.states.${state}`)}
        </span>
      )}
      {(state === 'listening' || state === 'paused') && (
        <button
          type="button"
          className="chat-input-btn"
          title={t(`voiceStreaming.${state === 'paused' ? 'resume' : 'pause'}`)}
          aria-label={t(`voiceStreaming.${state === 'paused' ? 'resume' : 'pause'}`)}
          onClick={() => pauseVoiceStreaming(state === 'listening')}
          data-testid="chat-panel-voice-streaming-pause"
          aria-pressed={state === 'paused'}
        >
          {state === 'paused' ? (
            <Play className="chat-input-btn-icon" aria-hidden="true" />
          ) : (
            <Pause className="chat-input-btn-icon" aria-hidden="true" />
          )}
        </button>
      )}
      {owns && snapshot.error && (
        <span role="alert" className="text-xs text-warn" data-testid="chat-panel-voice-streaming-error">
          {t(`voiceStreaming.errors.${snapshot.error}`, { defaultValue: snapshot.error })}
        </span>
      )}
    </div>
  );
}
