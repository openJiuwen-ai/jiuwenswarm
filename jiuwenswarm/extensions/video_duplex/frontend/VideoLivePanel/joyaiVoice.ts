const TARGET_RATE = 16_000;
const LISTENING_SPEECH_MS = 220;
const END_OF_TURN_SILENCE_MS = 600;
const CANDIDATE_SILENCE_MS = 240;
const PRE_ROLL_MS = 300;
const MAX_TURN_MS = 20_000;
const PCM_STREAM_START_BUFFER_MS = 240;

export interface JoyAIVoiceCallbacks {
  onSpeechStart: () => void;
  onTurnAudio: (audioDataUrl: string, turnId: string) => void;
  onState: (state: 'connecting' | 'listening' | 'speaking' | 'closed') => void;
  onError: (message: string) => void;
}

function readableError(value: unknown): string {
  return value instanceof Error ? value.message : String(value || '未知错误');
}

export function canPlayJoyAIResponse(
  responseGeneration: number,
  currentGeneration: number,
  userSpeechActive: boolean,
): boolean {
  return !userSpeechActive && responseGeneration === currentGeneration;
}

export class JoyAITtsInterruptionState {
  private pending: { text: string; speechEpoch: number } | null = null;

  capture(text: string, speechEpoch: number): void {
    const candidate = text.trim();
    this.pending = candidate ? { text: candidate, speechEpoch } : null;
  }

  discard(speechEpoch?: number): void {
    if (speechEpoch !== undefined && this.pending?.speechEpoch !== speechEpoch) return;
    this.pending = null;
  }

  takeAfterRejectedTurn(speechEpoch: number, transcript?: string): string {
    if (this.pending?.speechEpoch !== speechEpoch) return '';
    const pending = this.pending;
    this.pending = null;
    return transcript?.trim() ? '' : pending.text;
  }
}

export function resamplePcm16(
  input: Int16Array,
  sourceRate: number,
  targetRate = TARGET_RATE,
): Int16Array {
  if (sourceRate === targetRate || input.length === 0) return input.slice();
  const length = Math.max(1, Math.round(input.length * targetRate / sourceRate));
  const output = new Int16Array(length);
  const scale = sourceRate / targetRate;
  for (let index = 0; index < length; index += 1) {
    const position = index * scale;
    const left = Math.min(Math.floor(position), input.length - 1);
    const right = Math.min(left + 1, input.length - 1);
    const fraction = position - left;
    output[index] = Math.round(input[left] + (input[right] - input[left]) * fraction);
  }
  return output;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const blockSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += blockSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + blockSize));
  }
  return btoa(binary);
}

export function pcm16ToWavDataUrl(pcm: Int16Array, sampleRate = TARGET_RATE): string {
  const buffer = new ArrayBuffer(44 + pcm.byteLength);
  const view = new DataView(buffer);
  const writeText = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  writeText(0, 'RIFF');
  view.setUint32(4, 36 + pcm.byteLength, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, 'data');
  view.setUint32(40, pcm.byteLength, true);
  new Int16Array(buffer, 44).set(pcm);
  return `data:audio/wav;base64,${bytesToBase64(new Uint8Array(buffer))}`;
}

export class JoyAIVoiceSession {
  private microphone: MediaStream | null = null;
  private captureContext: AudioContext | null = null;
  private captureNode: AudioWorkletNode | null = null;
  private playback: HTMLAudioElement | null = null;
  private playbackFinished: (() => void) | null = null;
  private pcmPlaybackContext: AudioContext | null = null;
  private pcmStreamId: string | null = null;
  private pcmStreamSources = new Set<AudioBufferSourceNode>();
  private pcmStreamPending: Array<{ pcm: Int16Array; sampleRate: number }> = [];
  private pcmStreamBufferedMs = 0;
  private pcmStreamNextTime = 0;
  private pcmStreamStarted = false;
  private pcmStreamDone = false;
  private pcmStreamRemainder: number | null = null;
  private pcmStreamResolve: (() => void) | null = null;
  private pcmStreamReject: ((error: Error) => void) | null = null;
  private stopped = false;
  private noiseFloor = 220;
  private candidateSpeechMs = 0;
  private silenceMs = 0;
  private speechActive = false;
  private preRoll: Int16Array[] = [];
  private preRollSamples = 0;
  private utterance: Int16Array[] = [];
  private utteranceSamples = 0;

  constructor(private readonly callbacks: JoyAIVoiceCallbacks) {}

