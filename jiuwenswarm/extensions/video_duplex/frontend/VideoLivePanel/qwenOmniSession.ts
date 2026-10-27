import {
  APPROVAL_SOURCES,
  pendingApprovalInstructions,
  pendingCardTurnNotice,
  taskCancellationNotice,
  taskQuestionNotice,
} from './taskPrompts.js';
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
import { attachDuplexAudioMeter } from '../duplexAudioMeter';
import type { RealtimeBrief } from './types.js';
import { createQwenOmniOperationResponseEvent, createQwenOmniToolOutputEvent } from './qwenOmniTools.js';
import type { SileroVad, SpeechDetection } from '../../../../channels/web/frontend/src/utils/speechDetection/sileroVad';

export interface RealtimeDuplexConfig {
  restoreInstructions?: string;
  url: string;
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

export interface RealtimeDuplexCallbacks {
  getVideoFrame: () => string | null;
  onAssistantText: (text: string, final: boolean, toolJobId?: string, responseId?: string) => void;
  onUserText: (text: string, final: boolean) => void;
  onState: (state: 'connecting' | 'listening' | 'speaking' | 'closed') => void;
  onError: (message: string) => void;
  onIdleTimeout?: () => void;
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
  private playbackGain: GainNode | null = null;
  private detachMeters: Array<() => void> = [];
  private microphoneMuted = false;
  private speakerMuted = false;
  private pending: Int16Array[] = [];
  private pendingSamples = 0;
  private sendTimer: number | null = null;
  private sessionReady = false;
  private cancelSocketStartup: (() => void) | null = null;
  private responseId: string | null = null;
  private pendingOperations: Array<{ callId: string; output: unknown; callEpoch: number | null }> = [];
  private pendingQuestions: Array<{ jobId: string; interaction: unknown }> = [];
  // Open approval cards (job:interaction -> header), kept in the session instructions.
  private pendingApprovals = new Map<string, string>();
  // A user turn spoken while a card is open: the provider's automatic reply is cancelled and
  // re-requested after the card's status, so that status is the last thing the model reads.
  private cardTurn: { at: number; cancelling: boolean } | null = null;
  // The provider's automatic reply to the user's latest speech: when that speech
  // ended (until the reply is created), then the reply's ID while it plays.
  private speechReplyPendingAt: number | null = null;
  private speechReplyId: string | null = null;
  private pendingToolResults: RealtimeToolResult[] = [];
  private toolResultWaitKey = '';
  private acceptedToolResultIds = new Set<string>();
  private acceptedFunctionCallIds = new Set<string>();
  private pendingToolResponseJobId: string | null = null;
  private responseToolJobIds = new Map<string, string>();
  private assistantPlaying = false;
  private responseActive = false;
  private pendingResponseInterrupt = false;
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
  private heldOutput = new Map<string, {
    text: string;
    transcript: string;
    textItemId?: string;
    transcriptItemId?: string;
    streamedText?: string;
    audio: Array<{ event: Record<string, unknown>; encoded: string }>;
  }>();
  private silentToolResponses = new Set<string>();
  private messageOutputItems = new Map<string, Set<string>>();
  private finishedOutputResponses = new Set<string>();
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
    this.playbackGain = this.playbackContext.createGain();
    this.playbackGain.gain.value = this.speakerMuted ? 0 : 1;
    this.playbackNode.connect(this.playbackGain).connect(this.playbackContext.destination);
    this.detachMeters.push(attachDuplexAudioMeter('output', this.playbackContext, this.playbackNode));
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
    this.detachMeters.push(attachDuplexAudioMeter('input', this.captureContext, source));
    this.captureNode = new AudioWorkletNode(this.captureContext, 'jiuwen-duplex-capture');
    this.captureNode.port.onmessage = ({ data }) => {
      if (this.config.restoreInstructions && !this.sessionReady) return;
      const input = new Int16Array(data);
      if (this.microphoneMuted) input.fill(0);
      const pcm = resample(input, this.captureContext?.sampleRate || INPUT_RATE, INPUT_RATE);
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

  setMicrophoneMuted(muted: boolean): void {
    this.microphoneMuted = muted;
    this.microphone?.getAudioTracks().forEach((track) => { track.enabled = !muted; });
    if (muted) this.pending.forEach((chunk) => chunk.fill(0));
  }

  setSpeakerMuted(muted: boolean): void {
    this.speakerMuted = muted;
    if (this.playbackGain) this.playbackGain.gain.value = muted ? 0 : 1;
  }

  stop(): void {
    this.lifecycle += 1;
    this.cancelSocketStartup?.();
    this.vad?.stop();
    this.vad = null;
    if (this.sendTimer !== null) window.clearInterval(this.sendTimer);
    this.sendTimer = null;
    this.send(createQwenOmniFinishSessionEvent());
    this.socket?.close(1000, 'client stop');
    this.socket = null;
    this.detachMeters.forEach((detach) => detach());
    this.detachMeters = [];
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
    this.pendingApprovals.clear();
    this.cardTurn = null;
    this.speechReplyPendingAt = null;
    this.speechReplyId = null;
    this.toolResultWaitKey = '';
    this.acceptedToolResultIds.clear();
    this.acceptedFunctionCallIds.clear();
    this.pendingToolResponseJobId = null;
    this.responseToolJobIds.clear();
    this.sessionReady = false;
    this.responseActive = false;
    this.pendingResponseInterrupt = false;
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
    for (const responseId of this.heldOutput.keys()) this.closeStreamedText(responseId);
    this.heldOutput.clear();
    this.silentToolResponses.clear();
    this.messageOutputItems.clear();
    this.finishedOutputResponses.clear();
    this.qwenMedia.reset();
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
    this.emitDiagnostic('qwen_text_input_dispatched', { text: normalized });
    return true;
  }

  cancelToolTask(jobId: string, callId: string): void {
    this.pendingQuestions = this.pendingQuestions.filter(item => item.jobId !== jobId);
    this.closeApprovals(jobId);
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
    this.pendingOperations.push({ callId, output, callEpoch: call?.epoch ?? null });
    this.dispatchQueuedToolResult();
  }

  enqueueQuestion(
    jobId: string,
    interaction: { id?: string; request_id: string; state: string; source?: string; questions?: Array<{ header?: string }> },
  ): void {
    if (interaction.state !== 'pending') {
      this.pendingQuestions = this.pendingQuestions.filter(item => item.jobId !== jobId);
      this.closeApprovals(jobId);
      return;
    }
    const key = `question:${jobId}:${interaction.id || interaction.request_id}`;
    if (interaction.source && APPROVAL_SOURCES.has(interaction.source) && !this.pendingApprovals.has(key)) {
      this.pendingApprovals.set(key, interaction.questions?.[0]?.header || interaction.source);
      this.syncApprovalInstructions();
      this.redirectSpeechReply();
    }
    if (this.acceptedToolResultIds.has(key)) return;
    this.acceptedToolResultIds.add(key);
    this.pendingQuestions.push({ jobId, interaction });
    this.dispatchQueuedToolResult();
  }

  private closeApprovals(jobId: string): void {
    const prefix = `question:${jobId}:`;
    const keys = [...this.pendingApprovals.keys()].filter(key => key.startsWith(prefix));
    if (!keys.length) return;
    keys.forEach(key => this.pendingApprovals.delete(key));
    this.syncApprovalInstructions();
  }

  private sessionUpdate(): Record<string, unknown> {
    return createQwenOmniSessionUpdate({
      voice: this.config.voice,
      tools: this.config.tools,
      replyLanguage: this.config.replyLanguage,
      restoreInstructions: this.config.restoreInstructions,
      extraInstructions: pendingApprovalInstructions([...this.pendingApprovals.values()]),
      inputRate: INPUT_RATE,
      outputRate: OUTPUT_RATE,
    });
  }

  private cancelForCardTurn(): void {
    if (!this.cardTurn) return;
    this.cardTurn.cancelling = true;
    this.interruptQwenResponse(this.activeUserTurnId || 'card-turn', 0, 0, 0);
  }

  // A card that opens while the model answers speech finished just before it: that
  // reply was formed without the card ("已收到您的确认…继续执行"), so it is
  // redirected like any card turn. Notices and tool results are never speech replies.
  private redirectSpeechReply(): void {
    if (this.cardTurn) return;
    if (this.speechReplyPendingAt !== null && performance.now() - this.speechReplyPendingAt < 8_000) {
      this.cardTurn = { at: this.speechReplyPendingAt, cancelling: false };
    } else if (this.responseActive && this.speechReplyId && this.speechReplyId === this.responseId) {
      this.cardTurn = { at: performance.now(), cancelling: false };
      this.cancelForCardTurn();
    }
  }

  private reissueCardTurn(): void {
    if (!this.cardTurn?.cancelling) return;
    this.cardTurn = null;
    // Hold queued notices until this reply is created, so they cannot collide with it.
    this.awaitingProviderResponse = true;
    this.send({ type: 'conversation.item.create', item: {
      type: 'message', role: 'user',
      content: [{ type: 'input_text', text: pendingCardTurnNotice([...this.pendingApprovals.values()]) }],
    } });
    this.send({ type: 'response.create' });
    this.emitDiagnostic('qwen_card_turn_reissued', { pending: this.pendingApprovals.size });
  }

  private syncApprovalInstructions(): void {
    // Before the session is ready, the initial session.update already carries them.
    if (this.socket?.readyState !== WebSocket.OPEN || !this.sessionReady) return;
    this.send(this.sessionUpdate());
    this.emitDiagnostic('qwen_approval_instructions_synced', { pending: this.pendingApprovals.size });
  }

  enqueueToolResult(toolResult: RealtimeToolResult): boolean {
    this.pendingQuestions = this.pendingQuestions.filter(item => item.jobId !== toolResult.jobId);
    this.closeApprovals(toolResult.jobId);
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
      event,
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
    this.send(operation ? createQwenOmniOperationResponseEvent() : { type: 'response.create' });
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
        this.cancelSocketStartup = null;
        window.clearTimeout(initTimeout);
        resolve();
      };
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        this.cancelSocketStartup = null;
        window.clearTimeout(initTimeout);
        reject(error);
      };
      this.cancelSocketStartup = () => rejectOnce(new Error('Realtime startup cancelled'));
      const sendInit = async () => {
        if (initSent) return;
        initSent = true;
        this.send(this.sessionUpdate());
      };
      socket.onopen = () => {
        this.emitDiagnostic('realtime_websocket_open', { url: url.toString() });
        void sendInit();
        // Match the official client: microphone upload begins as soon as the
        // native duplex socket is open instead of waiting for session.created.
        if (!this.config.restoreInstructions) resolveOnce();
      };
      socket.onmessage = ({ data }) => {
        if (this.socket !== socket) return;
        if (typeof data !== 'string') return;
        try {
          const event = JSON.parse(data) as Record<string, unknown>;
          const type = String(event.type || '');
          if (type === 'error') {
            const error = event.error as Record<string, unknown> | undefined;
            // The gateway's close notice must not erase the provider's quota/auth error.
            if (lastServiceError && error?.code === 'qwen_gateway_upstream_closed') return;
            lastServiceError = readableError(event.error || event);
          }
          const response = event.response as Record<string, unknown> | undefined;
          if (type === 'response.done' && response?.status === 'failed') {
            const details = response.status_details as Record<string, unknown> | undefined;
            lastServiceError = readableError(details?.error || details || 'Realtime 模型响应失败，服务端未提供具体原因');
          }
          if (type === 'session.closed') {
            if (!this.sessionReady) reportedStartupError = true;
            const closeReason = readableError(event.error || event.reason || lastServiceError || '远端已结束会话，未提供具体原因');
            const startupAlreadyResolved = settled;
            this.emitDiagnostic('realtime_websocket_error', {
              url: url.toString(),
              message: closeReason,
            });
            const message = `${this.sessionReady ? 'Realtime 会话已结束' : 'Realtime 会话初始化失败'}：${closeReason}`;
            rejectOnce(new Error(message));
            if (startupAlreadyResolved) this.callbacks.onError(message);
            this.stop();
            return;
          }
          if (type === 'session.queue_done' || type === 'queue_done') {
            void sendInit();
            return;
          }
          if (type === 'session.updated' && this.sessionReady) return; // Instruction refresh, not startup.
          if (type === 'session.updated' || type === 'session.created') {
            if (this.config.restoreInstructions && type !== 'session.updated') return;
            this.sessionReady = true;
            resolveOnce();
            if (this.config.restoreInstructions) this.callbacks.onState('listening');
            this.emitDiagnostic('realtime_session_ready', {});
          }
          if (type === 'error' && !this.sessionReady) {
            reportedStartupError = true;
            rejectOnce(new Error(readableError(event.error || event)));
          }
          if (this.config.restoreInstructions && !this.sessionReady) return;
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
        const message = lastServiceError || 'Realtime 网络连接异常，浏览器未提供具体原因，请检查网络或模型服务。';
        const startupAlreadyResolved = settled;
        rejectOnce(new Error(message));
        if (startupAlreadyResolved) this.callbacks.onError(message);
      };
      socket.onclose = ({ code, reason }) => {
        if (this.socket !== socket) return;
        this.emitDiagnostic('realtime_websocket_closed', {
          code,
          message: reason,
        });
        if (!this.sessionReady && !reportedStartupError) {
          const closeReason = lastServiceError || reason || (code === 1000 ? '远端在初始化阶段主动关闭' : `关闭代码 ${code}`);
          const startupAlreadyResolved = settled;
          rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
          if (startupAlreadyResolved) this.callbacks.onError(`Realtime 会话初始化失败：${closeReason}`);
        } else if (this.sessionReady) {
          this.callbacks.onError(lastServiceError || `语音连接已断开（${code}：${reason || '服务端未提供具体原因，请检查网络或模型服务。'}），麦克风已停止，请重新开启 Full-duplex。后台任务继续执行。`);
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
    if (!this.pendingToolResults.length && !this.pendingOperations.length && !this.pendingQuestions.length && !this.pendingTextTurns.length && !this.retryNotificationResponse && !this.pendingNotices.length) {
      this.toolResultWaitKey = '';
      return;
    }
    const reason =
      this.socket?.readyState !== WebSocket.OPEN || !this.sessionReady
        ? 'connection_not_ready'
        : this.userActivityActive || this.turnHasUserActivity || this.providerSpeechActive || this.awaitingProviderResponse
          ? 'user_speaking'
          : this.responseActive
            ? 'response_generating'
            : '';
    if (reason) {
      const jobId = this.pendingToolResults[0]?.jobId || this.pendingOperations[0]?.callId || this.pendingQuestions[0]?.jobId || 'conversation';
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
    if (this.retryNotificationResponse) {
      this.retryNotificationResponse = false;
      this.requestResponse(this.operationResponse);
      return;
    }
    const textTurn = this.pendingTextTurns.shift();
    if (textTurn) {
      this.latestInputId = this.newTurnId('text');
      this.inputTexts.set(this.latestInputId, textTurn);
      this.send(createQwenOmniTextTurnEvents(textTurn)[0]);
      this.requestResponse();
      return;
    }
    const notice = this.pendingNotices.shift();
    if (notice) {
      this.notificationConflicts = 0;
      this.send({type: 'conversation.item.create', item: {
        type: 'message', role: 'user', content: [{type: 'input_text', text: notice}],
      }});
      this.requestResponse();
      return;
    }
    const question = this.pendingQuestions.shift();
    if (question) {
      this.notificationConflicts = 0;
      this.send({ type: 'conversation.item.create', item: {
        type: 'message', role: 'user', content: [{ type: 'input_text', text:
          taskQuestionNotice(question),
        }],
      } });
      this.requestResponse();
      return;
    }
    const operation = this.pendingOperations.shift();
    if (operation) {
      this.notificationConflicts = 0;
      const lateOutput = operation.callEpoch !== null && operation.callEpoch < this.inputEpoch;
      this.emitDiagnostic('qwen_tool_output_sent', {
        call_id: operation.callId, response_id: this.responseId || '',
        response_active: this.responseActive, user_speech_active: this.userActivityActive,
        output_chars: JSON.stringify(operation.output).length,
        delivery: lateOutput ? 'message' : 'function_call_output',
      });
      if (lateOutput) {
        // A later user turn can retire the old function call before its receipt is sent.
        this.emitDiagnostic('qwen_late_tool_output_rerouted', { call_id: operation.callId });
        this.send({ type: 'conversation.item.create', item: {
          type: 'message', role: 'user', content: [{ type: 'input_text',
            text: `An earlier Jiuwen operation returned this receipt. `
              + `Report its state briefly without repeating it: ${JSON.stringify(operation.output)}`,
          }],
        } });
      } else {
        this.send(createQwenOmniToolOutputEvent(operation.callId, JSON.stringify(operation.output)));
      }
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
      const events = createQwenOmniToolResultEvents(toolResult.callId || '', toolResult.brief, {
        jobId: toolResult.jobId,
        turnId: toolResult.turnId,
        question: toolResult.question,
      }, this.config.replyLanguage);
      // Accepted delegation closed the call; completion is a separate notification.
      const callEpoch = toolResult.callId ? this.toolCalls.get(toolResult.callId)?.epoch : undefined;
      if (!toolResult.callId || this.acceptedToolResultIds.has(`operation:${toolResult.callId}`)
          || (callEpoch !== undefined && callEpoch < this.inputEpoch)) events.shift();
      events.forEach((event) => this.send(event));
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
    const outputResponseId = eventResponseId || this.responseId || '';
    if (type === 'conversation.item.created') {
      const item = event.item as Record<string, unknown> | undefined;
      if (item?.type === 'function_call_output') this.emitDiagnostic('qwen_tool_output_acknowledged', {
        call_id: String(item.call_id || ''), item_id: String(item.id || ''),
      });
    }
    // A message item may precede a function call in the same response.
    // Keep message text and audio together even when the response also calls a tool.
    if (type === 'response.output_item.added') {
      const item = event.item as Record<string, unknown> | undefined;
      if (item?.type === 'function_call') this.suppressUnclassifiedToolOutput(outputResponseId);
      else if (item?.type === 'message' &&
        !this.finishedOutputResponses.has(outputResponseId) && !this.interruptedResponseIds.has(outputResponseId)) {
        this.silentToolResponses.delete(outputResponseId);
        const items = this.messageOutputItems.get(outputResponseId) || new Set<string>();
        items.add(String(item.id || ''));
        this.messageOutputItems.set(outputResponseId, items);
        const held = this.heldOutput.get(outputResponseId);
        if (held) {
          this.streamMessageText(outputResponseId, held);
          const pending = held.audio;
          held.audio = [];
          for (const chunk of pending) {
            if (this.isMessageOutput(chunk.event, outputResponseId)) this.enqueueAudioDelta(chunk.event, chunk.encoded, outputResponseId || null);
            else held.audio.push(chunk);
          }
        }
      }
    }
    if (type === 'response.function_call_arguments.done') {
      this.suppressUnclassifiedToolOutput(outputResponseId);
    }
    if (this.holdResponseOutput(type, event, outputResponseId)) return;
    if (type === 'response.done') {
      const output = response?.output;
      if (Array.isArray(output) && output.some(item => item?.type === 'function_call')) {
        this.suppressUnclassifiedToolOutput(outputResponseId);
      }
      this.releaseResponseOutput(outputResponseId, String(response?.status || 'completed'));
    }
    if (type === 'response.function_call_arguments.done' && eventResponseId && this.interruptedResponseIds.has(eventResponseId)) {
      this.emitDiagnostic('qwen_tool_call_stale', { response_id: eventResponseId, call_id: String(event.call_id || '') });
      return; // Cancellation may end a function call with incomplete JSON.
    }
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
          response_id: eventResponseId || '',
        });
        const args = JSON.parse(functionCall.arguments) as Record<string, unknown>;
        const signature = JSON.stringify([functionCall.name, Object.keys(args).sort().map(key => [key, args[key]])]);
        const target = JSON.stringify([functionCall.name, args.job_id || '', args.interaction_id || '']);
        if ((this.toolFailures.get(target) || 0) >= 3 || this.toolAttempts >= 16 || this.rejectedToolCalls.has(signature)) {
          const message = '本次操作未完成：已停止重复或超限尝试，请确认任务和要求后再试。';
          this.send(createQwenOmniToolOutputEvent(functionCall.callId, JSON.stringify({ state: 'rejected', retryable: false, error: message })));
          this.emitDiagnostic('qwen_tool_retry_stopped', { name: functionCall.name, call_id: functionCall.callId, attempt: this.toolAttempts });
          this.callbacks.onError(message);
          // No response.create: a rejected-call response must not recursively drive more calls.
          return;
        }
        this.toolAttempts += 1;
        this.toolCalls.set(functionCall.callId, { epoch: this.inputEpoch, signature, target });
        const inputId = this.responseInputs.get(eventResponseId || this.responseId || '');
        this.callbacks.onFunctionCall?.(inputId
          ? {...functionCall, inputId, originalInstruction: this.inputTexts.get(inputId)}
          : functionCall);
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
      this.awaitingProviderResponse = false;
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
      if (this.pendingResponseInterrupt) {
        this.pendingResponseInterrupt = false;
        this.speechReplyPendingAt = null;
        this.speechReplyId = null;
        this.interruptedResponseIds.add(this.responseId);
        this.send(createQwenOmniCancelResponseEvent());
        return;
      }
      this.speechReplyId = this.speechReplyPendingAt !== null ? this.responseId : null;
      this.speechReplyPendingAt = null;
      if (this.cardTurn && !this.cardTurn.cancelling) {
        if (this.pendingApprovals.size && performance.now() - this.cardTurn.at < 8_000) this.cancelForCardTurn();
        else this.cardTurn = null;
      }
      // Only VAD ends user speech. A model response must not re-arm interruption.
      if (!this.cardTurn && !this.interruptWhileUserSpeaking()) this.callbacks.onState('speaking');
    } else if (type === 'audio.cancelled' || type === 'response.audio.cancelled' || type === 'response.cancelled') {
      this.releaseResponseOutput(outputResponseId, 'cancelled');
      this.emitDiagnostic('qwen_response_cancelled', {
        response_id: eventResponseId,
        source: eventResponseId && this.interruptedResponseIds.has(eventResponseId) ? 'local_vad' : 'provider',
        raw_event: rawEvent || JSON.stringify(event),
      });
      if (!eventResponseId || eventResponseId === this.responseId) {
        this.responseActive = false;
        this.playbackNode?.port.postMessage({
          type: 'clear',
          cancelResponse: false,
        });
        this.callbacks.onState('listening');
        this.reissueCardTurn();
      }
    } else if (type === 'response.audio.done' || type === 'response.output_audio.done' || type === 'response.done') {
      const affectsActive = !eventResponseId || eventResponseId === this.responseId || this.notificationConflictWaiting;
      if (type === 'response.done' && response?.status === 'failed') {
        const details = response.status_details as Record<string, unknown> | undefined;
        this.callbacks.onError(readableError(details?.error || details || 'Realtime 模型响应失败，服务端未提供具体原因'));
      }
      if (type === 'response.done' && affectsActive) {
        this.responseActive = false;
        this.pendingResponseInterrupt = false;
        this.notificationConflictWaiting = false;
        this.emitDiagnostic('qwen_response_finished', { response_id: eventResponseId, reason: String(response?.status || '') });
        this.reissueCardTurn();
      }
      if (affectsActive)
        this.enqueuePlaybackDrain(eventResponseId || this.responseId);
      if (type === 'response.done' && affectsActive) this.dispatchQueuedToolResult();
    } else if (type === 'input_audio_buffer.speech_started' || type === 'input_audio_buffer.speech_stopped') {
      this.providerSpeechActive = type === 'input_audio_buffer.speech_started';
      if (this.providerSpeechActive) this.qwenMedia.resumeVideo();
      else this.qwenMedia.pauseVideo();
      if (this.providerSpeechActive) this.latestInputId = String(event.item_id || this.newTurnId('audio'));
      this.awaitingProviderResponse = true;
      // The provider answers this speech by itself; with a card open, that reply is redirected.
      if (type === 'input_audio_buffer.speech_stopped') this.speechReplyPendingAt = performance.now();
      if (type === 'input_audio_buffer.speech_stopped' && this.pendingApprovals.size) {
        this.cardTurn = { at: performance.now(), cancelling: false };
      }
      this.emitDiagnostic('qwen_provider_vad', { source: type, raw_event: rawEvent || JSON.stringify(event) });
    } else if (type === 'conversation.item.input_audio_transcription.delta') {
      this.callbacks.onUserText(`${String(event.delta || event.text || '')}${String(event.stash || '')}`, false);
    } else if (type === 'conversation.item.input_audio_transcription.completed') {
      const transcript = String(event.transcript || '');
      const inputId = String(event.item_id || this.latestInputId);
      if (inputId && transcript.trim()) this.inputTexts.set(inputId, transcript.trim());
      if (this.inputTexts.size > 64) this.inputTexts.delete(this.inputTexts.keys().next().value!);
      if (transcript.trim()) this.resetToolBudget();
      this.emitDiagnostic('qwen_native_asr_completed', {
        transcript,
        has_transcript: Boolean(transcript.trim()),
      });
      this.callbacks.onUserText(transcript, true);
    } else if (type === 'error') {
      const message = readableError(event.error || event);
      const imageAppendError = /^Error append image before append audio\.?$/i.test(message);
      const supersededResponse = message === 'Response request cancelled by new input';
      if (!imageAppendError && !supersededResponse && !message.includes('Conversation already has an active response')) {
        this.releaseResponseOutput(outputResponseId, 'failed');
      }
      const media = this.qwenMedia.snapshot();
      const error = event.error;
      const errorRecord = error && typeof error === 'object' ? (error as Record<string, unknown>) : {};
      if (message.includes('Conversation already has an active response')) {
        // The conversation items were already submitted. Retry only response creation,
        // after the provider's active response ends; never replay a tool or its output.
        this.responseActive = true;
        this.notificationConflictWaiting = true;
        this.retryNotificationResponse = ++this.notificationConflicts <= 3;
        if (!this.retryNotificationResponse) this.callbacks.onError('结果播报响应冲突，已停止重试。请查看任务结果。');
      }
      this.emitDiagnostic('qwen_realtime_error', {
        raw_event: rawEvent || JSON.stringify(event),
        code: String(errorRecord.code || event.code || 'unknown'),
        error_type: String(errorRecord.type || ''),
        message,
        audio_append_sequence: media.audioAppendSequence,
        image_append_sequence: media.imageAppendSequence,
        has_deferred_image: media.hasDeferredImage,
      });
      // Qwen closes a connection after 300s without a generated response.
      // This is a provider limit, not a local idle timer that can be disabled.
      // The owner renews the transport with restored history; never fabricate
      // a response or replay historical tool calls merely to keep it alive.
      if (errorRecord.code === 'response_idle_timeout' && this.sessionReady && this.callbacks.onIdleTimeout) {
        this.callbacks.onIdleTimeout();
        return;
      }
      if (imageAppendError) {
        // The rejected frame does not end audio or an already completed response.
        // Wait for the next provider speech turn before sending another image.
        this.qwenMedia.pauseVideo();
        return;
      }
      if (supersededResponse) {
        // Qwen discarded the pending response when the next spoken input arrived.
        this.pendingResponseInterrupt = false;
        if (!this.responseId) this.responseActive = false;
        return;
      }
      this.callbacks.onError(message);
    }
  }

  private interruptQwenResponse(turnId: string, speechMs: number, level: number, threshold: number): boolean {
    if (!this.responseActive && !this.assistantPlaying) return false;
    const interruptedResponseId = this.responseId;
    if (this.pendingResponseInterrupt) return false;
    if (interruptedResponseId && this.interruptedResponseIds.has(interruptedResponseId)) return false;
    if (interruptedResponseId) {
      this.interruptedResponseIds.add(interruptedResponseId);
      if (this.interruptedResponseIds.size > 64) {
        this.interruptedResponseIds.delete(this.interruptedResponseIds.values().next().value!);
      }
    }
    const cancelEventSent = this.responseActive && Boolean(interruptedResponseId);
    if (this.responseActive && !interruptedResponseId) this.pendingResponseInterrupt = true;
    this.closeStreamedText(interruptedResponseId || '');
    this.heldOutput.delete(interruptedResponseId || '');
    this.messageOutputItems.delete(interruptedResponseId || '');
    if (cancelEventSent) this.send(createQwenOmniCancelResponseEvent());
    this.playbackGeneration += 1;
    this.playbackOperation = Promise.resolve();
    this.queuedDrainResponseId = null;
    this.playbackNode?.port.postMessage({
      type: 'clear',
      cancelResponse: false,
    });
    // response.cancel is a request, not confirmation that the provider is idle.
    this.assistantPlaying = false;
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

  private finishAssistantText(text: string, responseId: string | null): void {
    if (!text) return;
    const toolJobId = this.toolJobIdForResponse(responseId);
    this.callbacks.onAssistantText(text, true, toolJobId, responseId || undefined);
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
  }

  private holdResponseOutput(type: string, event: Record<string, unknown>, responseId: string): boolean {
    if (!/^response\.(?:text\.(?:delta|done)|(?:output_)?audio(?:_transcript)?\.(?:delta|done))$/.test(type)) return false;
    if (this.silentToolResponses.has(responseId) || this.finishedOutputResponses.has(responseId) || this.interruptedResponseIds.has(responseId)) return true;
    if (/^response\.(?:output_)?audio\.(?:delta|done)$/.test(type) && this.isMessageOutput(event, responseId)) {
      if (type.endsWith('.done')) return false;
      const encoded = String(event.delta || event.audio || '');
      if (encoded) this.enqueueAudioDelta(event, encoded, responseId || null);
      return true;
    }
    const output: NonNullable<ReturnType<typeof this.heldOutput.get>> =
      this.heldOutput.get(responseId) || { text: '', transcript: '', audio: [] };
    this.heldOutput.set(responseId, output);
    if (type === 'response.text.delta') output.text += String(event.delta || '');
    else if (type === 'response.text.done') output.text = String(event.text || output.text);
    else if (type.includes('audio_transcript')) {
      if (type.endsWith('.done')) output.transcript = String(event.transcript || output.transcript);
      else {
        const delta = String(event.delta || '');
        if (delta.startsWith(output.transcript)) output.transcript = delta;
        else if (!output.transcript.endsWith(delta)) output.transcript += delta;
      }
    } else if (type.endsWith('.delta')) {
      const encoded = String(event.delta || event.audio || '');
      if (encoded) output.audio.push({ event, encoded });
    }
    if (type.startsWith('response.text.')) output.textItemId = String(event.item_id || '');
    else if (type.includes('audio_transcript')) output.transcriptItemId = String(event.item_id || '');
    this.streamMessageText(responseId, output);
    return true;
  }

  private streamMessageText(responseId: string, output: {
    text: string; transcript: string; textItemId?: string; transcriptItemId?: string; streamedText?: string;
  }): void {
    const text = output.transcript && this.isMessageOutput({ item_id: output.transcriptItemId }, responseId)
      ? output.transcript : this.isMessageOutput({ item_id: output.textItemId }, responseId) ? output.text : '';
    if (!text || output.streamedText === text) return;
    output.streamedText = text;
    this.callbacks.onAssistantText(text, false, this.toolJobIdForResponse(responseId), responseId || undefined);
  }

  private closeStreamedText(responseId: string): void {
    const text = this.heldOutput.get(responseId)?.streamedText;
    if (text) this.callbacks.onAssistantText(text, true, undefined, responseId || undefined);
  }

  private isMessageOutput(event: Record<string, unknown>, responseId: string): boolean {
    const items = this.messageOutputItems.get(responseId);
    if (!items) return false;
    const itemId = String(event.item_id || '');
    return itemId ? items.has(itemId) || items.has('') : items.size > 0;
  }

  private suppressUnclassifiedToolOutput(responseId: string): void {
    if (!this.messageOutputItems.has(responseId)) {
      this.silentToolResponses.add(responseId);
      this.heldOutput.delete(responseId);
    }
    // A separate tool response must also preserve an earlier message's audio.
    const inputId = this.responseInputs.get(responseId);
    if (!inputId) return;
    for (const heldId of this.heldOutput.keys()) {
      if (this.responseInputs.get(heldId) !== inputId) continue;
      if (this.messageOutputItems.has(heldId)) continue;
      this.silentToolResponses.add(heldId);
      this.heldOutput.delete(heldId);
    }
  }

  private releaseResponseOutput(responseId: string, status: string): void {
    this.messageOutputItems.delete(responseId);
    if (this.finishedOutputResponses.has(responseId)) return;
    const output = this.heldOutput.get(responseId);
    if (status !== 'completed') this.closeStreamedText(responseId);
    this.heldOutput.delete(responseId);
    const silent = this.silentToolResponses.delete(responseId);
    this.finishedOutputResponses.add(responseId);
    if (this.finishedOutputResponses.size > 64) {
      this.finishedOutputResponses.delete(this.finishedOutputResponses.values().next().value!);
    }
    if (!output || silent || status !== 'completed' || this.interruptedResponseIds.has(responseId)) return;
    this.finishAssistantText(output.transcript || output.text, responseId || null);
    for (const { event, encoded } of output.audio) this.enqueueAudioDelta(event, encoded, responseId || null);
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
