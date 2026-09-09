import {
  createQwenOmniCancelResponseEvent,
  createQwenOmniFinishSessionEvent,
  createQwenOmniSessionUpdate,
  createQwenOmniTextTurnEvents,
  createQwenOmniToolResultEvents,
  parseQwenOmniFunctionCall,
  QwenOmniFunctionCall,
  QwenOmniMediaSequencer,
} from './qwenOmniProtocol.js';
import { getWsBase } from '../../../../channels/web/frontend/src/utils/env.js';

export interface RealtimeDuplexConfig {
  url: string;
  voice?: string;
  tools?: Array<Record<string, unknown>>;
}

export interface RealtimeToolResult {
  jobId: string;
  question: string;
  result: string;
  callId?: string;
}

export interface RealtimeDuplexCallbacks {
  getVideoFrame: () => string | null;
  onAssistantText: (text: string, final: boolean, toolJobId?: string) => void;
  onUserText: (text: string, final: boolean) => void;
  onState: (state: 'connecting' | 'listening' | 'speaking' | 'closed') => void;
  onError: (message: string) => void;
  onDiagnostic?: (event: Record<string, unknown>) => void;
  onToolResultDispatched?: (jobId: string) => void;
  onFunctionCall?: (call: QwenOmniFunctionCall) => void;
}

function resolveRealtimeUrl(configuredUrl: string): string {
  if (/^wss?:\/\//i.test(configuredUrl)) return configuredUrl;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const browserBase = getWsBase() || `${protocol}//${window.location.host}`;
  const base = new URL(browserBase);
  return new URL(configuredUrl, `${base.protocol}//${base.host}`).toString();
}

export function createRealtimeDuplexSession(
  config: RealtimeDuplexConfig,
  callbacks: RealtimeDuplexCallbacks,
  onUnsupportedBrowser?: () => void,
): RealtimeDuplexSession {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    onUnsupportedBrowser?.();
    throw new Error('当前浏览器不支持 Full-duplex 音频。');
  }
  if (!config.url) throw new Error('请配置 Full-duplex WebSocket 地址');
  return new RealtimeDuplexSession({ ...config, url: resolveRealtimeUrl(config.url) }, callbacks);
}

const INPUT_RATE = 16_000;
const OUTPUT_RATE = 24_000;
const SEND_INTERVAL_MS = 200;
const USER_TURN_SILENCE_MS = 1_200;
const REALTIME_CLIENT_BUILD = 'qwen-media-bargein-v3';
const LISTENING_SPEECH_MS = 240;
const INITIAL_PLAYBACK_BUFFER_MS = 400;

