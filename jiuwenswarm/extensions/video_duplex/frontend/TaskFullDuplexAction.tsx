import { useCallback, useEffect, useState } from 'react';
import {
  Camera,
  Monitor,
  LoaderCircle,
  Mic,
  MicOff,
  MonitorOff,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react';
import { createPortal } from 'react-dom';
import { FormDialog } from '../../../channels/web/frontend/src/components/form';
import { isTaskDuplexScreenSharingEnabled } from '../../../channels/web/frontend/src/features/taskFullDuplex/mediaPreferences';
import { useTranslation } from 'react-i18next';

import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import {
  useTaskFullDuplexEnabled,
  setTaskFullDuplexEnabled,
} from '../../../channels/web/frontend/src/features/taskFullDuplex/featureFlag';
import { setTaskAsrEnabled } from '../../../channels/web/frontend/src/features/taskAsr/featureFlag';
import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import {
  startTaskFullDuplex,
  stopTaskFullDuplex,
  useTaskFullDuplexRuntime,
  toggleTaskDuplexMicrophone,
  toggleTaskDuplexSpeaker,
  stopTaskDuplexScreenSharing,
  clearTaskFullDuplexError,
} from './taskFullDuplexRuntimeStore';
import { DuplexOrb } from './DuplexOrb';
import './TaskFullDuplexAction.css';

// 来源：TaskFullDuplexAction.css 的 task-full-duplex-orb-leave 动画为 180ms，留 20ms 余量让它放完再卸载。
const ORB_LEAVE_MS = 200;

function parseEnabled(value: unknown): boolean {
  return value === true || String(value).trim().toLowerCase() === 'true';
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
  const [orbHost, setOrbHost] = useState<Element | null>(null);
  const [shareDialog, setShareDialog] = useState(false);
  const [shareSource, setShareSource] = useState<'screen' | 'camera'>('screen');
  const bindAnchor = useCallback((node: HTMLSpanElement | null) => {
    setOrbHost(node?.closest('.chat-input-frame') || null);
  }, []);
  const enabled = useTaskFullDuplexEnabled();
  const { state, error, microphoneMuted, speakerMuted, screenSharing } = useTaskFullDuplexRuntime();

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;
    const load = () => {
      void webRequest<Record<string, unknown>>('config.get', {})
        .then((config) => {
          if (!cancelled) {
            setTaskFullDuplexEnabled(parseEnabled(config.task_full_duplex_enabled));
            setTaskAsrEnabled(config.task_asr_enabled !== undefined && parseEnabled(config.task_asr_enabled));
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

  // A failure belongs to the conversation it happened in (#5288).
  useEffect(() => {
    clearTaskFullDuplexError();
  }, [sessionId]);

  const beginSession = useCallback(
    async (visualSource?: 'screen' | 'camera') => {
      setShareDialog(false);
      const readySessionId = await ensureSession('');
      if (readySessionId) await startTaskFullDuplex(readySessionId, visualSource);
    },
    [ensureSession],
  );

  const handleClick = useCallback(async () => {
    if (state !== 'idle') {
      stopTaskFullDuplex();
      return;
    }
    if (isTaskDuplexScreenSharingEnabled()) {
      setShareDialog(true);
      return;
    }
    await beginSession();
  }, [beginSession, state]);

  const showAction = enabled && eligible;
  const running = enabled && state !== 'idle';
  // Keep the orb mounted briefly after stop/failure so it fades out instead
  // of vanishing mid-frame.
  const [orbMounted, setOrbMounted] = useState(running);
  // Last non-idle state, so the fading orb keeps its look.
  const [orbVariant, setOrbVariant] = useState<'starting' | 'active'>('starting');
  if (state !== 'idle' && state !== orbVariant) setOrbVariant(state);
  useEffect(() => {
    if (running) {
      setOrbMounted(true);
      return undefined;
    }
    const timer = window.setTimeout(() => setOrbMounted(false), ORB_LEAVE_MS);
    return () => window.clearTimeout(timer);
  }, [running]);
  const title = error || (state === 'starting' ? labels.starting : state === 'active' ? labels.stop : labels.start);

  return (
    <>
      {shareDialog ? (
        <FormDialog
          open
          title={t('chat.duplexControls.chooseSource')}
          description={t('chat.duplexControls.chooseSourceDescription')}
          confirmLabel={t('common.confirm')}
          cancelLabel={t('common.cancel')}
          onConfirm={() => void beginSession(shareSource)}
          onCancel={() => setShareDialog(false)}
          testIdPrefix="chat-panel-duplex-share-dialog"
        >
          <div
            className="task-full-duplex-sources"
            role="radiogroup"
            aria-label={t('chat.duplexControls.chooseSource')}
          >
            {(['screen', 'camera'] as const).map((source) => (
              <button
                key={source}
                type="button"
                role="radio"
                aria-checked={shareSource === source}
                onClick={() => setShareSource(source)}
                data-testid="chat-panel-duplex-source"
                data-variant={source}
              >
                {source === 'screen' ? <Monitor aria-hidden /> : <Camera aria-hidden />}
                {t(source === 'screen' ? 'chat.duplexControls.screenOrWindow' : 'videoLive.sources.camera')}
              </button>
            ))}
          </div>
        </FormDialog>
      ) : null}
      {running && screenSharing
        ? createPortal(
            <div className="task-full-duplex-sharing" role="status" data-testid="chat-panel-duplex-sharing-indicator">
              <span className="task-full-duplex-sharing__dot" aria-hidden="true" />
              <span>{t('chat.duplexControls.sharingVideo')}</span>
              <button type="button" onClick={stopTaskDuplexScreenSharing} data-testid="chat-panel-duplex-close-sharing">
                {t('common.close')}
              </button>
            </div>,
            document.body,
          )
        : null}
      <span
        ref={bindAnchor}
        className="task-full-duplex-anchor"
        aria-hidden="true"
        data-testid="chat-panel-duplex-anchor"
      />
      {(running || orbMounted) && orbHost
        ? createPortal(
            <div
              className={`task-full-duplex-orb${running ? '' : ' is-leaving'}`}
              data-testid="chat-panel-duplex-controls"
              data-variant={orbVariant}
            >
              <div
                role="status"
                aria-label={t(
                  orbVariant === 'starting' ? 'chat.duplexControls.connecting' : 'chat.duplexControls.listening',
                )}
                data-testid="chat-panel-duplex-orb"
              >
                <DuplexOrb variant={orbVariant} microphoneMuted={running && microphoneMuted} />
              </div>
              <div className="task-full-duplex-orb__controls">
                <button
                  type="button"
                  onClick={toggleTaskDuplexMicrophone}
                  disabled={state !== 'active'}
                  aria-pressed={microphoneMuted}
                  aria-label={t(microphoneMuted ? 'chat.duplexControls.unmuteMic' : 'chat.duplexControls.muteMic')}
                  title={t(microphoneMuted ? 'chat.duplexControls.unmuteMic' : 'chat.duplexControls.muteMic')}
                  data-testid="chat-panel-duplex-microphone"
                >
                  {microphoneMuted ? <MicOff aria-hidden /> : <Mic aria-hidden />}
                </button>
                <button
                  type="button"
                  onClick={stopTaskFullDuplex}
                  aria-label={t('chat.duplexControls.exit')}
                  title={t('chat.duplexControls.exit')}
                  data-testid="chat-panel-duplex-exit"
                >
                  <X aria-hidden />
                </button>
                <button
                  type="button"
                  onClick={toggleTaskDuplexSpeaker}
                  disabled={state !== 'active'}
                  aria-pressed={speakerMuted}
                  aria-label={t(speakerMuted ? 'chat.duplexControls.unmuteSpeaker' : 'chat.duplexControls.muteSpeaker')}
                  title={t(speakerMuted ? 'chat.duplexControls.unmuteSpeaker' : 'chat.duplexControls.muteSpeaker')}
                  data-testid="chat-panel-duplex-speaker"
                >
                  {speakerMuted ? <VolumeX aria-hidden /> : <Volume2 aria-hidden />}
                </button>
                {screenSharing ? (
                  <button
                    type="button"
                    onClick={stopTaskDuplexScreenSharing}
                    aria-label={t('chat.duplexControls.stopSharing')}
                    title={t('chat.duplexControls.stopSharing')}
                    data-testid="chat-panel-duplex-stop-sharing"
                  >
                    <MonitorOff aria-hidden />
                  </button>
                ) : null}
              </div>
            </div>,
            orbHost,
          )
        : null}
      {showAction && !hasTextDraft ? (
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
            <LoaderCircle className="chat-input-btn-icon task-full-duplex-action__spinner" aria-hidden />
          ) : state === 'active' ? (
            <X className="chat-input-btn-icon" aria-hidden />
          ) : (
            <svg className="chat-input-btn-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="M10 2.5v15M13.75 6.25v7.5M6.25 6.25v7.5M17.5 8.75v2.5M2.5 8.75v2.5"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          )}
        </button>
      ) : (
        fallback
      )}
    </>
  );
}
