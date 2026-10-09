import { bytesToBase64, base64ToBytes, resample } from './audio/pcm.js';
import {
  taskCancellationNotice,
  taskQuestionNotice,
  taskResultNotice,
  TASK_OPERATION_INSTRUCTIONS,
} from './taskPrompts.js';
import { parseTaskCall } from './taskBridge.js';
import type { RealtimeFunctionCall } from './taskBridge.js';
import type { RealtimeBrief } from './taskTypes.js';
import { createRealtimeProvider } from './providers/registry.js';
import type { RealtimeProvider, ProviderEvent, WireEvent } from './providers/contract.js';

export interface SpeechDetection {
  state: 'idle' | 'candidate' | 'started' | 'active' | 'ended';
  probability: number;
  level: number;
  noiseFloor: number;
  speechMs: number;
  silenceMs: number;
}
export interface SpeechDetector {
  start(): Promise<void>;
  stop(): void;
  push(pcm: Int16Array): void;
}
export type VadFactory = (
  detection: (value: SpeechDetection) => void,
  error: (message: string) => void,
  diagnostic: (event: string, details: Record<string, unknown>) => void,
) => Promise<SpeechDetector>;

export interface RealtimeDuplexConfig {
  url: string;
  provider?: string;
  adapter?: RealtimeProvider;
  createVad?: VadFactory;
  voice?: string;
  tools?: Array<Record<string, unknown>>;
  replyLanguage?: string;
}

export interface RealtimeToolResult {
  jobId: string;
  turnId?: string;
  question: string;
  brief: RealtimeBrief;
  callId?: string;
}

export interface PlaybackUpdate {
  responseId: string;
  interrupted: boolean;
  playedMs: number;
}

export interface RealtimeDuplexCallbacks {
  onPlayback?: (update: PlaybackUpdate) => void;
  getVideoFrame: () => string | null;
  onAssistantText: (text: string, final: boolean, toolJobId?: string, responseId?: string) => void;
  onUserText: (text: string, final: boolean) => void;
  onState: (state: 'connecting' | 'listening' | 'speaking' | 'closed') => void;
  onError: (message: string) => void;
  onDiagnostic?: (event: Record<string, unknown>) => void;
  onToolResultDispatched?: (jobId: string) => void;
  onFunctionCall?: (call: RealtimeFunctionCall) => void;
}

const SEND_INTERVAL_MS = 200;
const USER_TURN_SILENCE_MS = 1_200;
const REALTIME_CLIENT_BUILD = 'realtime-provider-v1';
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

export class RealtimeDuplexSession {
  private socket: WebSocket | null = null;
  private cancelStartup: (() => void) | null = null;
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
  private pendingOperations: Array<{ callId: string; output: unknown }> = [];
  private pendingQuestions: Array<{ jobId: string; interaction: unknown }> = [];
  private pendingToolResults: RealtimeToolResult[] = [];
  private toolResultWaitKey = '';
  private acceptedToolResultIds = new Set<string>();
  private acceptedFunctionCallIds = new Set<string>();
  private pendingToolResponseJobId: string | null = null;
  private responseToolJobIds = new Map<string, string>();
  private assistantPlaying = false;
  private responseActive = false;
  private providerSpeechActive = false;
  private awaitingProviderResponse = false;
  private retryNotificationResponse = false;
  private operationResponse = false;
  private notificationConflicts = 0;
  private notificationConflictWaiting = false;
  private inputEpoch = 0;
  private toolAttempts = 0;
  private toolFailures = new Map<string, number>();
  private toolCalls = new Map<string, { epoch: number; signature: string; target: string }>();
  private rejectedToolCalls = new Set<string>();
  private pendingTextTurns: string[] = [];
  private pendingNotices: string[] = [];
  private latestInputId = '';
  private inputTexts = new Map<string, string>();
  private responseInputs = new Map<string, string>();
  private playbackOperation: Promise<void> = Promise.resolve();
  private playbackGeneration = 0;
  private queuedDrainResponseId: string | null = null;
  private vad: SpeechDetector | null = null;
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
  private readonly provider: RealtimeProvider;
  private clearSequence = 0;
  private pendingClears = new Map<
    number,
    { responseIds: string[]; generation: number; finalPlayed: Map<string, number>; timer: number | null }
  >();
  private playbackResponses = new Set<string>();
  private contextTruncations = new Set<string>();
  private truncationTimer: number | null = null;

  constructor(
    private readonly config: RealtimeDuplexConfig,
    private readonly callbacks: RealtimeDuplexCallbacks,
  ) {
    this.provider = config.adapter || createRealtimeProvider(config.provider);
  }

  async start(): Promise<void> {
    const lifecycle = ++this.lifecycle;
    try {
      await this.startMedia(lifecycle);
    } catch (error) {
      if (lifecycle === this.lifecycle) this.stop();
      throw error;
    }
  }

