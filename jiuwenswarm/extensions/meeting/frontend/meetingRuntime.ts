import { claimMeetingAudio } from '../../../channels/web/frontend/src/features/teamMeeting/playbackOwnership';
import { registerMeetingInput } from '../../../channels/web/frontend/src/features/teamMeeting/input';
import { onTtsStop, requestTtsStop } from '../../../channels/web/frontend/src/utils/ttsControl';

export type MeetingMember = { member_name: string; display_name: string };
export type MeetingContext = { leader: string; members: MeetingMember[] };
type Payload = Record<string, unknown>;
export interface Transport {
  request<T = unknown>(method: string, params?: Payload, options?: { timeoutMs?: number }): Promise<T>;
  on(event: string, handler: (event: { payload: Payload }) => void): () => void;
  onStateChange(handler: (state: string) => void): () => void;
}
export type MeetingLine = { id: string; speaker: string; text: string };

export type PlaybackState = 'idle' | 'loading' | 'playing' | 'paused' | 'failed';
type Speech = Payload & {
  session_id: string;
  meeting_id: string;
  request_id: string;
  lease_token: string;
  source_message_id: string | null;
  speaker_id: string;
  playback_id: string;
  chunks: string[];
  chunk_count: number;
  voice: string;
};
type Attempt = { speech: Speech; completed: number; reported: boolean };
const identityFields = [
  'session_id',
  'meeting_id',
  'request_id',
  'lease_token',
  'source_message_id',
  'speaker_id',
  'playback_id',
] as const;
function identity(speech: Speech): Payload {
  return Object.fromEntries(identityFields.map((key) => [key, speech[key]]));
}

function hasSpeechIdentity(payload: Payload): boolean {
  return identityFields.every(
    (key) =>
      (key === 'source_message_id' && payload[key] === null) || (typeof payload[key] === 'string' && !!payload[key]),
  );
}

export class MeetingSession {
  private active = false;
  private disposed = false;
  private off: Array<() => void> = [];
  private playbackEpoch = 0;
  private finishAudio: (() => void) | null = null;
  private speechTail = Promise.resolve();
  private seen = new Set<string>();
  private interrupted = new Set<string>();
  private meetingId: string | null = null;
  private currentSpeech: Attempt | null = null;
  private paused = false;
  private claimingAudio = false;
  private offStop: (() => void) | null = null;
  private releaseAudio: () => void;
  private offAudio: (() => void) | null = null;
  private offInput: (() => void) | null = null;

  constructor(
    private transport: Transport,
    private sessionId: string,
    public context: MeetingContext,
    private callbacks: {
      line: (line: MeetingLine) => void;
      error: (code: string, detail?: string) => void;
      ended: () => void;
      playback?: (state: PlaybackState, speaker?: string, voice?: string) => void;
      addressedMembers?: (names: string[]) => void;
    },
  ) {
    this.releaseAudio = claimMeetingAudio(sessionId);
    requestTtsStop();
  }

  async start(): Promise<void> {
    if (this.disposed) throw new Error('disconnected');
    this.active = true;
    this.off.push(
      this.transport.onStateChange((state) => {
        if (state !== 'ready') this.fail('disconnected');
      }),
    );
    this.off.push(this.transport.on('meeting.speech.ready', ({ payload }) => this.receiveSpeech(payload)));
    this.off.push(
      this.transport.on('meeting.speech.stop', ({ payload }) => {
        if (
          !this.active ||
          !this.matchesMeeting(payload) ||
          typeof payload.stop_id !== 'string' ||
          !payload.stop_id ||
          !hasSpeechIdentity(payload)
        )
          return;
        const attempt = this.currentSpeech;
        if (
          attempt &&
          attempt.speech.playback_id === payload.playback_id &&
          identityFields.some((key) => attempt.speech[key] !== payload[key])
        )
          return;
        this.interrupted.add(payload.playback_id as string);
        if (attempt && attempt.speech.playback_id === payload.playback_id) {
          // This is a speech interruption, not End: keep capture and listeners live.
          attempt.reported = true;
          this.playbackEpoch++;
          this.finishAudio?.();
          this.currentSpeech = null;
          this.callbacks.playback?.('idle');
        }
        void this.transport
          .request(
            'meeting.playback.stopped',
            {
              session_id: this.sessionId,
              meeting_id: this.meetingId,
              stop_id: payload.stop_id,
            },
            { timeoutMs: 20000 },
          )
          .catch(() => this.callbacks.error('playbackFailed'));
      }),
    );
    this.off.push(
      this.transport.on('meeting.addressed_members', ({ payload }) => {
        if (!this.active || !this.matchesMeeting(payload) || !Array.isArray(payload.addressed_members)) return;
        const names = payload.addressed_members;
        if (!names.every((name) => typeof name === 'string' && name.trim())) return;
        this.callbacks.addressedMembers?.(names);
      }),
    );
    this.off.push(
      this.transport.on('meeting.speech.error', ({ payload }) => {
        if (!this.matchesMeeting(payload) || !this.active) return;
        // A held lease has no automatic resume in this version.
        if (this.paused) return;
        this.stopPlayback(false);
        this.callbacks.playback?.('failed');
      }),
    );
    // This listener intentionally survives local teardown until End is confirmed.
    this.offStop = this.transport.on('meeting.playback.stop', ({ payload }) => {
      if (!this.matchesMeeting(payload) || typeof payload.stop_id !== 'string') return;
      this.stopPlayback(false);
      if (this.active) this.stop();
      void this.transport
        .request(
          'meeting.playback.stopped',
          {
            session_id: this.sessionId,
            meeting_id: this.meetingId,
            stop_id: payload.stop_id,
          },
          { timeoutMs: 20000 },
        )
        .catch(() => this.callbacks.error('cleanupFailed'));
    });
    this.offAudio = onTtsStop(() => {
      if (!this.claimingAudio && this.currentSpeech) this.stopPlayback();
    });
  }

