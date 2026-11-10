import { useTranslation } from 'react-i18next';
import { Square, UsersRound, VolumeX } from 'lucide-react';
import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { stopMeeting, stopMeetingAudio, useMeetingSnapshot } from './taskMeetingStore';
import { Button } from '../../../channels/web/frontend/src/components/ui/Button/Button';

export function MeetingPanel({ sessionId }: { sessionId: string | null }) {
  const { t, i18n } = useTranslation();
  const snapshot = useMeetingSnapshot();
  const runtime = useSessionStore((state) => state.runtimes[sessionId || '']);
  const ownsMeeting = !!sessionId && snapshot.sessionId === sessionId;
  const state = ownsMeeting ? (snapshot.state === 'idle' && snapshot.enabled ? 'active' : snapshot.state) : 'idle';
  const active = state === 'active';
  const canEnd = ['starting', 'active', 'cleanup_failed', 'failed'].includes(state);
  const leader = resolveTeamLeaderDisplayName(runtime?.teamLeaderIdentity, i18n.language) || t('meeting.leader');
  const addressedAgents = snapshot.addressedMembers.map((name) => {
    if (runtime?.teamLeaderMemberIds.includes(name)) return `@${leader}`;
    const member = runtime?.teamMembers.find((item) => item.member_id === name);
    return `@${member?.name || name}`;
  });

  return (
    <section className="min-w-0 flex-1 overflow-y-auto bg-card p-6" data-testid="team-area-meeting-panel">
      <div className="mx-auto flex max-w-[820px] flex-col gap-6">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-text" data-testid="team-area-meeting-title">
            <UsersRound size={20} aria-hidden="true" />
            {t('meeting.panel.title')}
          </h2>
          <span
            className="rounded-full bg-[var(--color-tag-surface)] px-3 py-1 text-sm text-text-secondary"
            role="status"
            data-testid="team-area-meeting-status"
            data-variant={state}
          >
            {t(`meeting.panel.states.${state}`)}
          </span>
        </header>

        {state === 'idle' ? (
          <p className="text-sm leading-6 text-text-secondary" data-testid="team-area-meeting-empty">
            {t('meeting.panel.empty')}
          </p>
        ) : (
          <>
            <dl
              className="grid gap-5 rounded-lg border border-border p-4 sm:grid-cols-2"
              data-testid="team-area-meeting-details"
            >
              <div>
                <dt className="mb-1 text-xs text-text-secondary" data-testid="team-area-meeting-addressed-label">{t('meeting.panel.addressedAgents')}</dt>
                <dd className="text-sm font-medium text-text" data-testid="team-area-meeting-addressed-agents">
                  {addressedAgents.length ? addressedAgents.join(', ') : t('meeting.panel.noAddressedAgents')}
                </dd>
              </div>
              <div className="col-span-full">
                <dt className="mb-1 text-xs text-text-secondary" data-testid="team-area-meeting-playback-label">{t('meeting.panel.playback')}</dt>
                <dd className="text-sm text-text" data-testid="team-area-meeting-playback">
                  {snapshot.playback === 'idle'
                    ? t('meeting.panel.noSpeaker')
                    : t(`meeting.playback.${snapshot.playback}`, {
                        speaker: snapshot.speaker,
                        voice: snapshot.voice,
                      })}
                </dd>
              </div>
            </dl>

            <div className="flex flex-wrap gap-2" data-testid="team-area-meeting-controls">
              <Button
                type="button"
                icon={<VolumeX size={16} aria-hidden="true" />}
                disabled={!active || snapshot.playback === 'idle'}
                onClick={stopMeetingAudio}
                data-testid="team-area-meeting-stop-audio-btn"
              >
                {t('meeting.stopPlayback')}
              </Button>
              <Button
                type="button"
                icon={<Square size={16} aria-hidden="true" />}
                disabled={!canEnd}
                onClick={stopMeeting}
                data-testid="team-area-meeting-end-btn"
              >
                {t(
                  state === 'cleanup_failed'
                    ? 'meeting.task.retryCleanup'
                    : state === 'closing'
                      ? 'meeting.task.closing'
                      : 'meeting.task.stop',
                )}
              </Button>
            </div>

            {(state === 'cleanup_failed' || state === 'failed') && (
              <p className="text-sm text-warn" role="alert" data-testid="team-area-meeting-cleanup-error">
                {t(state === 'cleanup_failed' ? 'meeting.errors.cleanupFailed' : 'meeting.errors.meetingStartFailed')}
                {snapshot.error ? ` ${t(`meeting.errors.${snapshot.error}`, { defaultValue: snapshot.error })}` : ''}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