  private async startMedia(lifecycle: number): Promise<void> {
    this.callbacks.onState('connecting');
    if (this.provider.localVad) {
      this.emitDiagnostic('realtime_vad_loading', { model: 'silero-v5' });
      if (!this.config.createVad) throw new Error('Speech detector is unavailable');
      if (lifecycle !== this.lifecycle) return;
      const vad = await this.config.createVad(
        (detection) => this.handleSpeechDetection(detection),
        (message) => {
          this.userActivityActive = false;
          this.turnHasUserActivity = false;
          this.activeUserTurnId = null;
          this.emitDiagnostic('realtime_vad_error', { message });
          this.callbacks.onError(`本地人声检测暂不可用：${message}。`);
        },
        (event, details) => {
          if (event === 'realtime_vad_resync') {
            this.userActivityActive = false;
            this.turnHasUserActivity = false;
            this.activeUserTurnId = null;
          }
          this.emitDiagnostic(event, details);
        },
      );
      if (lifecycle !== this.lifecycle) {
        vad.stop();
        return;
      }
      this.vad = vad;
      try {
        await vad.start();
      } catch (error) {
        vad.stop();
        this.emitDiagnostic('realtime_vad_error', { message: String(error) });
        throw error;
      }
      if (lifecycle !== this.lifecycle) return;
      this.emitDiagnostic('realtime_vad_ready', { model: 'silero-v5' });
    }
    this.playbackContext = new AudioContext({
      sampleRate: this.provider.outputRate,
    });
    await this.playbackContext.audioWorklet.addModule(new URL('./audio/duplex-playback.js', import.meta.url));
    if (lifecycle !== this.lifecycle) return;
    this.playbackNode = new AudioWorkletNode(this.playbackContext, 'jiuwen-duplex-playback');
    this.playbackNode.port.onmessage = ({ data }) => {
      if (lifecycle !== this.lifecycle) return;
      if (data.type === 'overflow') {
        this.callbacks.onError('Realtime playback queue overflow');
        this.stop();
        return;
      }
      if (data.type === 'cleared') {
        const pending = this.pendingClears.get(data.clearId);
        if (!pending) return; // A stale acknowledgement must not clear a new turn.
        if (pending.timer !== null) window.clearTimeout(pending.timer);
        this.pendingClears.delete(data.clearId);
        for (const responseId of pending.responseIds) {
          // With a per-response ledger, a missing entry is not the cumulative
          // playedMs: the worklet may already have reported that reply as drained.
          const clearedMs =
            data.played != null
              ? data.played[responseId] || 0
              : data.responseId === responseId
                ? data.playedMs || 0
                : 0;
          const playedMs = Math.max(pending.finalPlayed.get(responseId) || 0, clearedMs);
          this.callbacks.onPlayback?.({
            responseId,
            interrupted: true,
            playedMs: playedMs || 0,
          });
          for (const event of this.provider.truncate(responseId, playedMs || 0)) {
            if (event.item_id) this.contextTruncations.add(String(event.item_id));
            this.send(event);
          }
        }
        if (this.contextTruncations.size && this.truncationTimer === null) {
          this.truncationTimer = window.setTimeout(() => {
            this.callbacks.onError('Realtime playback synchronization timed out');
            this.stop();
          }, 5000);
        }
        this.dispatchQueuedToolResult();
        return;
      }
      if (data.type !== 'drained') return;
      // Playback may finish before clear reaches the worklet, but its notification
      // may arrive after interruption on the model socket. Keep that final count
      // for the matching clear; an old generation must still not update UI state.
      for (const pending of this.pendingClears.values()) {
        if (pending.generation === data.generation && pending.responseIds.includes(data.responseId)) {
          pending.finalPlayed.set(
            data.responseId,
            Math.max(pending.finalPlayed.get(data.responseId) || 0, data.playedMs || 0),
          );
        }
      }
      if (data.generation !== this.playbackGeneration) return;
      if (data.responseId) {
        this.callbacks.onPlayback?.({
          responseId: data.responseId,
          interrupted: false,
          playedMs: data.playedMs || 0,
        });
        this.playbackResponses.delete(data.responseId);
        this.provider.played(data.responseId);
      }
      if (this.queuedDrainResponseId === data.responseId) this.queuedDrainResponseId = null;
      this.assistantPlaying = this.playbackResponses.size > 0;
      if (!this.responseActive && !this.assistantPlaying) this.callbacks.onState('listening');
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
    this.captureContext = new AudioContext({
      sampleRate: this.provider.inputRate,
    });
    await this.captureContext.audioWorklet.addModule(new URL('./audio/duplex-capture.js', import.meta.url));
    if (lifecycle !== this.lifecycle) return;
    const source = this.captureContext.createMediaStreamSource(this.microphone);
    this.captureNode = new AudioWorkletNode(this.captureContext, 'jiuwen-duplex-capture');
    this.captureNode.port.onmessage = ({ data }) => {
      const pcm = resample(
        new Int16Array(data),
        this.captureContext?.sampleRate || this.provider.inputRate,
        this.provider.inputRate,
      );
      this.pending.push(pcm);
      this.pendingSamples += pcm.length;
      const maxPending = this.provider.inputRate * 2;
      while (this.pendingSamples > maxPending && this.pending.length > 1) {
        this.pendingSamples -= this.pending.shift()?.length || 0;
      }
      this.vad?.push(resample(pcm, this.provider.inputRate, 16000));
    };
    const silent = this.captureContext.createGain();
    silent.gain.value = 0;
    source.connect(this.captureNode);
    this.captureNode.connect(silent).connect(this.captureContext.destination);
    await this.captureContext.resume();
    if (lifecycle !== this.lifecycle) return;

    this.emitDiagnostic('realtime_websocket_connecting', {
      url: this.config.url.split('?')[0],
    });
    await this.openSocket();
    if (lifecycle !== this.lifecycle) return;
    this.sendTimer = window.setInterval(() => this.flush(), SEND_INTERVAL_MS);
  }

  stop(): void {
    this.lifecycle += 1;
    this.cancelStartup?.();
    this.cancelStartup = null;
    this.vad?.stop();
    this.vad = null;
    if (this.sendTimer !== null) window.clearInterval(this.sendTimer);
    this.sendTimer = null;
    if (this.assistantTranscript) this.finishAssistantText();
    this.provider.finish().forEach((event) => this.send(event));
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
    this.pendingOperations = [];
    this.pendingQuestions = [];
    this.toolResultWaitKey = '';
    this.acceptedToolResultIds.clear();
    this.acceptedFunctionCallIds.clear();
    this.pendingToolResponseJobId = null;
    this.responseToolJobIds.clear();
    this.sessionReady = false;
    this.responseActive = false;
    this.providerSpeechActive = false;
    this.awaitingProviderResponse = false;
    this.retryNotificationResponse = false;
    this.operationResponse = false;
    this.notificationConflicts = 0;
    this.notificationConflictWaiting = false;
    this.pendingTextTurns = [];
    this.pendingNotices = [];
    this.latestInputId = '';
    this.inputTexts.clear();
    this.responseInputs.clear();
    this.toolCalls.clear();
    this.resetToolBudget();
    this.activeUserTurnId = null;
    this.userSpeechMs = 0;
    this.userSilenceMs = 0;
    this.userActivityActive = false;
    this.turnHasUserActivity = false;
    this.interruptedResponseIds.clear();
    this.provider.reset();
    for (const pending of this.pendingClears.values()) if (pending.timer !== null) window.clearTimeout(pending.timer);
    this.pendingClears.clear();
    if (this.truncationTimer !== null) window.clearTimeout(this.truncationTimer);
    this.truncationTimer = null;
    this.contextTruncations.clear();
    this.playbackResponses.clear();
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    this.callbacks.onState('closed');
  }

  async sendTextTurn(text: string, isFresh: () => boolean = () => true): Promise<boolean> {
    const normalized = text.trim();
    if (!normalized || !isFresh()) return false;
    this.resetToolBudget();
    this.pendingTextTurns.push(normalized);
    this.dispatchQueuedToolResult();
    this.emitDiagnostic('realtime_text_input_dispatched', { text: normalized });
    return true;
  }

  cancelToolTask(jobId: string, _callId: string): void {
    this.pendingQuestions = this.pendingQuestions.filter((item) => item.jobId !== jobId);
    this.pendingToolResults = this.pendingToolResults.filter((item) => item.jobId !== jobId);
    if (this.acceptedToolResultIds.has(jobId)) return;
    this.acceptedToolResultIds.add(jobId);
    this.pendingNotices.push(taskCancellationNotice(jobId));
    this.dispatchQueuedToolResult();
  }

  enqueueOperationResult(callId: string, output: unknown): void {
    const key = `operation:${callId}`;
    if (this.acceptedToolResultIds.has(key)) return;
    this.acceptedToolResultIds.add(key);
    const call = this.toolCalls.get(callId);
    this.toolCalls.delete(callId);
    const receipt = output as { state?: string; error?: string } | null;
    if (call?.epoch === this.inputEpoch && receipt?.state === 'rejected') {
      this.toolFailures.set(call.target, (this.toolFailures.get(call.target) || 0) + 1);
      this.rejectedToolCalls.add(call.signature);
    }
    this.pendingOperations.push({ callId, output });
    this.dispatchQueuedToolResult();
  }

  enqueueQuestion(jobId: string, interaction: { id?: string; request_id: string; state: string }): void {
    if (interaction.state !== 'pending') {
      this.pendingQuestions = this.pendingQuestions.filter((item) => item.jobId !== jobId);
      return;
    }
    const key = `question:${jobId}:${interaction.id || interaction.request_id}`;
    if (this.acceptedToolResultIds.has(key)) return;
    this.acceptedToolResultIds.add(key);
    this.pendingQuestions.push({ jobId, interaction });
    this.dispatchQueuedToolResult();
  }

  enqueueToolResult(toolResult: RealtimeToolResult): boolean {
    this.pendingQuestions = this.pendingQuestions.filter((item) => item.jobId !== toolResult.jobId);
    const jobId = toolResult.jobId.trim();
    const question = toolResult.question.trim();
    const summary = toolResult.brief.summary.trim().slice(0, 1600);
    const callId = toolResult.callId?.trim();
    if (!jobId || !question || !summary || this.acceptedToolResultIds.has(jobId)) return false;

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
      event: this.provider.diagnostic(event),
      client_time: new Date().toISOString(),
      client_build: REALTIME_CLIENT_BUILD,
      ...details,
    });
  }

  private resetToolBudget(): void {
    this.inputEpoch += 1;
    this.toolAttempts = 0;
    this.toolFailures.clear();
    this.rejectedToolCalls.clear();
  }

  private requestResponse(operation = false): void {
    this.responseActive = true;
    // A late response.done for the preceding response must not free this reservation.
    this.responseId = null;
    this.operationResponse = operation;
    this.send(this.provider.response(operation ? TASK_OPERATION_INSTRUCTIONS : undefined));
  }

  private openSocket(): Promise<void> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.config.url);
      const socket = new WebSocket(url);
      this.socket = socket;
      let initSent = false;
      let settled = false;
      let reportedStartupError = false;
      let lastServiceError = '';
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
        this.cancelStartup = null;
        resolve();
      };
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(initTimeout);
        this.cancelStartup = null;
        reject(error);
      };
      this.cancelStartup = () => rejectOnce(new Error('Realtime connection cancelled'));
      const sendInit = async () => {
        if (initSent) return;
        initSent = true;
        this.send(this.provider.configure(this.config));
      };
      socket.onopen = () => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_open', {
          url: url.origin + url.pathname,
        });
        void sendInit();
        // Match the official client: microphone upload begins as soon as the
        // native duplex socket is open instead of waiting for session.created.
        if (this.provider.readyOnOpen) resolveOnce();
      };
      socket.onmessage = ({ data }) => {
        if (this.socket !== socket) return;
        if (typeof data !== 'string') return;
        try {
          const raw = JSON.parse(data) as WireEvent;
          for (const event of this.provider.normalize(raw)) {
            const type = String(event.type || '');
            if (type === 'error') {
              const error = event.error as Record<string, unknown> | undefined;
              // The gateway's close notice must not erase the provider's quota/auth error.
              if (lastServiceError && String(error?.code || '').endsWith('_gateway_upstream_closed')) return;
              lastServiceError = readableError(event.error || event);
            }
            const response = event.response as Record<string, unknown> | undefined;
            if (type === 'response_finished' && response?.status === 'failed') {
              const details = response.status_details as Record<string, unknown> | undefined;
              lastServiceError = readableError(
                details?.error || details || 'Realtime 模型响应失败，服务端未提供具体原因',
              );
            }
            if (type === 'session_closed') {
              if (!this.sessionReady) reportedStartupError = true;
              const closeReason = readableError(
                event.error || event.reason || lastServiceError || '远端已结束会话，未提供具体原因',
              );
              const startupAlreadyResolved = settled;
              this.emitDiagnostic('realtime_websocket_error', {
                url: url.origin + url.pathname,
                message: closeReason,
              });
              const message = `${this.sessionReady ? 'Realtime 会话已结束' : 'Realtime 会话初始化失败'}：${closeReason}`;
              rejectOnce(new Error(message));
              if (startupAlreadyResolved) this.callbacks.onError(message);
              this.stop();
              return;
            }
            if (type === 'queue_ready') {
              void sendInit();
              return;
            }
            if (type === 'session_ready' || type === 'session_created') {
              if (type !== 'session_created' || this.provider.readyOnOpen) {
                this.sessionReady = true;
                resolveOnce();
              }
              this.emitDiagnostic('realtime_session_ready', {});
            }
            if (type === 'error' && !this.sessionReady) {
              reportedStartupError = true;
              rejectOnce(new Error(readableError(event.error || event)));
            }
            this.processEvent(event, data);
          }
        } catch {
          this.callbacks.onError('Realtime 返回了无效事件');
        }
      };
      socket.onerror = () => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_error', {
          url: url.origin + url.pathname,
        });
        const message = lastServiceError || 'Realtime 网络连接异常，浏览器未提供具体原因，请检查网络或模型服务。';
        const startupAlreadyResolved = settled;
        rejectOnce(new Error(message));
        if (startupAlreadyResolved) this.callbacks.onError(message);
      };
      socket.onclose = ({ code, reason }) => {
        if (this.socket !== socket) return;
        if (this.assistantTranscript) this.finishAssistantText();
        this.emitDiagnostic('realtime_websocket_closed', {
          code,
          message: reason,
        });
        if (!this.sessionReady && !reportedStartupError) {
          const closeReason =
            lastServiceError || reason || (code === 1000 ? '远端在初始化阶段主动关闭' : `关闭代码 ${code}`);
          const startupAlreadyResolved = settled;
          rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
          if (startupAlreadyResolved) this.callbacks.onError(`Realtime 会话初始化失败：${closeReason}`);
        } else if (this.sessionReady) {
          this.callbacks.onError(
            lastServiceError ||
              `语音连接已断开（${code}：${reason || '服务端未提供具体原因，请检查网络或模型服务。'}），麦克风已停止，请重新开启 Full-duplex。后台任务继续执行。`,
          );
        }
        // Release media on remote disconnect as well. Conversation-owned work continues outside this session.
        this.stop();
      };
    });
  }

  private send(event: WireEvent): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }

  private flush(): void {
    if (this.pendingSamples === 0) return;
    if ((this.socket?.bufferedAmount || 0) > 2 * 1024 * 1024) {
      this.callbacks.onError('Realtime network send queue overflow');
      this.stop();
      return;
    }
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
      this.emitDiagnostic('realtime_vad_candidate_rejected', {
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
    this.interruptResponse(this.activeUserTurnId, detection.speechMs, detection.level, 80);
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
    const batch = this.provider.audio(audio, includeVideo ? this.callbacks.getVideoFrame() : null);
    batch.events.forEach((event) => this.send(event));
    batch.diagnostics.forEach(({ event, details }) => this.emitDiagnostic(event, details));
  }

  private dispatchQueuedToolResult(): void {
    if (
      !this.pendingToolResults.length &&
      !this.pendingOperations.length &&
      !this.pendingQuestions.length &&
      !this.pendingTextTurns.length &&
      !this.retryNotificationResponse &&
      !this.pendingNotices.length
    ) {
      this.toolResultWaitKey = '';
      return;
    }
    const reason =
      this.socket?.readyState !== WebSocket.OPEN || !this.sessionReady
        ? 'connection_not_ready'
        : this.userActivityActive ||
            this.turnHasUserActivity ||
            this.providerSpeechActive ||
            this.awaitingProviderResponse
          ? 'user_speaking'
          : (this.provider.truncatePlayback && this.pendingClears.size > 0) || this.contextTruncations.size > 0
            ? 'playback_synchronizing'
            : this.responseActive
              ? 'response_generating'
              : '';
    if (reason) {
      const jobId =
        this.pendingToolResults[0]?.jobId ||
        this.pendingOperations[0]?.callId ||
        this.pendingQuestions[0]?.jobId ||
        'conversation';
      const waitKey = `${jobId}:${reason}`;
      if (waitKey !== this.toolResultWaitKey) {
        this.toolResultWaitKey = waitKey;
        this.emitDiagnostic('realtime_tool_result_waiting', {
          job_id: jobId,
          reason,
          queued_count: this.pendingToolResults.length,
        });
      }
      return;
    }
    this.toolResultWaitKey = '';
    if (this.retryNotificationResponse) {
      this.retryNotificationResponse = false;
      this.requestResponse(this.operationResponse);
      return;
    }
    const textTurn = this.pendingTextTurns.shift();
    if (textTurn) {
      this.latestInputId = this.newTurnId('text');
      this.inputTexts.set(this.latestInputId, textTurn);
      this.send(this.provider.text(textTurn));
      this.requestResponse();
      return;
    }
    const notice = this.pendingNotices.shift();
    if (notice) {
      this.notificationConflicts = 0;
      this.send(this.provider.text(notice));
      this.requestResponse();
      return;
    }
    const question = this.pendingQuestions.shift();
    if (question) {
      this.notificationConflicts = 0;
      this.send(this.provider.text(taskQuestionNotice(question)));
      this.requestResponse();
      return;
    }
    const operation = this.pendingOperations.shift();
    if (operation) {
      this.notificationConflicts = 0;
      this.send(this.provider.toolOutput(operation.callId, JSON.stringify(operation.output)));
      this.requestResponse(true);
      return;
    }
    while (this.pendingToolResults.length > 0) {
      const toolResult = this.pendingToolResults.shift();
      if (!toolResult) return;
      this.emitDiagnostic('search_result_dispatched', {
        job_id: toolResult.jobId,
        turn_id: toolResult.turnId,
      });
      this.callbacks.onToolResultDispatched?.(toolResult.jobId);

      this.pendingToolResponseJobId = toolResult.jobId;
      this.notificationConflicts = 0;
      this.responseActive = true;
      this.responseId = null;
      if (toolResult.callId && !this.acceptedToolResultIds.has(`operation:${toolResult.callId}`)) {
        this.send(
          this.provider.toolOutput(
            toolResult.callId,
            JSON.stringify({
              ...toolResult.brief,
              task_context: {
                job_id: toolResult.jobId,
                turn_id: toolResult.turnId,
                original_question: toolResult.question.slice(0, 1000),
              },
            }),
          ),
        );
      }
      this.send(this.provider.text(taskResultNotice(toolResult.brief, toolResult.question, this.config.replyLanguage)));
      this.send(this.provider.response());
      this.emitDiagnostic('realtime_tool_result_returned', {
        job_id: toolResult.jobId,
        turn_id: toolResult.turnId,
        call_id: toolResult.callId,
        question: toolResult.question,
      });
      return;
    }
  }

  handleEvent(event: WireEvent, rawEvent = ''): void {
    for (const normalized of this.provider.normalize(event)) this.processEvent(normalized, rawEvent);
  }

  private processEvent(event: ProviderEvent, rawEvent = ''): void {
    const type = String(event.type || '');
    const response = event.response as Record<string, unknown> | undefined;
    const eventResponseId = String(event.response_id || response?.id || '') || null;
    if (
      eventResponseId &&
      this.interruptedResponseIds.has(eventResponseId) &&
      eventResponseId !== this.responseId &&
      ['text_delta', 'text_done', 'transcript_delta', 'transcript_done'].includes(type)
    )
      return;
    if (type === 'tool_call' && eventResponseId && this.interruptedResponseIds.has(eventResponseId)) {
      this.emitDiagnostic('realtime_tool_call_stale', {
        response_id: eventResponseId,
        call_id: String(event.call_id || ''),
      });
      return; // Cancellation may end a function call with incomplete JSON.
    }
    if (type === 'unheard_audio' && eventResponseId) {
      for (const message of this.provider.truncate(eventResponseId, 0)) {
        if (message.item_id) this.contextTruncations.add(String(message.item_id));
        this.send(message);
      }
      if (this.contextTruncations.size && this.truncationTimer === null) {
        this.truncationTimer = window.setTimeout(() => {
          this.callbacks.onError('Realtime playback synchronization timed out');
          this.stop();
        }, 5000);
      }
      return;
    }
    const functionCall = parseTaskCall(event as unknown as Record<string, unknown>);
    if (functionCall) {
      if (!this.acceptedFunctionCallIds.has(functionCall.callId)) {
        this.acceptedFunctionCallIds.add(functionCall.callId);

        this.emitDiagnostic('realtime_tool_call_received', {
          name: functionCall.name,
          call_id: functionCall.callId,
          task: functionCall.task,
        });
        const args = JSON.parse(functionCall.arguments) as Record<string, unknown>;
        const signature = JSON.stringify([
          functionCall.name,
          Object.keys(args)
            .sort()
            .map((key) => [key, args[key]]),
        ]);
        const target = JSON.stringify([functionCall.name, args.job_id || '', args.interaction_id || '']);
        if (
          (this.toolFailures.get(target) || 0) >= 3 ||
          this.toolAttempts >= 16 ||
          this.rejectedToolCalls.has(signature)
        ) {
          const message = '本次操作未完成：已停止重复或超限尝试，请确认任务和要求后再试。';
          this.send(
            this.provider.toolOutput(
              functionCall.callId,
              JSON.stringify({
                state: 'rejected',
                retryable: false,
                error: message,
              }),
            ),
          );
          this.emitDiagnostic('realtime_tool_retry_stopped', {
            name: functionCall.name,
            call_id: functionCall.callId,
            attempt: this.toolAttempts,
          });
          this.callbacks.onError(message);
          // No response.create: a rejected-call response must not recursively drive more calls.
          return;
        }
        this.toolAttempts += 1;
        this.toolCalls.set(functionCall.callId, {
          epoch: this.inputEpoch,
          signature,
          target,
        });
        const inputId = this.responseInputs.get(eventResponseId || this.responseId || '');
        this.callbacks.onFunctionCall?.(
          inputId
            ? {
                ...functionCall,
                inputId,
                originalInstruction: this.inputTexts.get(inputId),
              }
            : functionCall,
        );
      }
    } else if (type === 'tool_call') {
      this.emitDiagnostic('realtime_tool_call_invalid', {
        name: String(event.name || ''),
        call_id: String(event.call_id || ''),
        arguments: typeof event.arguments === 'string' ? event.arguments.slice(0, 2_000) : event.arguments,
      });
      this.callbacks.onError('Realtime 返回了无效的工具调用');
    } else if (type === 'session_ready' || (type === 'session_created' && this.provider.readyOnOpen)) {
      this.callbacks.onState('listening');
    } else if (type === 'response_started') {
      this.awaitingProviderResponse = false;
      // Some providers start the tool-call response before ending its spoken acknowledgement.
      if (this.assistantTranscript) this.finishAssistantText();
      this.responseId = eventResponseId || this.newTurnId('response');
      this.responseInputs.set(this.responseId, this.latestInputId);
      if (this.responseInputs.size > 64) this.responseInputs.delete(this.responseInputs.keys().next().value!);
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
    } else if (type === 'text_delta') {
      this.beginOfficialTurn(eventResponseId);
      const delta = String(event.delta || '');
      this.assistantTranscript += delta;
      this.callbacks.onAssistantText(
        this.assistantTranscript,
        false,
        this.toolJobIdForResponse(eventResponseId),
        eventResponseId || this.responseId || undefined,
      );
    } else if (type === 'text_done') {
      this.assistantTranscript = String(event.text || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'response_cancelled') {
      this.emitDiagnostic('realtime_response_cancelled', {
        response_id: eventResponseId,
        source: eventResponseId && this.interruptedResponseIds.has(eventResponseId) ? 'local_vad' : 'provider',
        raw_event: rawEvent || JSON.stringify(event),
      });
      if (!eventResponseId || eventResponseId === this.responseId) {
        if (this.assistantTranscript) this.finishAssistantText();
        this.responseActive = false;
        if (!eventResponseId || !this.interruptedResponseIds.has(eventResponseId))
          this.clearPlayback(eventResponseId || this.responseId);
        this.callbacks.onState('listening');
      }
    } else if (type === 'audio_chunk') {
      const encoded = String(event.delta || '');
      if (!encoded || !this.playbackNode) return;
      const responseId = eventResponseId || this.responseId;
      this.enqueueAudioDelta(event, encoded, responseId);
    } else if (type === 'audio_finished' || type === 'response_finished') {
      const affectsActive = !eventResponseId || eventResponseId === this.responseId || this.notificationConflictWaiting;
      if (type === 'response_finished' && response?.status === 'failed') {
        const details = response.status_details as Record<string, unknown> | undefined;
        this.callbacks.onError(
          readableError(details?.error || details || 'Realtime 模型响应失败，服务端未提供具体原因'),
        );
      }
      if (type === 'response_finished' && affectsActive) {
        if (this.assistantTranscript) this.finishAssistantText();
        this.responseActive = false;
        this.notificationConflictWaiting = false;
        this.emitDiagnostic('realtime_response_finished', {
          response_id: eventResponseId,
          reason: String(response?.status || ''),
        });
      }
      if (affectsActive && (type === 'response_finished' || !this.provider.truncatePlayback))
        this.enqueuePlaybackDrain(eventResponseId || this.responseId);
      if (type === 'response_finished' && affectsActive) this.dispatchQueuedToolResult();
    } else if (type === 'transcript_delta') {
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
    } else if (type === 'transcript_done') {
      this.assistantTranscript = String(event.transcript || this.assistantTranscript);
      this.finishAssistantText();
    } else if (type === 'speech_started' || type === 'speech_stopped') {
      this.providerSpeechActive = type === 'speech_started';
      if (!this.provider.localVad) {
        this.userActivityActive = this.providerSpeechActive;
        this.turnHasUserActivity = this.providerSpeechActive;
        if (this.providerSpeechActive) {
          this.activeUserTurnId = String(event.item_id || this.newTurnId('voice'));
          this.interruptResponse(this.activeUserTurnId, 0, 0, 0, true);
        }
      }
      if (this.providerSpeechActive) this.latestInputId = String(event.item_id || this.newTurnId('audio'));
      this.awaitingProviderResponse = true;
      this.emitDiagnostic('realtime_provider_vad', {
        source: type,
        raw_event: rawEvent || JSON.stringify(event),
      });
    } else if (type === 'input_transcript_delta') {
      this.callbacks.onUserText(`${String(event.delta || event.text || '')}${String(event.stash || '')}`, false);
    } else if (type === 'input_transcript_done') {
      const transcript = String(event.transcript || '');
      const inputId = String(event.item_id || this.latestInputId);
      if (inputId && transcript.trim()) this.inputTexts.set(inputId, transcript.trim());
      if (this.inputTexts.size > 64) this.inputTexts.delete(this.inputTexts.keys().next().value!);
      if (transcript.trim()) this.resetToolBudget();
      this.emitDiagnostic('realtime_native_asr_completed', {
        transcript,
        has_transcript: Boolean(transcript.trim()),
      });
      this.callbacks.onUserText(transcript, true);
    } else if (type === 'context_truncated') {
      if (event.item_id) this.contextTruncations.delete(event.item_id);
      if (!this.contextTruncations.size && this.truncationTimer !== null) {
        window.clearTimeout(this.truncationTimer);
        this.truncationTimer = null;
      }
      this.dispatchQueuedToolResult();
    } else if (type === 'error') {
      if (this.assistantTranscript) this.finishAssistantText();
      const media = this.provider.snapshot();
      const error = event.error;
      const errorRecord = error && typeof error === 'object' ? (error as Record<string, unknown>) : {};
      if (event.errorCategory === 'response_busy') {
        // The conversation items were already submitted. Retry only response creation,
        // after the provider's active response ends; never replay a tool or its output.
        this.responseActive = true;
        this.notificationConflictWaiting = true;
        this.retryNotificationResponse = ++this.notificationConflicts <= 3;
        if (!this.retryNotificationResponse) this.callbacks.onError('结果播报响应冲突，已停止重试。请查看任务结果。');
      }
      this.emitDiagnostic('realtime_realtime_error', {
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

  protected interruptResponse(
    turnId: string,
    speechMs: number,
    level: number,
    threshold: number,
    serverCancelled = false,
  ): boolean {
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
    if (cancelEventSent && !serverCancelled) this.provider.cancel(this.responseId).forEach((event) => this.send(event));
    this.clearPlayback(interruptedResponseId);
    // response.cancel is a request, not confirmation that the provider is idle.
    this.assistantPlaying = false;
    if (this.assistantTranscript) this.finishAssistantText();
    this.emitDiagnostic('realtime_response_interrupted_by_user', {
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

  private clearPlayback(responseId: string | null): void {
    const generation = this.playbackGeneration;
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    const ids = new Set(this.playbackResponses);
    if (responseId) ids.add(responseId);
    for (const id of ids) this.interruptedResponseIds.add(id);
    this.playbackResponses.clear();
    const clearId = ++this.clearSequence;
    if (this.playbackNode) {
      const timer = this.provider.truncatePlayback
        ? window.setTimeout(() => {
            this.callbacks.onError('Realtime playback synchronization timed out');
            this.stop();
          }, 2000)
        : null;
      this.pendingClears.set(clearId, { responseIds: [...ids], generation, finalPlayed: new Map(), timer });
    }
    this.playbackNode?.port.postMessage({
      type: 'clear',
      clearId,
      cancelResponse: false,
    });
    this.assistantPlaying = false;
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
    if (!this.provider.localVad) return this.providerSpeechActive;
    return this.userActivityActive && this.userSilenceMs < 160 && performance.now() - this.lastSpeechDetectionAt < 500;
  }

  private interruptWhileUserSpeaking(): boolean {
    if (!this.userIsSpeakingNow() || !this.activeUserTurnId) return false;
    return this.interruptResponse(this.activeUserTurnId, this.userSpeechMs, this.lastSpeechLevel, 80);
  }

  private enqueueAudioDelta(event: ProviderEvent, encoded: string, responseId: string | null): void {
    if (responseId && this.interruptedResponseIds.has(responseId)) return;
    if (this.userIsSpeakingNow()) {
      this.interruptWhileUserSpeaking();
      if (responseId) this.interruptedResponseIds.add(responseId);
      this.emitDiagnostic('realtime_audio_blocked_during_speech', {
        response_id: responseId,
      });
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
          this.emitDiagnostic('realtime_audio_blocked_during_speech', {
            response_id: responseId,
          });
          return;
        }
        this.assistantPlaying = true;
        if (responseId) this.playbackResponses.add(responseId);
        const playbackPcm = resample(
          output,
          this.provider.outputRate,
          this.playbackContext?.sampleRate || this.provider.outputRate,
        );
        this.playbackNode.port.postMessage(
          {
            type: 'audio',
            lane: 'normal',
            pcm: playbackPcm.buffer,
            responseId,
            initialBufferMs: INITIAL_PLAYBACK_BUFFER_MS,
            generation,
          },
          [playbackPcm.buffer],
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
        generation,
        lane: 'normal',
        responseId,
      });
    });
  }

  private async decodeOutputAudio(_event: ProviderEvent, encoded: string): Promise<Int16Array> {
    const bytes = base64ToBytes(encoded);
    return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
  }

  private newTurnId(prefix: string): string {
    this.turnSequence += 1;
    return prefix + '-' + Date.now() + '-' + this.turnSequence;
  }
}