  async start(): Promise<void> {
    this.callbacks.onState('connecting');
    this.microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (this.stopped) {
      this.microphone.getTracks().forEach((track) => track.stop());
      return;
    }
    this.captureContext = new AudioContext({ sampleRate: TARGET_RATE });
    await this.captureContext.audioWorklet.addModule(
      new URL('./duplex-capture.js', import.meta.url),
    );
    const source = this.captureContext.createMediaStreamSource(this.microphone);
    this.captureNode = new AudioWorkletNode(this.captureContext, 'jiuwen-duplex-capture');
    this.captureNode.port.onmessage = ({ data }) => {
      const input = new Int16Array(data);
      const pcm = resamplePcm16(
        input,
        this.captureContext?.sampleRate || TARGET_RATE,
        TARGET_RATE,
      );
      this.processAudio(pcm);
    };
    const silent = this.captureContext.createGain();
    silent.gain.value = 0;
    source.connect(this.captureNode);
    this.captureNode.connect(silent).connect(this.captureContext.destination);
    await this.captureContext.resume();
    this.callbacks.onState('listening');
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.interruptPlayback();
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.microphone = null;
    this.captureNode?.disconnect();
    this.captureNode = null;
    void this.captureContext?.close();
    this.captureContext = null;
    void this.pcmPlaybackContext?.close();
    this.pcmPlaybackContext = null;
    this.resetTurn();
    this.callbacks.onState('closed');
  }

