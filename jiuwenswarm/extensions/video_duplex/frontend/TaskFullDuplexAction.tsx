import { useCallback, useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
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
  exitTaskFullDuplex,
  useTaskFullDuplexRuntime,
  toggleTaskDuplexMicrophone,
  toggleTaskDuplexSpeaker,
  stopTaskDuplexScreenSharing,
  clearTaskFullDuplexError,
} from './taskFullDuplexRuntimeStore';
import './TaskFullDuplexAction.css';
import { DuplexOrb } from './DuplexOrb';

// The orb floats above the docked composer stack: the input frame plus any
// card or bar stacked over it, such as a pending approval.
const COMPOSER_DOCK_SELECTOR = '.chat-compose, .chat-input-frame, [data-testid="interaction-slot-root"]';
const COMPOSER_DOCK_GAP = 12;

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
  const [shareDialog, setShareDialog] = useState(false);
  const [shareSource, setShareSource] = useState<'screen' | 'camera'>('screen');
  const enabled = useTaskFullDuplexEnabled();
  const { state: runtimeState, sessionId: runtimeSessionId, error } = useTaskFullDuplexRuntime();
  const state = runtimeSessionId === sessionId ? runtimeState : 'idle';

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
      exitTaskFullDuplex();
      return;
    }
    if (isTaskDuplexScreenSharingEnabled()) {
      setShareDialog(true);
      return;
    }
    await beginSession();
  }, [beginSession, state]);

  const showAction = enabled && eligible;
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

// Mounted with the app runtime, independently of any conversation composer.
export function TaskFullDuplexControls() {
  const { t } = useTranslation();
  const { state, microphoneMuted, speakerMuted, screenSharing } = useTaskFullDuplexRuntime();
  const running = state !== 'idle';
  const [orbMounted, setOrbMounted] = useState(running);
  const [orbVariant, setOrbVariant] = useState<'starting' | 'active'>('starting');
  useEffect(() => {
    if (running) {
      setOrbMounted(true);
      setOrbVariant(state);
      return;
    }
    const timer = window.setTimeout(() => setOrbMounted(false), 200);
    return () => window.clearTimeout(timer);
  }, [running, state]);
  const controlsRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const constrainPosition = useCallback((left: number, top: number) => {
    const bounds = controlsRef.current?.getBoundingClientRect();
    return {
      left: Math.max(8, Math.min(left, window.innerWidth - (bounds?.width ?? 80) - 8)),
      top: Math.max(8, Math.min(top, window.innerHeight - (bounds?.height ?? 116) - 8)),
    };
  }, []);

  // Until the user drags the orb, keep it clear of the composer and its cards.
  const [dockBottom, setDockBottom] = useState<number | null>(null);
  useEffect(() => {
    if (!orbMounted || position) return;
    let observed: Element[] = [];
    // Page mutations arrive per React commit while replies stream; measure (a forced
    // layout) at most once per frame.
    let frame = 0;
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(() => { frame = 0; update(); });
    };
    const resizeObserver = typeof window.ResizeObserver === 'undefined' ? null : new window.ResizeObserver(schedule);
    const update = () => {
      const docked = Array.from(document.querySelectorAll(COMPOSER_DOCK_SELECTOR));
      if (resizeObserver && (docked.length !== observed.length || docked.some((el, i) => el !== observed[i]))) {
        resizeObserver.disconnect();
        docked.forEach((el) => resizeObserver.observe(el));
        observed = docked;
      }
      let top = Infinity;
      for (const el of docked) {
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) top = Math.min(top, rect.top);
      }
      if (!Number.isFinite(top)) {
        setDockBottom(null);
        return;
      }
      const height = controlsRef.current?.offsetHeight || 116;
      setDockBottom(Math.max(8, Math.min(window.innerHeight - top + COMPOSER_DOCK_GAP, window.innerHeight - height - 8)));
    };
    update();
    const mutationObserver = new window.MutationObserver(schedule);
    mutationObserver.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', schedule);
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
      window.removeEventListener('resize', schedule);
    };
  }, [orbMounted, position]);

  useEffect(() => {
    const keepVisible = () => setPosition((current) => current
      ? constrainPosition(current.left, current.top) : current);
    keepVisible();
    window.addEventListener('resize', keepVisible);
    return () => window.removeEventListener('resize', keepVisible);
  }, [running, screenSharing, constrainPosition]);

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !event.isPrimary) return;
    const bounds = controlsRef.current?.getBoundingClientRect();
    if (!bounds) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY, left: bounds.left, top: bounds.top };
    setPosition(constrainPosition(bounds.left, bounds.top));
    setDragging(true);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    setPosition(constrainPosition(drag.left + event.clientX - drag.x, drag.top + event.clientY - drag.y));
  };

  const endDrag = () => {
    dragRef.current = null;
    setDragging(false);
  };

  return (
    <>
      {running || orbMounted
        ? createPortal(
            <div
              ref={controlsRef}
              className={`task-full-duplex-orb${dragging ? ' is-dragging' : ''}${running ? '' : ' is-leaving'}`}
              style={
                position
                  ? { ...position, bottom: 'auto', transform: 'none' }
                  : dockBottom !== null
                    ? { bottom: dockBottom }
                    : undefined
              }
              data-testid="chat-panel-duplex-controls"
              data-variant={state}
            >
              {screenSharing ? (
                <div className="task-full-duplex-sharing" role="status" data-testid="chat-panel-duplex-sharing-indicator">
                  <span className="task-full-duplex-sharing__dot" aria-hidden="true" />
                  <span>{t('chat.duplexControls.sharingVideo')}</span>
                  <button type="button" onClick={stopTaskDuplexScreenSharing} data-testid="chat-panel-duplex-close-sharing">
                    {t('common.close')}
                  </button>
                </div>
              ) : null}
              <div
                className="task-full-duplex-orb__handle"
                onPointerDown={startDrag}
                onPointerMove={moveDrag}
                onPointerUp={endDrag}
                onPointerCancel={endDrag}
                onLostPointerCapture={endDrag}
                role="status"
                aria-label={t(
                  state === 'starting' ? 'chat.duplexControls.connecting' : 'chat.duplexControls.listening',
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
                  onClick={exitTaskFullDuplex}
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
            document.body,
          )
        : null}
    </>
  );
}
