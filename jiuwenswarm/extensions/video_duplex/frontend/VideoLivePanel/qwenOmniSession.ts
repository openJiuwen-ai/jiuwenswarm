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
import type { RealtimeBrief } from './types.js';
import { createQwenOmniToolOutputEvent } from './qwenOmniTools.js';
import type { SileroVad, SpeechDetection } from '../../../../channels/web/frontend/src/utils/speechDetection/sileroVad';

export interface RealtimeDuplexConfig {
  url: string;
  voice?: string;
  tools?: Array<Record<string, unknown>>;
}

export interface RealtimeToolResult {
  jobId: string;
  turnId?: string;
  question: string;
  brief: RealtimeBrief;
  callId?: string;
}

export interface RealtimeDuplexCallbacks {
  getVideoFrame: () => string | null;
  onAssistantText: (text: string, final: boolean, toolJobId?: string, responseId?: string) => void;
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
const REALTIME_CLIENT_BUILD = 'qwen-silero-recovery-v2';
const INITIAL_PLAYBACK_BUFFER_MS = 400;

function readableError(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const error = value as Record<string, unknown>;
    const message = error.message || error.detail || error.code || error.error;
    if (message) return readableError(message);
    try {
      return JSON.stringify(value);
    } catch {
      return 'Realtime 服务错误';
    }
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
  private toolResultWaitKey = '';
  private acceptedToolResultIds = new Set<string>();
  private acceptedFunctionCallIds = new Set<string>();
  private pendingToolResponseJobId: string | null = null;
  private responseToolJobIds = new Map<string, string>();
  private assistantPlaying = false;
  private responseActive = false;
  private playbackOperation: Promise<void> = Promise.resolve();
  private playbackGeneration = 0;
  private queuedDrainResponseId: string | null = null;
  private vad: SileroVad | null = null;
  private lifecycle = 0;
  private rejectedCandidateMs = 0;
  private interruptedResponseIds = new Set<string>();
  private lastSpeechDetectionAt = 0;
  private lastSpeechLevel = 0;
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
    const lifecycle = ++this.lifecycle;
    this.callbacks.onState('connecting');
    this.emitDiagnostic('qwen_vad_loading', { model: 'silero-v5' });
    const { SileroVad } = await import('../../../../channels/web/frontend/src/utils/speechDetection/sileroVad');
    if (lifecycle !== this.lifecycle) return;
    const vad = new SileroVad(
      (detection) => this.handleSpeechDetection(detection),
      (message) => {
        this.userActivityActive = false;
        this.turnHasUserActivity = false;
        this.activeUserTurnId = null;
        this.emitDiagnostic('qwen_vad_error', { message });
        this.callbacks.onError(`本地人声检测暂不可用：${message}。`);
      },
      (event, details) => {
        if (event === 'qwen_vad_resync') {
          this.userActivityActive = false;
          this.turnHasUserActivity = false;
          this.activeUserTurnId = null;
        }
        this.emitDiagnostic(event, details);
      },
    );
    this.vad = vad;
    try {
      await vad.start();
    } catch (error) {
      vad.stop();
      this.emitDiagnostic('qwen_vad_error', { message: String(error) });
      throw error;
    }
    if (lifecycle !== this.lifecycle) return;
    this.emitDiagnostic('qwen_vad_ready', { model: 'silero-v5' });
    this.playbackContext = new AudioContext({ sampleRate: OUTPUT_RATE });
    await this.playbackContext.audioWorklet.addModule(new URL('./duplex-playback.js', import.meta.url));
    if (lifecycle !== this.lifecycle) return;
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
    if (lifecycle !== this.lifecycle) return;

    this.emitDiagnostic('realtime_microphone_request_started', {});
    const microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (lifecycle !== this.lifecycle) {
      microphone.getTracks().forEach((track) => track.stop());
      return;
    }
    this.microphone = microphone;
    this.emitDiagnostic('realtime_microphone_ready', {});
    this.captureContext = new AudioContext({ sampleRate: INPUT_RATE });
    await this.captureContext.audioWorklet.addModule(new URL('./duplex-capture.js', import.meta.url));
    if (lifecycle !== this.lifecycle) return;
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
      this.vad?.push(pcm);
    };
    const silent = this.captureContext.createGain();
    silent.gain.value = 0;
    source.connect(this.captureNode);
    this.captureNode.connect(silent).connect(this.captureContext.destination);
    await this.captureContext.resume();
    if (lifecycle !== this.lifecycle) return;