  async speak(audioDataUrl: string): Promise<void> {
    if (this.stopped) return;
    this.interruptPlayback();
    const audio = new Audio(audioDataUrl);
    this.playback = audio;
    await new Promise<void>((resolve) => {
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        if (this.playback === audio) this.playback = null;
        if (this.playbackFinished === finish) this.playbackFinished = null;
        if (!this.stopped) this.callbacks.onState('listening');
        resolve();
      };
      this.playbackFinished = finish;
      audio.onended = finish;
      audio.onerror = () => {
        this.callbacks.onError('JoyAI 语音播放失败');
        finish();
      };
      this.callbacks.onState('speaking');
      void audio.play().catch((error) => {
        this.callbacks.onError(`JoyAI 语音播放失败：${readableError(error)}`);
        finish();
      });
    });
  }

  beginPcmStream(streamId: string): Promise<void> {
    this.interruptPlayback();
    this.pcmStreamId = streamId;
    this.pcmStreamDone = false;
    return new Promise<void>((resolve, reject) => {
      this.pcmStreamResolve = resolve;
      this.pcmStreamReject = reject;
    });
  }

  appendPcm16Chunk(streamId: string, audioBase64: string, sampleRate = 24_000): boolean {
    if (this.stopped || this.pcmStreamId !== streamId || !audioBase64) return false;
    const binary = atob(audioBase64);
    const bytes = new Uint8Array(binary.length + (this.pcmStreamRemainder === null ? 0 : 1));
    let byteOffset = 0;
    if (this.pcmStreamRemainder !== null) {
      bytes[0] = this.pcmStreamRemainder;
      byteOffset = 1;
      this.pcmStreamRemainder = null;
    }
    for (let index = 0; index < binary.length; index += 1) {
      bytes[byteOffset + index] = binary.charCodeAt(index);
    }
    if (bytes.length % 2 !== 0) {
      this.pcmStreamRemainder = bytes.at(-1) ?? null;
    }
    const sampleCount = Math.floor(bytes.length / 2);
    if (sampleCount === 0) return true;
    const view = new DataView(bytes.buffer, bytes.byteOffset, sampleCount * 2);
    const pcm = new Int16Array(sampleCount);
    for (let index = 0; index < sampleCount; index += 1) {
      pcm[index] = view.getInt16(index * 2, true);
    }
    this.pcmStreamPending.push({ pcm, sampleRate });
    this.pcmStreamBufferedMs += pcm.length * 1_000 / sampleRate;
    if (
      this.pcmStreamStarted
      || this.pcmStreamBufferedMs >= PCM_STREAM_START_BUFFER_MS
    ) {
      this.schedulePendingPcm();
    }
    return true;
  }

  finishPcmStream(streamId: string): void {
    if (this.pcmStreamId !== streamId) return;
    this.pcmStreamDone = true;
    this.schedulePendingPcm();
    this.settlePcmStreamIfDrained();
  }

  failPcmStream(streamId: string, message: string): void {
    if (this.pcmStreamId !== streamId) return;
    const reject = this.pcmStreamReject;
    this.clearPcmStream();
    reject?.(new Error(message || 'JoyAI 语音流失败'));
    if (!this.stopped) this.callbacks.onState('listening');
  }

  interruptPlayback(): void {
    const audio = this.playback;
    this.playback = null;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    const finish = this.playbackFinished;
    this.playbackFinished = null;
    if (finish) finish();
    const resolve = this.pcmStreamResolve;
    const hadPcmStream = this.pcmStreamId !== null;
    this.clearPcmStream();
    resolve?.();
    if (hadPcmStream && !this.stopped) this.callbacks.onState('listening');
  }

  private schedulePendingPcm(): void {
    if (!this.pcmStreamId || this.pcmStreamPending.length === 0) return;
    if (!this.pcmPlaybackContext) this.pcmPlaybackContext = new AudioContext();
    const context = this.pcmPlaybackContext;
    void context.resume().catch((error) => {
      const streamId = this.pcmStreamId;
      if (streamId) this.failPcmStream(streamId, readableError(error));
    });
    if (!this.pcmStreamStarted) {
      this.pcmStreamStarted = true;
      this.pcmStreamNextTime = context.currentTime + 0.03;
      this.callbacks.onState('speaking');
    }
    while (this.pcmStreamPending.length > 0) {
      const chunk = this.pcmStreamPending.shift();
      if (!chunk) break;
      const buffer = context.createBuffer(1, chunk.pcm.length, chunk.sampleRate);
      const channel = buffer.getChannelData(0);
      for (let index = 0; index < chunk.pcm.length; index += 1) {
        channel[index] = chunk.pcm[index] / 32768;
      }
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      const startAt = Math.max(this.pcmStreamNextTime, context.currentTime + 0.01);
      this.pcmStreamNextTime = startAt + buffer.duration;
      this.pcmStreamSources.add(source);
      source.onended = () => {
        this.pcmStreamSources.delete(source);
        this.settlePcmStreamIfDrained();
      };
      source.start(startAt);
    }
    this.pcmStreamBufferedMs = 0;
  }

  private settlePcmStreamIfDrained(): void {
    if (
      !this.pcmStreamDone
      || this.pcmStreamPending.length > 0
      || this.pcmStreamSources.size > 0
    ) return;
    const resolve = this.pcmStreamResolve;
    this.clearPcmStream();
    resolve?.();
    if (!this.stopped) this.callbacks.onState('listening');
  }

  private clearPcmStream(): void {
    const sources = [...this.pcmStreamSources];
    this.pcmStreamId = null;
    this.pcmStreamSources.clear();
    this.pcmStreamPending = [];
    this.pcmStreamBufferedMs = 0;
    this.pcmStreamNextTime = 0;
    this.pcmStreamStarted = false;
    this.pcmStreamDone = false;
    this.pcmStreamRemainder = null;
    this.pcmStreamResolve = null;
    this.pcmStreamReject = null;
    sources.forEach((source) => {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // The source may already have ended.
      }
      source.disconnect();
    });
  }

  private processAudio(pcm: Int16Array): void {
    if (this.stopped || pcm.length === 0) return;
    const frameMs = pcm.length * 1_000 / TARGET_RATE;
    const level = this.rms(pcm);
    const playbackActive = this.playback !== null || this.pcmStreamStarted;
    const threshold = playbackActive
      ? Math.max(1_500, this.noiseFloor * 5)
      : Math.max(650, this.noiseFloor * 3);
    const voice = level > threshold;

    if (!this.speechActive) this.pushPreRoll(pcm);
    if (voice) {
      this.candidateSpeechMs += frameMs;
      this.silenceMs = 0;
      if (!this.speechActive && this.candidateSpeechMs >= LISTENING_SPEECH_MS) {
        this.speechActive = true;
        this.utterance = this.preRoll.map((chunk) => chunk.slice());
        this.utteranceSamples = this.preRollSamples;
        this.callbacks.onSpeechStart();
      } else if (this.speechActive) {
        this.pushUtterance(pcm);
      }
    } else {
      this.silenceMs += frameMs;
      if (this.speechActive) {
        this.pushUtterance(pcm);
      } else if (this.silenceMs >= CANDIDATE_SILENCE_MS) {
        this.candidateSpeechMs = 0;
      }
      this.observeNoise(level);
    }

    if (this.speechActive && (
      this.silenceMs >= END_OF_TURN_SILENCE_MS
      || this.utteranceSamples >= TARGET_RATE * MAX_TURN_MS / 1_000
    )) {
      this.finishTurn();
    }
  }

  private pushPreRoll(pcm: Int16Array): void {
    this.preRoll.push(pcm.slice());
    this.preRollSamples += pcm.length;
    const limit = TARGET_RATE * PRE_ROLL_MS / 1_000;
    while (this.preRollSamples > limit && this.preRoll.length > 1) {
      this.preRollSamples -= this.preRoll.shift()?.length || 0;
    }
  }

  private pushUtterance(pcm: Int16Array): void {
    this.utterance.push(pcm.slice());
    this.utteranceSamples += pcm.length;
  }

  private finishTurn(): void {
    const pcm = new Int16Array(this.utteranceSamples);
    let offset = 0;
    this.utterance.forEach((chunk) => {
      pcm.set(chunk, offset);
      offset += chunk.length;
    });
    const turnId = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `joyai-turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    this.resetTurn();
    if (pcm.length > 0) this.callbacks.onTurnAudio(pcm16ToWavDataUrl(pcm), turnId);
  }

  private resetTurn(): void {
    this.candidateSpeechMs = 0;
    this.silenceMs = 0;
    this.speechActive = false;
    this.preRoll = [];
    this.preRollSamples = 0;
    this.utterance = [];
    this.utteranceSamples = 0;
  }

  private rms(pcm: Int16Array): number {
    let energy = 0;
    for (let index = 0; index < pcm.length; index += 1) {
      energy += pcm[index] * pcm[index];
    }
    return Math.sqrt(energy / pcm.length);
  }

  private observeNoise(level: number): void {
    if (this.speechActive || this.playback || this.pcmStreamStarted) return;
    this.noiseFloor = this.noiseFloor * 0.97 + Math.min(level, 1_200) * 0.03;
  }
}
