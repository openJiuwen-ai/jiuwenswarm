import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, LoaderCircle, RotateCw, Square, UsersRound, VolumeX, X } from 'lucide-react';
import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { useAdaptiveTooltip } from '../../../channels/web/frontend/src/hooks/useAdaptiveTooltip';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../../../channels/web/frontend/src/components/ui/DropdownMenu/DropdownMenu';
import {
  setTeamMeetingEnabled,
  useTeamMeetingEnabled,
} from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { publishMeeting, startMeeting, stopMeeting, stopMeetingAudio, useMeetingSnapshot } from './taskMeetingStore';
import './TaskMeetingAction.css';

export function TaskMeetingAction({ fallback, sessionId }: ApplicationPluginTaskInputActionProps) {
  const { t } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({
    placement: 'top',
    multiline: true,
  });
  const snapshot = useMeetingSnapshot();
  const [controlsOpen, setControlsOpen] = useState(false);
  const active = snapshot.sessionId === sessionId;
  const requested = active && snapshot.enabled;
  const status =
    (active || snapshot.state === 'closing' || snapshot.state === 'cleanup_failed') && snapshot.state !== 'idle'
      ? snapshot.state
      : requested
        ? 'active'
        : 'idle';
  const teamReady = !!sessionId && sessionId !== 'new' && active && snapshot.teamExists;
  const needsTeam =
    !teamReady && !requested && status !== 'starting' && status !== 'closing' && status !== 'cleanup_failed';

  useEffect(() => {
    setControlsOpen(false);
  }, [sessionId]);
  useEffect(() => {
    if (status === 'idle') setControlsOpen(false);
    else if (status === 'failed' || status === 'cleanup_failed') setControlsOpen(true);
  }, [status]);

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      void webClient
        .request<Record<string, unknown>>('config.get')
        .then((config) => {
          if (!cancelled) setTeamMeetingEnabled(config.team_meeting_enabled);
        })
        .catch(() => {
          if (!cancelled) retry = setTimeout(load, 2000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
    };
  }, []);
  const toggle = () => {
    if (status === 'cleanup_failed') return stopMeeting();
    if ((!requested || status === 'failed') && sessionId && teamReady && status !== 'starting') {
      publishMeeting({ state: 'starting', error: undefined });
      void startMeeting(sessionId).catch((error) => {
        publishMeeting({
          state: 'failed',
          sessionId,
          error: error instanceof Error ? error.message : 'meetingStartFailed',
        });
      });
      return;
    }
    if (requested || status === 'starting') stopMeeting();
  };
  const title = needsTeam
    ? t('meeting.task.teamRequired')
    : t(
        `meeting.task.${status === 'idle' ? 'enable' : status === 'starting' ? 'enabling' : status === 'closing' ? 'disabling' : status === 'cleanup_failed' ? 'retryCleanup' : status === 'failed' ? 'retryEnable' : 'stop'}`,
      );
  const activationError =
    status === 'failed'
      ? t(`meeting.errors.${snapshot.error || 'meetingStartFailed'}`, {
          defaultValue: snapshot.error || t('meeting.errors.meetingStartFailed'),
        })
      : '';
  const playbackStatus =
    status === 'active' && snapshot.playback !== 'idle'
      ? t(`meeting.playback.${snapshot.playback}`, {
          speaker: snapshot.speaker,
          voice: snapshot.voice,
        })
      : '';
  const statusDescription = [
    `${t('meeting.panel.title')}: ${t(`meeting.panel.states.${status}`)}`,
    title,
    activationError,
    status === 'cleanup_failed' ? t('meeting.errors.cleanupFailed') : '',
    status === 'cleanup_failed' ? snapshot.error : '',
    playbackStatus,
  ]
    .filter(Boolean)
    .join('\n');
  const stopLabel = t(
    status === 'cleanup_failed'
      ? 'meeting.task.retryCleanup'
      : status === 'closing'
        ? 'meeting.task.closing'
        : 'meeting.task.stop',
  );
  const canStopAudio = status === 'active' && ['loading', 'playing'].includes(snapshot.playback);
  const dismissFailure = () => {
    if (requested) stopMeeting();
    else publishMeeting({ state: 'idle', error: undefined });
  };
  return (
    <>
      {(enabled || status !== 'idle') && (
        <div className="task-meeting-actions" data-testid="chat-panel-meeting-controls" data-variant={status}>
          <DropdownMenu open={controlsOpen} onOpenChange={setControlsOpen}>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className={`chat-input-btn task-meeting-action is-${status}`}
                data-tooltip={controlsOpen ? undefined : statusDescription}
                {...tooltipHandlers}
                aria-label={status === 'idle' ? title : `${t('meeting.controls.title')}: ${t(`meeting.panel.states.${status}`)}`}
                aria-pressed={requested}
                aria-busy={status === 'starting' || status === 'closing'}
                disabled={status === 'idle' && needsTeam}
                data-testid="chat-panel-input-meeting"
                data-variant={status}
                onClick={() => {
                  if (status === 'idle') toggle();
                }}
              >
                {status === 'starting' || status === 'closing' ? (
                  <LoaderCircle className="chat-input-btn-icon task-meeting-spinner" aria-hidden="true" />
                ) : (
                  <UsersRound className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
                )}
                {status !== 'idle' && (
                  <>
                    <span className="task-meeting-action__label" data-testid="chat-panel-meeting-trigger-status">
                      {t(`meeting.panel.states.${status}`)}
                    </span>
                    <ChevronDown className="task-meeting-action__chevron" size={12} aria-hidden="true" />
                  </>
                )}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              side="top"
              align="end"
              className="task-meeting-layer"
              data-testid="chat-panel-meeting-layer"
              data-variant={status}
            >
              <div className="task-meeting-layer__header">
                <div className="task-meeting-layer__heading">
                  <UsersRound size={17} aria-hidden="true" />
                  <strong data-testid="chat-panel-meeting-layer-title">{t('meeting.controls.title')}</strong>
                </div>
                <DropdownMenuItem
                  className="task-meeting-layer__minimize"
                  aria-label={t('meeting.controls.minimize')}
                  title={t('meeting.controls.minimize')}
                  icon={<ChevronDown size={16} />}
                  data-testid="chat-panel-meeting-minimize"
                >
                  <span className="sr-only">{t('meeting.controls.minimize')}</span>
                </DropdownMenuItem>
              </div>
              <div className="task-meeting-layer__body">
                <span
                  className="task-meeting-layer__status"
                  role="status"
                  data-testid="chat-panel-meeting-layer-status"
                  data-variant={status}
                >
                  <span className="task-meeting-layer__dot" aria-hidden="true" />
                  {t(`meeting.panel.states.${status}`)}
                </span>
                {status === 'active' && (
                  <p
                    className="task-meeting-layer__message"
                    role="status"
                    data-testid="chat-panel-meeting-playback"
                    data-variant={snapshot.playback}
                  >
                    {snapshot.playback === 'idle' ? t('meeting.panel.noSpeaker') : playbackStatus}
                  </p>
                )}
                {status === 'failed' && (
                  <p className="task-meeting-layer__error" role="alert" data-testid="chat-panel-meeting-activation-error">
                    {activationError}
                  </p>
                )}
                {status === 'cleanup_failed' && (
                  <p className="task-meeting-layer__error" role="alert" data-testid="chat-panel-meeting-cleanup-error">
                    {t('meeting.errors.cleanupFailed')}
                    {snapshot.error ? ` ${snapshot.error}` : ''}
                  </p>
                )}
              </div>
              <div className="task-meeting-layer__actions" data-testid="chat-panel-meeting-layer-controls">
                {status === 'active' && (
                  <DropdownMenuItem
                    className="task-meeting-layer__control"
                    icon={<VolumeX size={18} />}
                    disabled={!canStopAudio}
                    onSelect={stopMeetingAudio}
                    data-testid="chat-panel-input-meeting-stop-audio"
                  >
                    {t('meeting.stopPlayback')}
                  </DropdownMenuItem>
                )}
                {status === 'failed' && (
                  <DropdownMenuItem
                    className="task-meeting-layer__control"
                    icon={<RotateCw size={18} />}
                    disabled={!teamReady}
                    onSelect={toggle}
                    data-testid="chat-panel-meeting-retry-enable"
                  >
                    {t('meeting.task.retryEnable')}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  className="task-meeting-layer__control"
                  danger
                  icon={status === 'closing'
                    ? <LoaderCircle size={16} className="task-meeting-spinner" />
                    : status === 'failed' ? <X size={18} /> : <Square size={15} />}
                  disabled={status === 'closing'}
                  onSelect={status === 'failed' ? dismissFailure : stopMeeting}
                  data-testid="chat-panel-meeting-layer-end"
                >
                  {stopLabel}
                </DropdownMenuItem>
              </div>
            </DropdownMenuContent>
          </DropdownMenu>
          {tooltip}
        </div>
      )}
      {fallback}
    </>
  );
}