    this.emitDiagnostic('realtime_websocket_connecting', {
      url: this.config.url,
    });
    await this.openSocket();
    if (lifecycle !== this.lifecycle) return;
    this.sendTimer = window.setInterval(() => this.flush(), SEND_INTERVAL_MS);
  }

  stop(): void {
    this.lifecycle += 1;
    this.vad?.stop();
    this.vad = null;
    if (this.sendTimer !== null) window.clearInterval(this.sendTimer);
    this.sendTimer = null;
    if (this.assistantTranscript) this.finishAssistantText();
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
    this.toolResultWaitKey = '';
    this.acceptedToolResultIds.clear();
    this.acceptedFunctionCallIds.clear();
    this.pendingToolResponseJobId = null;
    this.responseToolJobIds.clear();
    this.sessionReady = false;
    this.responseActive = false;
    this.activeUserTurnId = null;
    this.userSpeechMs = 0;
    this.userSilenceMs = 0;
    this.userActivityActive = false;
    this.turnHasUserActivity = false;
    this.interruptedResponseIds.clear();
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

  cancelToolTask(jobId: string, callId: string): void {
    this.pendingToolResults = this.pendingToolResults.filter((item) => item.jobId !== jobId);
    if (this.acceptedToolResultIds.has(jobId)) return;
    this.acceptedToolResultIds.add(jobId);
    this.send(createQwenOmniToolOutputEvent(callId, JSON.stringify({
      status: 'cancelled', job_id: jobId,
      message: '用户手动停止了此任务。不要重试，不要宣称完成；取消记录已显示，无需播报。',
    })));
  }

  enqueueToolResult(toolResult: RealtimeToolResult): boolean {
    const jobId = toolResult.jobId.trim();
    const question = toolResult.question.trim();
    const summary = toolResult.brief.summary.trim();
    const callId = toolResult.callId?.trim();
    if (!jobId || !question || !summary || this.acceptedToolResultIds.has(jobId)) return false;
    if (!callId) return false;
    this.acceptedToolResultIds.add(jobId);
    this.pendingToolResults.push({
      jobId,
      ...(toolResult.turnId?.trim() ? { turnId: toolResult.turnId.trim() } : {}),
      question,
      brief: { ...toolResult.brief, summary },
      ...(callId ? { callId } : {}),
    });
    this.emitDiagnostic('search_result_queued', {
      job_id: jobId,
      question,
      result_kind: toolResult.brief.result_kind,
      brief_chars: summary.length,
    });
    this.dispatchQueuedToolResult();
    // Keep job IDs for the entire live session so delayed polls cannot replay an old result.
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
        this.send(
          createQwenOmniSessionUpdate({
            voice: this.config.voice,
            tools: this.config.tools,
            inputRate: INPUT_RATE,
            outputRate: OUTPUT_RATE,
          }),
        );
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
          this.handleEvent(event, data);
        } catch {
          this.callbacks.onError('Realtime 返回了无效事件');
        }
      };
      socket.onerror = () => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_error', {
          url: url.toString(),
        });
        rejectOnce(new Error(`Realtime WebSocket 连接失败：${url}`));
      };
      socket.onclose = ({ code, reason }) => {
        if (this.socket !== socket) return;
        if (this.assistantTranscript) this.finishAssistantText();
        this.emitDiagnostic('realtime_websocket_closed', {
          code,
          message: reason,
        });
        if (!this.sessionReady) {
          const closeReason = reason || (code === 1000 ? '远端在初始化阶段主动关闭' : `关闭代码 ${code}`);
          const startupAlreadyResolved = settled;
          rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
          if (startupAlreadyResolved) this.callbacks.onError(`Realtime 会话初始化失败：${closeReason}`);
        } else if (code !== 1000) {
          this.callbacks.onError(`Realtime 连接已断开（${code}），请确认远端模型服务仍可用。`);
        }
        // Release media on remote disconnect as well. Conversation-owned work continues outside this session.
        this.stop();
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

  private handleSpeechDetection(detection: SpeechDetection): void {
    this.lastSpeechDetectionAt = performance.now();
    this.lastSpeechLevel = detection.level;
    this.userSpeechMs = detection.speechMs;
    this.userSilenceMs = detection.silenceMs;
    this.userActivityActive = detection.state === 'started' || detection.state === 'active';
    if (detection.state === 'candidate') this.rejectedCandidateMs = detection.speechMs;
    if (detection.state === 'idle' && this.rejectedCandidateMs) {
      this.emitDiagnostic('qwen_vad_candidate_rejected', {
        speech_ms: this.rejectedCandidateMs,
        speech_probability: detection.probability,
        noise_floor: Math.round(detection.noiseFloor),
      });
      this.rejectedCandidateMs = 0;
    }
    if (detection.state !== 'started') {
      this.interruptWhileUserSpeaking();
      return;
    }
    this.rejectedCandidateMs = 0;
    this.turnHasUserActivity = true;
    this.activeUserTurnId = this.newTurnId('voice');
    this.interruptQwenResponse(this.activeUserTurnId, detection.speechMs, detection.level, 80);
    this.emitDiagnostic('realtime_user_turn_started', {
      turn_id: this.activeUserTurnId,
      source: 'silero-v5',
      speech_ms: detection.speechMs,
      speech_probability: detection.probability,
      noise_floor: Math.round(detection.noiseFloor),
      audio_level: Math.round(detection.level),
    });
  }

  private sendAudio(pcm: Int16Array, includeVideo: boolean): void {
    const audio = bytesToBase64(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength));
    const batch = this.qwenMedia.createBatch(audio, includeVideo, includeVideo ? this.callbacks.getVideoFrame() : null);
    batch.events.forEach((event) => this.send(event));
    batch.diagnostics.forEach(({ event, details }) => this.emitDiagnostic(event, details));
  }

  private dispatchQueuedToolResult(): void {
    if (!this.pendingToolResults.length) {
      this.toolResultWaitKey = '';
      return;
    }
    const reason =
      this.socket?.readyState !== WebSocket.OPEN || !this.sessionReady
        ? 'connection_not_ready'
        : this.userActivityActive || this.turnHasUserActivity
          ? 'user_speaking'
          : this.responseActive
            ? 'response_generating'
            : '';
    if (reason) {
      const jobId = this.pendingToolResults[0].jobId;
      const waitKey = `${jobId}:${reason}`;
      if (waitKey !== this.toolResultWaitKey) {
        this.toolResultWaitKey = waitKey;
        this.emitDiagnostic('qwen_tool_result_waiting', {
          job_id: jobId,
          reason,
          queued_count: this.pendingToolResults.length,
        });
      }
      return;
    }
    this.toolResultWaitKey = '';
    while (this.pendingToolResults.length > 0) {
      const toolResult = this.pendingToolResults.shift();
      if (!toolResult) return;
      this.emitDiagnostic('search_result_dispatched', {
        job_id: toolResult.jobId,
        turn_id: toolResult.turnId,
      });
      this.callbacks.onToolResultDispatched?.(toolResult.jobId);
      if (!toolResult.callId) continue;
      this.pendingToolResponseJobId = toolResult.jobId;
      this.responseActive = true;
      createQwenOmniToolResultEvents(toolResult.callId, toolResult.brief, {
        jobId: toolResult.jobId,
        turnId: toolResult.turnId,
        question: toolResult.question,
      }).forEach((event) => this.send(event));
      this.emitDiagnostic('qwen_tool_result_returned', {
        job_id: toolResult.jobId,
        turn_id: toolResult.turnId,
        call_id: toolResult.callId,
        question: toolResult.question,
      });
      return;
    }
  }

  private handleEvent(event: Record<string, unknown>, rawEvent = ''): void {
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
          task: functionCall.task,
        });
        this.callbacks.onFunctionCall?.(functionCall);
      }
    } else if (type === 'response.function_call_arguments.done') {
      this.emitDiagnostic('qwen_tool_call_invalid', {
        name: String(event.name || ''),
        call_id: String(event.call_id || ''),
        arguments: typeof event.arguments === 'string' ? event.arguments.slice(0, 2_000) : event.arguments,
      });
      this.callbacks.onError('千问返回了无效的工具调用');
    } else if (type === 'session.created') {
      this.callbacks.onState('listening');
    } else if (type === 'response.created') {
      // Some providers start the tool-call response before ending its spoken acknowledgement.
      if (this.assistantTranscript) this.finishAssistantText();
      this.responseId = eventResponseId || this.newTurnId('response');
      if (this.pendingToolResponseJobId) {
        this.responseToolJobIds.set(this.responseId, this.pendingToolResponseJobId);
        this.pendingToolResponseJobId = null;
        if (this.responseToolJobIds.size > 32) {
          const oldest = this.responseToolJobIds.keys().next().value;
          if (oldest) this.responseToolJobIds.delete(oldest);
        }
      }
      this.responseActive = true;
      // Only VAD ends user speech. A model response must not re-arm interruption.
      this.assistantTranscript = '';
      if (!this.interruptWhileUserSpeaking()) this.callbacks.onState('speaking');
    } else if (type === 'response.text.delta') {
      this.beginOfficialTurn(eventResponseId);
      const delta = String(event.delta || '');
      this.assistantTranscript += delta;
      this.callbacks.onAssistantText(
        this.assistantTranscript,
        false,
        this.toolJobIdForResponse(eventResponseId),
        eventResponseId || this.responseId || undefined,
      );
    } else if (type === 'response.text.done') {
      this.assistantTranscript = String(event.text || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'audio.cancelled' || type === 'response.audio.cancelled' || type === 'response.cancelled') {
      this.emitDiagnostic('qwen_response_cancelled', {
        response_id: eventResponseId,
        source: eventResponseId && this.interruptedResponseIds.has(eventResponseId) ? 'local_vad' : 'provider',
        raw_event: rawEvent || JSON.stringify(event),
      });
      if (!eventResponseId || eventResponseId === this.responseId) {
        if (this.assistantTranscript) this.finishAssistantText();
        this.responseActive = false;
        this.playbackNode?.port.postMessage({
          type: 'clear',
          cancelResponse: false,
        });
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
        if (this.assistantTranscript) this.finishAssistantText();
        this.responseActive = false;
      }
      if (affectsActive) this.enqueuePlaybackDrain(eventResponseId || this.responseId);
      if (type === 'response.done' && affectsActive) this.dispatchQueuedToolResult();
    } else if (type === 'response.audio_transcript.delta' || type === 'response.output_audio_transcript.delta') {
      const delta = String(event.delta || '');
      if (!delta) return;
      if (delta.startsWith(this.assistantTranscript)) this.assistantTranscript = delta;
      else if (!this.assistantTranscript.endsWith(delta)) this.assistantTranscript += delta;
      this.callbacks.onAssistantText(
        this.assistantTranscript,
        false,
        this.toolJobIdForResponse(eventResponseId),
        eventResponseId || this.responseId || undefined,
      );
    } else if (type === 'response.audio_transcript.done' || type === 'response.output_audio_transcript.done') {
      this.assistantTranscript = String(event.transcript || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'input_audio_buffer.speech_started' || type === 'input_audio_buffer.speech_stopped') {
      this.emitDiagnostic('qwen_provider_vad', { source: type, raw_event: rawEvent || JSON.stringify(event) });
    } else if (type === 'conversation.item.input_audio_transcription.delta') {
      this.callbacks.onUserText(`${String(event.delta || event.text || '')}${String(event.stash || '')}`, false);
    } else if (type === 'conversation.item.input_audio_transcription.completed') {
      const transcript = String(event.transcript || '');
      this.emitDiagnostic('qwen_native_asr_completed', {
        transcript,
        has_transcript: Boolean(transcript.trim()),
      });
      this.callbacks.onUserText(transcript, true);
    } else if (type === 'error') {
      if (this.assistantTranscript) this.finishAssistantText();
      const media = this.qwenMedia.snapshot();
      const error = event.error;
      const errorRecord = error && typeof error === 'object' ? (error as Record<string, unknown>) : {};
      this.emitDiagnostic('qwen_realtime_error', {
        raw_event: rawEvent || JSON.stringify(event),
        code: String(errorRecord.code || event.code || 'unknown'),
        error_type: String(errorRecord.type || ''),
        message: readableError(error || event),
        audio_append_sequence: media.audioAppendSequence,
        image_append_sequence: media.imageAppendSequence,
        has_deferred_image: media.hasDeferredImage,
      });
      this.callbacks.onError(readableError(error || event));
    }
  }

  private interruptQwenResponse(turnId: string, speechMs: number, level: number, threshold: number): boolean {
    if (!this.responseActive && !this.assistantPlaying) return false;
    const interruptedResponseId = this.responseId;
    if (interruptedResponseId && this.interruptedResponseIds.has(interruptedResponseId)) return false;
    if (interruptedResponseId) {
      this.interruptedResponseIds.add(interruptedResponseId);
      if (this.interruptedResponseIds.size > 64) {
        this.interruptedResponseIds.delete(this.interruptedResponseIds.values().next().value!);
      }
    }
    const cancelEventSent = this.responseActive;
    if (cancelEventSent) this.send(createQwenOmniCancelResponseEvent());
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    this.playbackNode?.port.postMessage({
      type: 'clear',
      cancelResponse: false,
    });
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
      source: 'silero-v5',
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
    const toolJobId = this.toolJobIdForResponse(this.responseId);
    this.callbacks.onAssistantText(text, true, toolJobId, this.responseId || undefined);
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
  }

  private toolJobIdForResponse(responseId: string | null): string | undefined {
    return responseId ? this.responseToolJobIds.get(responseId) : undefined;
  }

  private userIsSpeakingNow(): boolean {
    // Exclude VAD's end-of-utterance hangover and stale detections after a worker stall.
    return this.userActivityActive && this.userSilenceMs < 160 && performance.now() - this.lastSpeechDetectionAt < 500;
  }

  private interruptWhileUserSpeaking(): boolean {
    if (!this.userIsSpeakingNow() || !this.activeUserTurnId) return false;
    return this.interruptQwenResponse(this.activeUserTurnId, this.userSpeechMs, this.lastSpeechLevel, 80);
  }

  private enqueueAudioDelta(event: Record<string, unknown>, encoded: string, responseId: string | null): void {
    if (responseId && this.interruptedResponseIds.has(responseId)) return;
    if (this.userIsSpeakingNow()) {
      this.interruptWhileUserSpeaking();
      if (responseId) this.interruptedResponseIds.add(responseId);
      this.emitDiagnostic('qwen_audio_blocked_during_speech', { response_id: responseId });
      return;
    }
    const generation = this.playbackGeneration;
    this.playbackOperation = this.playbackOperation
      .then(() => this.decodeOutputAudio(event, encoded))
      .then((output) => {
        if (generation !== this.playbackGeneration || !output || !this.playbackNode) return;
        if (responseId && this.interruptedResponseIds.has(responseId)) return;
        if (this.userIsSpeakingNow()) {
          this.interruptWhileUserSpeaking();
          if (responseId) this.interruptedResponseIds.add(responseId);
          this.emitDiagnostic('qwen_audio_blocked_during_speech', { response_id: responseId });
          return;
        }
        this.assistantPlaying = true;
        this.playbackNode.port.postMessage(
          {
            type: 'audio',
            lane: 'normal',
            pcm: output.buffer,
            responseId,
            initialBufferMs: INITIAL_PLAYBACK_BUFFER_MS,
          },
          [output.buffer],
        );
      })
      .catch(() => this.callbacks.onError('Realtime 音频解码失败'));
  }

  private enqueuePlaybackDrain(responseId: string | null): void {
    if (responseId && this.queuedDrainResponseId === responseId) return;
    this.queuedDrainResponseId = responseId;
    const generation = this.playbackGeneration;
    this.playbackOperation = this.playbackOperation.then(() => {
      if (generation !== this.playbackGeneration) return;
      this.playbackNode?.port.postMessage({
        type: 'drain',
        lane: 'normal',
        responseId,
      });
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
