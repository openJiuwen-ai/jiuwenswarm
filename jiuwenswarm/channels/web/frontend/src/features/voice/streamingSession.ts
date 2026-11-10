import { BrowserPcmCapture, type PcmCapture } from './pcmCapture';

type Payload = Record<string, unknown>;
export interface VoiceTransport {
  request<T = unknown>(method: string, params?: Payload, options?: { timeoutMs?: number }): Promise<T>;
  on(event: string, handler: (event: { payload: Payload }) => void): () => void;
  onStateChange(handler: (state: string) => void): () => void;
}
export type VoiceStreamingState = 'idle' | 'starting' | 'listening' | 'paused' | 'failed';

export class VoiceStreamingSession {
  private media: MediaStream | null = null;
  private capture: PcmCapture | null = null;
  private streamId: string | null = null;
  private readonly startupId = crypto.randomUUID();
  private disposed = false;
  private listening = false;
  private resuming = false;
  private requested = false;
  private sequence = 0;
  private pendingSamples = 0;
  private lastUtterance = 0;
  private pendingDeliveries = 0;
  private inputTail = Promise.resolve();
  private deliveryTail = Promise.resolve();
  private off: Array<() => void> = [];

  constructor(
    private transport: VoiceTransport,
    readonly sessionId: string,
    private callbacks: {
      state: (state: VoiceStreamingState) => void;
      transcript: (text: string) => Promise<void>;
      error: (code: string) => void;
      ended: () => void;
    },
  ) {}

  async start(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('unsupported');
    const media = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (this.disposed) {
      media.getTracks().forEach((track) => track.stop());
      return;
    }
    this.media = media;
    media.getAudioTracks().forEach((track) => {
      track.onended = () => this.fail('microphoneEnded');
    });
    this.off.push(
      this.transport.onStateChange((state) => {
        if (state !== 'ready') this.fail('disconnected');
      }),
    );
    this.off.push(
      this.transport.on('voice.input.transcript', ({ payload }) => {
        if (!this.accept(payload)) return;
        const text = typeof payload.transcript === 'string' ? payload.transcript.trim() : '';
        if (!text) return;
        if (this.pendingDeliveries >= 7) return this.fail('deliveryBacklog');
        this.pendingDeliveries++;
        this.deliveryTail = this.deliveryTail
          .then(async () => {
            if (!this.disposed) await this.callbacks.transcript(text);
          })
          .catch(() => {
            if (!this.disposed) this.callbacks.error('deliveryFailed');
          })
          .finally(() => {
            this.pendingDeliveries--;
          });
      }),
    );
    this.off.push(
      this.transport.on('voice.input.error', ({ payload }) => {
        if (this.accept(payload)) this.callbacks.error('transcriptionFailed');
      }),
    );
    this.requested = true;
    const result = await this.transport.request<{ ready?: boolean; stream_id?: string }>(
      'voice.input.start',
      {
        ...this.identity(),
      },
      { timeoutMs: 60000 },
    );
    if (this.disposed) return;
    if (!result.ready || !result.stream_id) throw new Error('inputStreamFailed');
    this.streamId = result.stream_id;
    this.capture = new BrowserPcmCapture(
      media,
      (pcm) => this.sendPcm(pcm),
      () => this.fail('recordingFailed'),
    );
    this.listening = true;
    await this.capture.start();
    if (!this.disposed) this.callbacks.state('listening');
  }

  private identity(): Payload {
    return { session_id: this.sessionId, startup_id: this.startupId, stream_id: this.streamId };
  }

  private accept(payload: Payload): boolean {
    if (this.disposed || !this.streamId || payload.session_id !== this.sessionId || payload.stream_id !== this.streamId)
      return false;
    const id = payload.utterance_id;
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= this.lastUtterance) return false;
    this.lastUtterance = id;
    return true;
  }

  private sendPcm(pcm: Int16Array): void {
    if (this.disposed || !this.listening || !this.streamId || !pcm.length) return;
    if (pcm.length > 3200 || this.pendingSamples + pcm.length > 32000) return this.fail('inputStreamFailed');
    this.pendingSamples += pcm.length;
    const bytes = new Uint8Array(pcm.length * 2),
      view = new DataView(bytes.buffer);
    pcm.forEach((sample, index) => view.setInt16(index * 2, sample, true));
    const sequence = this.sequence++;
    const request = this.transport
      .request<{ accepted?: boolean }>(
        'voice.input.append',
        {
          ...this.identity(),
          sequence,
          audio_base64: btoa(String.fromCharCode(...bytes)),
        },
        { timeoutMs: 10000 },
      )
      .then((result) => {
        if (!result.accepted) throw new Error('inputStreamFailed');
      })
      .finally(() => {
        this.pendingSamples -= pcm.length;
      });
    this.inputTail = Promise.all([this.inputTail, request])
      .then(() => {})
      .catch(() => {
        if (!this.disposed) this.fail('inputStreamFailed');
      });
  }

  setListening(listening: boolean): void {
    if (this.disposed || !this.streamId || this.resuming || this.listening === listening) return;
    this.listening = false;
    this.resuming = true;
    this.inputTail = this.inputTail
      .then(async () => {
        if (this.disposed) return;
        const result = await this.transport.request<{ accepted?: boolean }>(
          `voice.input.${listening ? 'resume' : 'pause'}`,
          this.identity(),
        );
        if (!result.accepted) throw new Error('inputStreamFailed');
        if (this.disposed) return;
        this.listening = listening;
        this.callbacks.state(listening ? 'listening' : 'paused');
      })
      .catch(() => {
        if (!this.disposed) this.fail('inputStreamFailed');
      })
      .finally(() => {
        this.resuming = false;
      });
  }

  private fail(code: string): void {
    this.callbacks.error(code);
    this.stop();
  }

  stop(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.listening = false;
    this.capture?.stop();
    this.off.splice(0).forEach((off) => off());
    this.media?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    if (this.requested) void this.transport.request('voice.input.stop', this.identity()).catch(() => {});
    this.callbacks.ended();
  }
}