  resume(): void {
    if (!this.active || this.disposed) throw new Error('disconnected');
    this.paused = false;
  }

  async activatePlayback(meetingId: string, startupId: string): Promise<void> {
    if (!this.active || this.disposed) throw new Error('disconnected');
    this.meetingId = meetingId;
    const result = await this.transport.request<{ ready?: boolean }>(
      'meeting.playback.ready',
      { session_id: this.sessionId, meeting_id: meetingId, startup_id: startupId },
      { timeoutMs: 20000 },
    );
    if (!this.active || this.disposed) return;
    if (!result.ready) throw new Error('playbackUnavailable');
    this.offInput = registerMeetingInput(this.sessionId, async (text) => {
      if (!this.active || this.disposed || this.paused || !text.trim()) return;
      const prepared = await this.transport.request<{ interruption_id?: string }>('meeting.input.prepare', {
        session_id: this.sessionId,
        meeting_id: meetingId,
        transcript: text,
      });
      if (!prepared.interruption_id) return;
      return {
        admitted: async () => {
          const result = await this.transport.request<{ accepted?: boolean }>('meeting.input.admitted', {
            session_id: this.sessionId,
            meeting_id: meetingId,
            interruption_id: prepared.interruption_id,
          });
          if (!result.accepted) throw new Error('deliveryFailed');
        },
      };
    });
  }

  private matchesMeeting(payload: Payload): boolean {
    return !!this.meetingId && payload.session_id === this.sessionId && payload.meeting_id === this.meetingId;
  }