function readableError(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const error = value as Record<string, unknown>;
    const message = error.message || error.detail || error.code || error.error;
    if (message) return readableError(message);
    try { return JSON.stringify(value); } catch { return 'Realtime 服务错误'; }
  }
  return String(value || 'Realtime 服务错误');
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToBytes(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function resample(input: Int16Array, sourceRate: number, targetRate: number): Int16Array {
  if (sourceRate === targetRate) return input;
  const ratio = sourceRate / targetRate;
  const output = new Int16Array(Math.floor(input.length / ratio));
  for (let index = 0; index < output.length; index += 1) {
    const start = Math.floor(index * ratio);
    const end = Math.max(start + 1, Math.min(input.length, Math.floor((index + 1) * ratio)));
    let sum = 0;
    for (let source = start; source < end; source += 1) sum += input[source];
    output[index] = sum / (end - start);
  }
  return output;
}

export class RealtimeDuplexSession {
  private socket: WebSocket | null = null;
  private microphone: MediaStream | null = null;
  private captureContext: AudioContext | null = null;
  private playbackContext: AudioContext | null = null;
  private captureNode: AudioWorkletNode | null = null;
  private playbackNode: AudioWorkletNode | null = null;
  private pending: Int16Array[] = [];
  private pendingSamples = 0;
  private sendTimer: number | null = null;
  private sessionReady = false;
  private responseId: string | null = null;
  private pendingToolResults: RealtimeToolResult[] = [];
  private acceptedToolResultIds = new Set<string>();
  private acceptedFunctionCallIds = new Set<string>();
  private activeToolJobId: string | null = null;
  private assistantPlaying = false;
  private responseActive = false;
  private playbackOperation: Promise<void> = Promise.resolve();
  private playbackGeneration = 0;
  private queuedDrainResponseId: string | null = null;
  private noiseFloor = 120;
  private userSpeechMs = 0;
  private userSilenceMs = 0;
  private userActivityActive = false;
  private turnHasUserActivity = false;
  private assistantTranscript = '';
  private activeUserTurnId: string | null = null;
  private turnSequence = 0;
  private qwenMedia = new QwenOmniMediaSequencer();

  constructor(
    private readonly config: RealtimeDuplexConfig,
    private readonly callbacks: RealtimeDuplexCallbacks,
  ) {}

  async start(): Promise<void> {
    this.callbacks.onState('connecting');
    this.playbackContext = new AudioContext({ sampleRate: OUTPUT_RATE });
    await this.playbackContext.audioWorklet.addModule(
      new URL('./duplex-playback.js', import.meta.url),
    );
    this.playbackNode = new AudioWorkletNode(this.playbackContext, 'jiuwen-duplex-playback');
    this.playbackNode.port.onmessage = ({ data }) => {
      if (data.type === 'cleared') {
        this.playbackGeneration += 1;
        this.playbackOperation = Promise.resolve();
        this.queuedDrainResponseId = null;
        this.assistantPlaying = false;
        return;
      }
      if (data.type !== 'drained') return;
      if (this.queuedDrainResponseId === data.responseId) this.queuedDrainResponseId = null;
      this.assistantPlaying = false;
      if (!this.responseActive) this.callbacks.onState('listening');
    };
    this.playbackNode.connect(this.playbackContext.destination);
    await this.playbackContext.resume();

    this.emitDiagnostic('realtime_microphone_request_started', {});
    this.microphone = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.emitDiagnostic('realtime_microphone_ready', {});
    this.captureContext = new AudioContext({ sampleRate: INPUT_RATE });
    await this.captureContext.audioWorklet.addModule(
      new URL('./duplex-capture.js', import.meta.url),
    );
    const source = this.captureContext.createMediaStreamSource(this.microphone);
    this.captureNode = new AudioWorkletNode(this.captureContext, 'jiuwen-duplex-capture');
    this.captureNode.port.onmessage = ({ data }) => {
      const pcm = resample(new Int16Array(data), this.captureContext?.sampleRate || INPUT_RATE, INPUT_RATE);
      this.pending.push(pcm);
      this.pendingSamples += pcm.length;
      const maxPending = INPUT_RATE * 2;
      while (this.pendingSamples > maxPending && this.pending.length > 1) {
        this.pendingSamples -= this.pending.shift()?.length || 0;
      }
      const level = this.rms(pcm);
      const frameMs = pcm.length * 1_000 / INPUT_RATE;
      const threshold = Math.max(350, this.noiseFloor * 2.5);
      if (level > threshold) {
        this.userSpeechMs += frameMs;
        this.userSilenceMs = 0;
        if (!this.userActivityActive && this.userSpeechMs >= LISTENING_SPEECH_MS) {
          this.userActivityActive = true;
          this.turnHasUserActivity = true;
          this.activeUserTurnId = this.newTurnId('voice');
          this.interruptQwenResponse(this.activeUserTurnId, this.userSpeechMs, level, threshold);
          this.emitDiagnostic('realtime_user_turn_started', {
            turn_id: this.activeUserTurnId,
            speech_ms: Math.round(this.userSpeechMs),
          });
        }
      } else {
        this.userSilenceMs += frameMs;
        if (!this.userActivityActive) this.userSpeechMs = 0;
        if (this.userSilenceMs >= USER_TURN_SILENCE_MS) this.userActivityActive = false;
      }
      this.observeNoise(pcm);
    };
    const silent = this.captureContext.createGain();
    silent.gain.value = 0;
    source.connect(this.captureNode);
    this.captureNode.connect(silent).connect(this.captureContext.destination);
    await this.captureContext.resume();

    this.emitDiagnostic('realtime_websocket_connecting', { url: this.config.url });
    await this.openSocket();
    this.sendTimer = window.setInterval(() => this.flush(), SEND_INTERVAL_MS);
  }

  stop(): void {
    if (this.sendTimer !== null) window.clearInterval(this.sendTimer);
    this.sendTimer = null;
    this.send(createQwenOmniFinishSessionEvent());
    this.socket?.close(1000, 'client stop');
    this.socket = null;
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.microphone = null;
    void this.captureContext?.close();
    void this.playbackContext?.close();
    this.captureContext = null;
    this.playbackContext = null;
    this.pending = [];
    this.pendingSamples = 0;
    this.pendingToolResults = [];
    this.acceptedToolResultIds.clear();
    this.acceptedFunctionCallIds.clear();
    this.activeToolJobId = null;
    this.sessionReady = false;
    this.responseActive = false;
    this.activeUserTurnId = null;
    this.userSpeechMs = 0;
    this.userSilenceMs = 0;
    this.userActivityActive = false;
    this.qwenMedia.reset();
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    this.callbacks.onState('closed');
  }

  async sendTextTurn(text: string, isFresh: () => boolean = () => true): Promise<boolean> {
    const normalized = text.trim();
    if (!normalized || !isFresh()) return false;
    createQwenOmniTextTurnEvents(normalized).forEach((event) => this.send(event));
    this.emitDiagnostic('qwen_text_input_dispatched', { text: normalized });
    return true;
  }

  enqueueToolResult(toolResult: RealtimeToolResult): boolean {
    const jobId = toolResult.jobId.trim();
    const question = toolResult.question.trim();
    const result = toolResult.result.trim();
    const callId = toolResult.callId?.trim();
    if (!jobId || !question || !result || this.acceptedToolResultIds.has(jobId)) return false;
    if (!callId) return false;
    this.acceptedToolResultIds.add(jobId);
    this.pendingToolResults.push({ jobId, question, result, ...(callId ? { callId } : {}) });
    this.emitDiagnostic('search_result_queued', {
      job_id: jobId,
      question,
      result,
    });
    if (this.acceptedToolResultIds.size > 32) {
      const oldest = this.acceptedToolResultIds.values().next().value;
      if (oldest) this.acceptedToolResultIds.delete(oldest);
    }
    return true;
  }

  private emitDiagnostic(event: string, details: Record<string, unknown>): void {
    this.callbacks.onDiagnostic?.({
      event,
      client_time: new Date().toISOString(),
      client_build: REALTIME_CLIENT_BUILD,
      ...details,
    });
  }

  private openSocket(): Promise<void> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.config.url);
      const socket = new WebSocket(url);
      this.socket = socket;
      let initSent = false;
      let settled = false;
      const initTimeout = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        socket.close(1000, 'session init timeout');
        reject(new Error('Realtime 会话初始化超时，远端未返回 session.created'));
      }, 30_000);
      const resolveOnce = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(initTimeout);
        resolve();
      };
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(initTimeout);
        reject(error);
      };
      const sendInit = async () => {
        if (initSent) return;
        initSent = true;
        this.send(createQwenOmniSessionUpdate({
          voice: this.config.voice,
          tools: this.config.tools,
          inputRate: INPUT_RATE,
          outputRate: OUTPUT_RATE,
        }));
      };
      socket.onopen = () => {
        this.emitDiagnostic('realtime_websocket_open', { url: url.toString() });
        void sendInit();
        // Match the official client: microphone upload begins as soon as the
        // native duplex socket is open instead of waiting for session.created.
        resolveOnce();
      };
      socket.onmessage = ({ data }) => {
        if (this.socket !== socket) return;
        if (typeof data !== 'string') return;
        try {
          const event = JSON.parse(data) as Record<string, unknown>;
          const type = String(event.type || '');
          if (type === 'session.closed' && !this.sessionReady) {
            const closeReason = readableError(event.reason || event.error || '远端在初始化阶段主动关闭');
            const startupAlreadyResolved = settled;
            this.emitDiagnostic('realtime_websocket_error', {
              url: url.toString(),
              message: closeReason,
            });
            rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
            if (startupAlreadyResolved) this.callbacks.onError(`Realtime 会话初始化失败：${closeReason}`);
            return;
          }
          if (type === 'session.queue_done' || type === 'queue_done') {
            void sendInit();
            return;
          }
          if (type === 'session.updated' || type === 'session.created') {
            this.sessionReady = true;
            this.emitDiagnostic('realtime_session_ready', {});
          }
          this.handleEvent(event);
        }
        catch { this.callbacks.onError('Realtime 返回了无效事件'); }
      };
      socket.onerror = () => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_error', { url: url.toString() });
        rejectOnce(new Error(`Realtime WebSocket 连接失败：${url}`));
      };
      socket.onclose = ({ code, reason }) => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_closed', { code, message: reason });
        if (!this.sessionReady) {
          const closeReason = reason || (code === 1000 ? '远端在初始化阶段主动关闭' : `关闭代码 ${code}`);
          const startupAlreadyResolved = settled;
          rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
          if (startupAlreadyResolved) this.callbacks.onError(`Realtime 会话初始化失败：${closeReason}`);
        } else if (code !== 1000) {
          this.callbacks.onError(`Realtime 连接已断开（${code}），请确认远端模型服务仍可用。`);
        }
        this.callbacks.onState('closed');
      };
    });
  }

  private send(event: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }

  private flush(): void {
    if (this.pendingSamples === 0) return;
    const outgoing = new Int16Array(this.pendingSamples);
    let written = 0;
    while (written < outgoing.length && this.pending.length > 0) {
      const chunk = this.pending[0];
      const count = Math.min(chunk.length, outgoing.length - written);
      outgoing.set(chunk.subarray(0, count), written);
      written += count;
      this.pendingSamples -= count;
      if (count === chunk.length) this.pending.shift();
      else this.pending[0] = chunk.slice(count);
    }
    this.dispatchQueuedToolResult();
    this.sendAudio(outgoing, true);
    if (this.turnHasUserActivity && !this.userActivityActive && this.userSilenceMs >= USER_TURN_SILENCE_MS) {
      this.turnHasUserActivity = false;
      this.activeUserTurnId = null;
      this.userSpeechMs = 0;
      this.userSilenceMs = 0;
    }
  }

  private rms(pcm: Int16Array): number {
    let energy = 0;
    for (let index = 0; index < pcm.length; index += 8) energy += pcm[index] * pcm[index];
    return Math.sqrt(energy / Math.ceil(pcm.length / 8));
  }

  private observeNoise(pcm: Int16Array): void {
    const level = this.rms(pcm);
    if (level > this.noiseFloor * 3) return;
    const weight = level < this.noiseFloor ? 0.05 : 0.005;
    this.noiseFloor = Math.max(50, this.noiseFloor * (1 - weight) + level * weight);
  }

  private sendAudio(pcm: Int16Array, includeVideo: boolean): void {
    const audio = bytesToBase64(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength));
    const batch = this.qwenMedia.createBatch(
      audio,
      includeVideo,
      includeVideo ? this.callbacks.getVideoFrame() : null,
    );
    batch.events.forEach((event) => this.send(event));
    batch.diagnostics.forEach(({ event, details }) => this.emitDiagnostic(event, details));
  }

  private dispatchQueuedToolResult(): void {
    if (this.pendingToolResults.length > 0
      && !this.responseActive
      && !this.assistantPlaying
      && !this.userActivityActive
      && !this.turnHasUserActivity) {
      const toolResult = this.pendingToolResults.shift();
      if (toolResult) {
        this.emitDiagnostic('search_result_dispatched', {
          job_id: toolResult.jobId,
        });
        this.callbacks.onToolResultDispatched?.(toolResult.jobId);
        if (toolResult.callId) {
          this.activeToolJobId = toolResult.jobId;
          this.responseActive = true;
          createQwenOmniToolResultEvents(
            toolResult.callId,
            toolResult.result,
            toolResult.question,
          ).forEach((event) => this.send(event));
          this.emitDiagnostic('qwen_tool_result_returned', {
            job_id: toolResult.jobId,
            call_id: toolResult.callId,
          });
        }
      }
    }
  }

  private handleEvent(event: Record<string, unknown>): void {
    const type = String(event.type || '');
    const response = event.response as Record<string, unknown> | undefined;
    const eventResponseId = String(event.response_id || response?.id || '') || null;
    const functionCall = parseQwenOmniFunctionCall(event);
    if (functionCall) {
      if (!this.acceptedFunctionCallIds.has(functionCall.callId)) {
        this.acceptedFunctionCallIds.add(functionCall.callId);
        if (this.acceptedFunctionCallIds.size > 32) {
          const oldest = this.acceptedFunctionCallIds.values().next().value;
          if (oldest) this.acceptedFunctionCallIds.delete(oldest);
        }
        this.emitDiagnostic('qwen_tool_call_received', {
          name: functionCall.name,
          call_id: functionCall.callId,
          query: functionCall.query,
        });
        this.callbacks.onFunctionCall?.(functionCall);
      }
    } else if (type === 'response.function_call_arguments.done') {
      this.emitDiagnostic('qwen_tool_call_invalid', {
        name: String(event.name || ''),
        call_id: String(event.call_id || ''),
      });
      this.callbacks.onError('千问返回了无效的工具调用');
    } else if (type === 'session.created') {
      this.callbacks.onState('listening');
    } else if (type === 'response.created') {
      if (eventResponseId) this.responseId = eventResponseId;
      this.responseActive = true;
      this.userSpeechMs = 0;
      this.userSilenceMs = 0;
      this.userActivityActive = false;
      this.assistantTranscript = '';
      this.callbacks.onState('speaking');
    } else if (type === 'response.text.delta') {
      this.beginOfficialTurn(eventResponseId);
      const delta = String(event.delta || '');
      this.assistantTranscript += delta;
      this.callbacks.onAssistantText(
        this.assistantTranscript,
        false,
        this.activeToolJobId || undefined,
      );
    } else if (type === 'response.text.done') {
      this.assistantTranscript = String(event.text || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'audio.cancelled' || type === 'response.audio.cancelled'
      || type === 'response.cancelled') {
      if (!eventResponseId || eventResponseId === this.responseId) {
        this.responseActive = false;
        this.playbackNode?.port.postMessage({ type: 'clear', cancelResponse: false });
        this.callbacks.onState('listening');
      }
    } else if (type === 'response.audio.delta' || type === 'response.output_audio.delta') {
      const encoded = String(event.delta || event.audio || '');
      if (!encoded || !this.playbackNode) return;
      const responseId = eventResponseId || this.responseId;
      this.enqueueAudioDelta(event, encoded, responseId);
    } else if (type === 'response.audio.done' || type === 'response.output_audio.done' || type === 'response.done') {
      const affectsActive = !eventResponseId || eventResponseId === this.responseId;
      if (type === 'response.done' && affectsActive) {
        this.responseActive = false;
      }
      if (affectsActive) this.enqueuePlaybackDrain(eventResponseId || this.responseId);
    } else if (type === 'response.audio_transcript.delta'
      || type === 'response.output_audio_transcript.delta') {
      const delta = String(event.delta || '');
      if (!delta) return;
      if (delta.startsWith(this.assistantTranscript)) this.assistantTranscript = delta;
      else if (!this.assistantTranscript.endsWith(delta)) this.assistantTranscript += delta;
      this.callbacks.onAssistantText(
        this.assistantTranscript,
        false,
        this.activeToolJobId || undefined,
      );
    } else if (type === 'response.audio_transcript.done'
      || type === 'response.output_audio_transcript.done') {
      this.assistantTranscript = String(event.transcript || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'conversation.item.input_audio_transcription.delta') {
      this.callbacks.onUserText(
        `${String(event.delta || event.text || '')}${String(event.stash || '')}`,
        false,
      );
    } else if (type === 'conversation.item.input_audio_transcription.completed') {
      const transcript = String(event.transcript || '');
      this.emitDiagnostic('qwen_native_asr_completed', {
        transcript,
        has_transcript: Boolean(transcript.trim()),
      });
      this.callbacks.onUserText(transcript, true);
    } else if (type === 'error') {
      const media = this.qwenMedia.snapshot();
      this.emitDiagnostic('qwen_realtime_error', {
        raw_event: event,
        audio_append_sequence: media.audioAppendSequence,
        image_append_sequence: media.imageAppendSequence,
        has_deferred_image: media.hasDeferredImage,
      });
      this.callbacks.onError(readableError(event.error || event));
    }
  }

  private interruptQwenResponse(
    turnId: string,
    speechMs: number,
    level: number,
    threshold: number,
  ): boolean {
    if (!this.responseActive && !this.assistantPlaying) return false;
    const interruptedResponseId = this.responseId;
    const cancelEventSent = this.responseActive;
    if (cancelEventSent) this.send(createQwenOmniCancelResponseEvent());
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    this.playbackNode?.port.postMessage({ type: 'clear', cancelResponse: false });
    this.responseActive = false;
    this.assistantPlaying = false;
    if (this.assistantTranscript) this.finishAssistantText();
    this.emitDiagnostic('qwen_response_interrupted_by_user', {
      turn_id: turnId,
      response_id: interruptedResponseId,
      speech_ms: Math.round(speechMs),
      audio_level: Math.round(level),
      speech_threshold: Math.round(threshold),
      cancel_event_sent: cancelEventSent,
    });
    this.callbacks.onState('listening');
    return true;
  }

  private beginOfficialTurn(responseId: string | null): void {
    if (this.responseActive) return;
    if (responseId) this.responseId = responseId;
    this.responseActive = true;
    this.assistantTranscript = '';
    this.callbacks.onState('speaking');
  }

  private finishAssistantText(): void {
    const text = this.assistantTranscript;
    const toolJobId = this.activeToolJobId || undefined;
    this.callbacks.onAssistantText(text, true, toolJobId);
    this.emitDiagnostic('realtime_answer_final', {
      realtime_answer: text,
      ...(toolJobId ? { job_id: toolJobId } : {}),
    });
    if (toolJobId) {
      this.emitDiagnostic('search_result_answered', {
        job_id: toolJobId,
        realtime_answer: text,
      });
    }
    this.assistantTranscript = '';
    this.activeToolJobId = null;
  }

  private enqueueAudioDelta(
    event: Record<string, unknown>,
    encoded: string,
    responseId: string | null,
  ): void {
    const generation = this.playbackGeneration;
    this.playbackOperation = this.playbackOperation
      .then(() => this.decodeOutputAudio(event, encoded))
      .then((output) => {
        if (generation !== this.playbackGeneration || !output || !this.playbackNode) return;
        this.assistantPlaying = true;
        this.playbackNode.port.postMessage({
          type: 'audio',
          lane: 'normal',
          pcm: output.buffer,
          responseId,
          initialBufferMs: INITIAL_PLAYBACK_BUFFER_MS,
        }, [output.buffer]);
      })
      .catch(() => this.callbacks.onError('Realtime 音频解码失败'));
  }

  private enqueuePlaybackDrain(responseId: string | null): void {
    if (responseId && this.queuedDrainResponseId === responseId) return;
    this.queuedDrainResponseId = responseId;
    const generation = this.playbackGeneration;
    this.playbackOperation = this.playbackOperation.then(() => {
      if (generation !== this.playbackGeneration) return;
      this.playbackNode?.port.postMessage({ type: 'drain', lane: 'normal', responseId });
    });
  }

  private async decodeOutputAudio(_event: Record<string, unknown>, encoded: string): Promise<Int16Array> {
    const bytes = base64ToBytes(encoded);
    return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
  }

  private newTurnId(prefix: string): string {
    this.turnSequence += 1;
    return prefix + '-' + Date.now() + '-' + this.turnSequence;
  }
}
