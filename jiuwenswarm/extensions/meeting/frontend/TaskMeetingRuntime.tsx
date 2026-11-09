import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { ApplicationPluginTaskRuntimeProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { useTeamMeetingEnabled } from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';
import { rememberGroupChatInput } from '../../../channels/web/frontend/src/features/groupChatInput';
import { toast } from '../../../channels/web/frontend/src/components/ui/Toast/toastStore';
import { MeetingSession, type MeetingContext } from './meetingRuntime';
import { meetingErrorDetails, publishMeeting, registerMeetingController, useMeetingSnapshot } from './taskMeetingStore';

type ServerMeeting = {
  state: 'idle' | 'starting' | 'active' | 'closing' | 'cleanup_failed' | 'failed';
  meeting_id?: string;
  conversation_mode?: string;
  error?: string;
  activation_error?: string;
  team_exists: boolean;
  enabled: boolean;
  addressed_members?: string[];
};

type ActiveMeeting = {
  sessionId: string;
  meeting: MeetingSession;
  meetingId: string | null;
  startupId: string;
  cancellation: AbortController;
  disabled?: boolean;
  ending?: Promise<void>;
  endingRelease?: boolean;
  error?: string;
  enabling?: boolean;
  cancelled?: boolean;
  playbackReady?: boolean;
};

// Keep the browser session for same-page Stop/Start. Unmount releases its media.
const retainedMeeting: { current: ActiveMeeting | null } = { current: null };

async function enableServerMeeting(active: ActiveMeeting): Promise<string> {
  const deadline = Date.now() + 60_000;
  let startupError: string | undefined;
  const off = webClient.on('chat.error', ({ payload }) => {
    if (payload.session_id === active.sessionId)
      startupError = String(payload.error || payload.message || 'meetingStartFailed');
  });
  try {
    let server = await webClient.request<ServerMeeting>('meeting.start', {
      session_id: active.sessionId,
      startup_id: active.startupId,
    });
    while (server.state === 'starting') {
      if (active.cancellation.signal.aborted) throw new Error('disconnected');
      if (Date.now() >= deadline) throw new Error('meetingStartFailed');
      if (startupError) throw new Error(startupError);
      publishMeeting({ state: server.state });
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (active.cancellation.signal.aborted) throw new Error('disconnected');
      server = await webClient.request<ServerMeeting>('meeting.status', {
        session_id: active.sessionId,
        startup_id: active.startupId,
      });
    }
    if (active.cancellation.signal.aborted || retainedMeeting.current !== active) throw new Error('disconnected');
    if (server.state !== 'active' || !server.meeting_id) throw new Error(server.error || 'meetingStartFailed');
    if (server.conversation_mode === 'group_chat') rememberGroupChatInput(active.sessionId);
    publishMeeting({
      state: 'starting',
      enabled: server.enabled,
      teamExists: server.team_exists,
      meetingId: server.meeting_id,
      addressedMembers: server.addressed_members ?? [],
    });
    return server.meeting_id;
  } finally {
    off();
  }
}

export function TaskMeetingRuntime({ sessionId }: ApplicationPluginTaskRuntimeProps) {
  const { t, i18n } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const translator = useRef(t);
  translator.current = t;
  const snapshot = useMeetingSnapshot();
  const current = retainedMeeting;
  const mounted = useRef(true);
  const visibleSession = useRef(sessionId);
  visibleSession.current = sessionId;
  const roster = useSessionStore((state) => state.runtimes[sessionId || '']);

  const context = useCallback(
    (id: string): MeetingContext => {
      const runtime = useSessionStore.getState().getRuntime(id);
      const leader = runtime?.teamLeaderMemberIds[0] || 'team_leader';
      return {
        leader,
        members: [
          {
            member_name: leader,
            display_name:
              resolveTeamLeaderDisplayName(runtime?.teamLeaderIdentity, i18n.language) || t('meeting.leader'),
          },
          ...(runtime?.teamMembers || [])
            .filter(
              (member) =>
                member.member_id !== leader &&
                member.role !== 'human_agent' &&
                member.status.toLowerCase() !== 'shutdown' &&
                !member.workflow_run_id,
            )
            .map((member) => ({
              member_name: member.member_id,
              display_name: member.name || member.member_id,
            })),
        ],
      };
    },
    [i18n.language, t],
  );

  const endServer = useCallback(function finishServer(active: ActiveMeeting, release = true): Promise<void> {
    if (current.current !== active) return Promise.resolve();
    if (active.ending) {
      if (release && !active.endingRelease) {
        return active.ending.then(() => (current.current === active ? finishServer(active) : undefined));
      }
      return active.ending;
    }
    if (release) active.cancellation.abort();
    publishMeeting({ state: 'closing' });
    const finish = (server: ServerMeeting) => {
      if (release) active.meeting.finishCleanup();
      else active.disabled = true;
      if (current.current !== active) return;
      if (release) current.current = null;
      if (!mounted.current || visibleSession.current !== active.sessionId) return;
      publishMeeting({
        state: 'idle',
        enabled: server.enabled,
        teamExists: server.team_exists,
        sessionId: active.sessionId,
        playback: 'idle',
        speaker: '',
        voice: '',
        addressedMembers: server.addressed_members ?? [],
        error: undefined,
      });
    };
    active.endingRelease = release;
    active.ending = (async () => {
      try {
        const result = await webClient.request<ServerMeeting>(
          release ? 'meeting.release' : 'meeting.end',
          {
            session_id: active.sessionId,
            meeting_id: active.meetingId || '',
            startup_id: active.startupId,
          },
          { timeoutMs: 25000 },
        );
        if (result.state !== 'idle') throw new Error(result.error || 'cleanupPending');
        finish(result);
      } catch (error) {
        // Disabled state alone does not prove that a transport release finished.
        let settled: ServerMeeting | undefined;
        let detail = error instanceof Error ? error.message : undefined;
        try {
          const status = await webClient.request<ServerMeeting>(release ? 'meeting.release' : 'meeting.status', {
            session_id: active.sessionId,
            startup_id: active.startupId,
          });
          if (status.state === 'idle') settled = status;
          else detail = meetingErrorDetails(detail, status.activation_error, status.error);
        } catch (retryError) {
          detail = meetingErrorDetails(detail, retryError instanceof Error ? retryError.message : undefined);
        }
        if (settled) finish(settled);
        else if (current.current === active) {
          const message = meetingErrorDetails(active.error, detail);
          publishMeeting({ state: 'cleanup_failed', error: message });
          if (mounted.current)
            toast.open({
              content: meetingErrorDetails(translator.current('meeting.errors.cleanupFailed'), message),
              variant: 'error',
            });
        }
      } finally {
        active.ending = undefined;
        active.endingRelease = undefined;
      }
    })();
    return active.ending;
  }, []);

  const release = useCallback(() => {
    const active = current.current;
    if (!active) return;
    active.cancelled = true;
    active.cancellation.abort();
    active.meeting.stop();
    void endServer(active);
  }, [endServer]);

  const stop = useCallback(() => {
    const active = current.current;
    if (!active) {
      if (!sessionId || !snapshot.meetingId) return;
      publishMeeting({ state: 'closing', error: undefined });
      void webClient
        .request<ServerMeeting>('meeting.end', {
          session_id: sessionId,
          meeting_id: snapshot.meetingId,
        })
        .then((server) => {
          if (visibleSession.current === sessionId) publishMeeting({ state: 'idle', enabled: server.enabled });
        })
        .catch((error) => {
          if (visibleSession.current === sessionId)
            publishMeeting({
              state: 'failed',
              error: error instanceof Error ? error.message : 'cleanupFailed',
            });
        });
      return;
    }
    if (!active.meetingId || active.cancellation.signal.aborted || active.enabling) return release();
    void endServer(active, false);
  }, [endServer, release, sessionId, snapshot.meetingId]);

  const start = useCallback(
    async (id: string) => {
      if (!enabled) return;
      const previous = current.current;
      if (previous && (previous.sessionId !== id || previous.cancellation.signal.aborted)) {
        previous.cancellation.abort();
        previous.meeting.stop();
        await endServer(previous);
        if (current.current) throw new Error('cleanupFailed');
      } else if (previous) {
        if (previous.enabling) return;
        previous.enabling = true;
        try {
          await previous.ending;
          if (current.current !== previous || previous.cancellation.signal.aborted) return;
          if (!previous.disabled) {
            if (previous.playbackReady)
              publishMeeting({
                state: 'active',
                sessionId: id,
                error: undefined,
              });
            return;
          }
          publishMeeting({ state: 'starting', sessionId: id });
          try {
            const meetingId = await enableServerMeeting(previous);
            if (meetingId !== previous.meetingId) throw new Error('meetingStartFailed');
            previous.disabled = false;
            previous.meeting.resume();
            publishMeeting({ state: 'active' });
          } catch (error) {
            previous.error = error instanceof Error ? error.message : 'meetingStartFailed';
            previous.meeting.stop();
            await endServer(previous);
            if (!previous.cancelled && current.current !== previous) throw error;
          }
          return;
        } finally {
          previous.enabling = false;
        }
      }
      if (webClient.getState() !== 'ready') throw new Error('disconnected');
      const startupId = crypto.randomUUID();
      const cancellation = new AbortController();
      const meeting: MeetingSession = new MeetingSession(webClient, id, context(id), {
        line: () => {}, // Task handlers already own the conversation timeline.
        error: (code, detail) => {
          if (current.current?.meeting === meeting) current.current.error = code;
          if (
            mounted.current &&
            current.current?.meeting === meeting &&
            !current.current.disabled &&
            !cancellation.signal.aborted
          )
            toast.open({
              content: [t(`meeting.errors.${code}`), detail].filter(Boolean).join(' '),
              variant: 'error',
            });
        },
        playback: (playback, member, voice) => {
          if (current.current?.meeting !== meeting || cancellation.signal.aborted) return;
          const speaker = context(id).members.find((item) => item.member_name === member)?.display_name || member || '';
          publishMeeting({ playback, speaker, voice: voice || '' });
        },
        addressedMembers: (names) => {
          if (mounted.current && current.current?.meeting === meeting && !cancellation.signal.aborted)
            publishMeeting({ addressedMembers: names });
        },
        ended: () => {
          const active = current.current;
          if (active?.meeting !== meeting) return;
          const unexpected = !cancellation.signal.aborted && !!active.error;
          void endServer(active).then(() => {
            if (unexpected && mounted.current && !current.current && visibleSession.current === id) {
              publishMeeting({
                state: 'failed',
                sessionId: id,
                error: active.error,
              });
            }
          });
        },
      });
      const active: ActiveMeeting = {
        sessionId: id,
        meeting,
        meetingId: null,
        startupId,
        cancellation,
      };
      current.current = active;
      publishMeeting({
        state: 'starting',
        error: undefined,
        sessionId: id,
        playback: 'idle',
        speaker: '',
        voice: '',
        addressedMembers: [],
      });
      try {
        const meetingId = await enableServerMeeting(active);
        active.meetingId = meetingId;
        await meeting.start();
        if (mounted.current && current.current === active && !cancellation.signal.aborted) {
          await meeting.activatePlayback(meetingId, startupId);
          if (current.current !== active || cancellation.signal.aborted) return;
          active.playbackReady = true;
          publishMeeting({ state: 'active' });
        }
      } catch (error) {
        active.error = error instanceof Error ? error.message : 'meetingStartFailed';
        if (error instanceof Error && /Another (connection|startup)/.test(error.message)) {
          cancellation.abort();
          if (current.current === active) current.current = null;
          meeting.finishCleanup();
        }
        meeting.stop();
        await endServer(active);
        if (!active.cancelled && current.current !== active) throw error;
      }
    },
    [context, enabled, endServer, t],
  );

  useEffect(
    () =>
      registerMeetingController({
        start,
        stop,
        stopAudio: () => current.current?.meeting.stopPlayback(),
      }),
    [start, stop],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (!current.current?.cancellation.signal.aborted) release();
    };
  }, [release]);

  useEffect(() => {
    if (
      current.current &&
      !current.current.cancellation.signal.aborted &&
      (!enabled || current.current.sessionId !== sessionId)
    )
      release();
  }, [enabled, sessionId, release]);
  useEffect(() => {
    publishMeeting({
      sessionId,
      teamExists: false,
      enabled: false,
      state: 'idle',
      error: undefined,
      meetingId: undefined,
      addressedMembers: [],
    });
    if (!enabled || !sessionId || sessionId === 'new') return;
    let cancelled = false;
    let attaching = false;
    let attachmentFailed = false;
    let revision = 0;
    const fail = (error: unknown) => {
      attachmentFailed = true;
      if (!cancelled)
        publishMeeting({
          state: 'failed',
          sessionId,
          error: error instanceof Error ? error.message : 'meetingStartFailed',
        });
    };
    const reflect = async (server: ServerMeeting) => {
      if (cancelled) return;
      publishMeeting({
        teamExists: server.team_exists,
        enabled: server.enabled,
        meetingId: server.meeting_id,
        addressedMembers: server.addressed_members ?? [],
      });
      const local = current.current?.sessionId === sessionId ? current.current : null;
      if (server.state === 'failed' || server.state === 'cleanup_failed') {
        publishMeeting({
          state: server.state,
          error: meetingErrorDetails(local?.error, server.activation_error, server.error),
        });
      } else if (server.state === 'starting' || server.state === 'closing') {
        if (server.state === 'closing' || !local?.ending) publishMeeting({ state: server.state });
      } else if (!server.enabled) {
        if (local?.ending) {
          await local.ending;
          if (cancelled || (current.current && current.current !== local)) return;
          // Failed release retains the aborted instance for an explicit retry.
          if (current.current === local && local.cancellation.signal.aborted) return;
        }
        if (local) {
          local.disabled = true;
        }
        if (!attachmentFailed) publishMeeting({ state: 'idle' });
      } else if (!attaching && !attachmentFailed && (!local || local.disabled)) {
        attaching = true;
        try {
          await start(sessionId);
        } catch (error) {
          fail(error);
        } finally {
          attaching = false;
        }
      } else if (local?.playbackReady && !local.ending && !local.enabling && !local.cancellation.signal.aborted) {
        attachmentFailed = false;
        publishMeeting({ state: 'active', error: undefined });
      }
    };
    const refresh = () => {
      const observed = revision;
      void webClient
        .request<ServerMeeting>('meeting.status', { session_id: sessionId })
        .then((server) => {
          if (observed === revision) return reflect(server);
        })
        .catch(fail);
    };
    const off = webClient.on('meeting.state', ({ payload }) => {
      if (payload.session_id === sessionId) {
        revision++;
        // Gateway may reduce this notification to session_id/content.
        if (
          typeof payload.team_exists !== 'boolean' ||
          typeof payload.enabled !== 'boolean' ||
          typeof payload.state !== 'string'
        ) {
          refresh();
          return;
        }
        void reflect(payload as unknown as ServerMeeting);
      }
    });
    const offState = webClient.onStateChange((state) => {
      if (state === 'ready') refresh();
    });
    if (webClient.getState() === 'ready') refresh();
    return () => {
      cancelled = true;
      off();
      offState();
    };
  }, [enabled, sessionId, start]);
  useEffect(() => {
    if (current.current) current.current.meeting.context = context(current.current.sessionId);
  }, [roster, context]);
  return null;
}