  private receiveSpeech(payload: Payload): void {
    if (!this.active || !this.matchesMeeting(payload)) return;
    if (!hasSpeechIdentity(payload)) return;
    if (
      !Array.isArray(payload.chunks) ||
      !payload.chunks.length ||
      payload.chunks.length > 64 ||
      payload.chunk_count !== payload.chunks.length ||
      payload.chunks.some((text) => typeof text !== 'string' || !text)
    )
      return;
    const speech = payload as Speech;
    if (this.seen.has(speech.playback_id) || this.interrupted.has(speech.playback_id)) return;
    this.seen.add(speech.playback_id);
    const attempt: Attempt = { speech, completed: 0, reported: false };
    const epoch = this.playbackEpoch;
    this.speechTail = this.speechTail
      .then(async () => {
        if (this.interrupted.has(speech.playback_id)) return;
        if (!this.active || this.paused || epoch !== this.playbackEpoch) {
          await this.report(attempt, 'stopped');
          return;
        }
        this.currentSpeech = attempt;
        this.callbacks.line({
          id: speech.source_message_id ?? speech.request_id,
          speaker: speech.speaker_id,
          text: speech.chunks.join(''),
        });
        try {
          for (let index = 0; index < speech.chunk_count; index++) {
            if (!this.active || this.paused || epoch !== this.playbackEpoch) return;
            this.callbacks.playback?.('loading', speech.speaker_id, speech.voice);
            const result = await this.transport.request<{
              success: boolean;
              playback_id: string;
              chunk_index: number;
              audio_base64?: string;
              audio_mime?: string;
            }>('meeting.speech.synthesize', { ...identity(speech), chunk_index: index }, { timeoutMs: 90000 });
            if (!this.active || this.paused || epoch !== this.playbackEpoch) return;
            if (
              !result.success ||
              !result.audio_base64 ||
              result.playback_id !== speech.playback_id ||
              result.chunk_index !== index
            )
              throw new Error('No matching synthesized audio');
            this.callbacks.playback?.('playing', speech.speaker_id, speech.voice);
            const outcome = await this.play(result.audio_base64, result.audio_mime || 'audio/wav');
            if (outcome !== 'completed' || !this.active || epoch !== this.playbackEpoch) return;
            attempt.completed++;
          }
          // Clear before acknowledging: the next ready event can arrive before the RPC response.
          if (this.currentSpeech === attempt) this.currentSpeech = null;
          await this.report(attempt, 'completed');
          if (!this.paused && this.active) this.callbacks.playback?.('idle');
        } catch (error) {
          if (epoch !== this.playbackEpoch || !this.active) return;
          // A failed completion acknowledgement is a coordination error, not
          // failed audio. Do not claim it was skipped or send a conflicting receipt.
          if (attempt.reported) throw error;
          this.callbacks.playback?.('failed', speech.speaker_id, speech.voice);
          const detail =
            error && typeof error === 'object' && 'message' in error && typeof error.message === 'string'
              ? error.message.slice(0, 500)
              : undefined;
          this.callbacks.error('playbackSkipped', detail);
          // The next ready event may arrive before the result RPC response.
          if (this.currentSpeech === attempt) this.currentSpeech = null;
          await this.report(attempt, 'failed');
          if (!this.paused && this.active) this.callbacks.playback?.('idle');
        } finally {
          if (this.currentSpeech === attempt) this.currentSpeech = null;
        }
      })
      .catch(() => {
        this.paused = true;
        this.callbacks.playback?.('failed');
        this.callbacks.error('playbackFailed');
      });
  }

  private async report(attempt: Attempt, outcome: 'completed' | 'stopped' | 'failed'): Promise<void> {
    if (attempt.reported) return;
    attempt.reported = true;
    const params = {
      ...identity(attempt.speech),
      outcome,
      completed_chunks: attempt.completed,
    };
    // Identical receipts are idempotent even if the next speaker has been granted.
    try {
      await this.transport.request('meeting.playback.result', params, {
        timeoutMs: 20000,
      });
    } catch {
      await this.transport.request('meeting.playback.result', params, {
        timeoutMs: 20000,
      });
    }
  }

  private claimAudio(): void {
    this.claimingAudio = true;
    try {
      requestTtsStop();
    } finally {
      this.claimingAudio = false;
    }
  }

  private play(encoded: string, mime: string): Promise<'completed' | 'stopped'> {
    this.claimAudio();
    const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(url);
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = (outcome: 'completed' | 'stopped', error?: Error) => {
        if (finished) return;
        finished = true;
        audio.pause();
        audio.onended = null;
        audio.onerror = null;
        URL.revokeObjectURL(url);
        if (this.finishAudio === cancel) this.finishAudio = null;
        if (error) reject(error);
        else resolve(outcome);
      };
      const cancel = () => finish('stopped');
      this.finishAudio = cancel;
      audio.onended = () => finish('completed');
      audio.onerror = () => finish('stopped', new Error('Audio playback failed'));
      try {
        void audio.play().catch((error) => finish('stopped', error));
      } catch (error) {
        finish('stopped', error instanceof Error ? error : new Error('Audio playback failed'));
      }
    });
  }

  stopPlayback(notify = true): void {
    this.playbackEpoch++;
    this.finishAudio?.();
    if (!this.meetingId) return;
    this.paused = true;
    this.callbacks.playback?.('paused');
    const attempt = this.currentSpeech;
    this.currentSpeech = null;
    if (notify && attempt) void this.report(attempt, 'stopped').catch(() => this.callbacks.error('playbackFailed'));
  }

  finishCleanup(): void {
    this.offInput?.();
    this.offInput = null;
    this.offStop?.();
    this.offStop = null;
    this.offAudio?.();
    this.offAudio = null;
    this.releaseAudio();
  }

  private fail(code: string): void {
    if (!this.active) return;
    this.callbacks.error(code);
    this.stop();
  }

  stop(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.active = false;
    this.offInput?.();
    this.offInput = null;
    this.stopPlayback();
    this.off.splice(0).forEach((off) => off());
    this.callbacks.ended();
  }
}
