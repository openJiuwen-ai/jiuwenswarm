import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { LoaderCircle, UsersRound, VolumeX, X } from 'lucide-react';
import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { useAdaptiveTooltip } from '../../../channels/web/frontend/src/hooks/useAdaptiveTooltip';
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
  return (
    <>
      {(enabled || status === 'closing' || status === 'cleanup_failed') && (
        <div className="task-meeting-actions" data-testid="chat-panel-meeting-controls" data-variant={status}>
          <button
            type="button"
            className={`chat-input-btn task-meeting-action is-${status}`}
            data-tooltip={statusDescription}
            {...tooltipHandlers}
            aria-label={statusDescription}
            aria-pressed={requested}
            aria-busy={status === 'starting' || status === 'closing'}
            disabled={status === 'closing' || needsTeam}
            data-testid="chat-panel-input-meeting"
            data-variant={status}
            onClick={toggle}
          >
            {status === 'starting' || status === 'closing' ? (
              <LoaderCircle className="chat-input-btn-icon task-meeting-spinner" aria-hidden="true" />
            ) : (
              <UsersRound className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
            )}
          </button>
          {tooltip}
          {status === 'failed' && (
            <>
              <span className="sr-only" role="alert" data-testid="chat-panel-meeting-activation-error">
                {activationError}
              </span>
              <button
                type="button"
                className="chat-input-btn task-meeting-action"
                title={t('meeting.task.stop')}
                aria-label={t('meeting.task.stop')}
                data-testid="chat-panel-meeting-cancel-enable"
                onClick={() => {
                  if (requested) stopMeeting();
                  else publishMeeting({ state: 'idle', error: undefined });
                }}
              >
                <X className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
              </button>
            </>
          )}
          {status === 'cleanup_failed' && (
            <span className="sr-only" role="status" data-testid="chat-panel-meeting-cleanup-error">
              {t('meeting.errors.cleanupFailed')}
              {snapshot.error ? ` ${snapshot.error}` : ''}
            </span>
          )}
          {status === 'active' && snapshot.playback !== 'idle' && (
            <span
              className="sr-only"
              role="status"
              data-testid="chat-panel-meeting-playback"
              data-variant={snapshot.playback}
            >
              {playbackStatus}
            </span>
          )}
          {status === 'active' && (
            <>
              <button
                type="button"
                className="chat-input-btn task-meeting-action"
                title={t('meeting.stopPlayback')}
                aria-label={t('meeting.stopPlayback')}
                data-testid="chat-panel-input-meeting-stop-audio"
                onClick={stopMeetingAudio}
              >
                <VolumeX className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
              </button>
            </>
          )}
        </div>
      )}
      {fallback}
    </>
  );
}
