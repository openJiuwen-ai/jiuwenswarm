import {
  createQwenOmniSessionUpdate, createQwenOmniTextTurnEvents, createQwenOmniToolResultEvents,
  QwenOmniMediaSequencer, type QwenOmniSessionOptions,
} from './qwenOmniProtocol.js';
import { createQwenOmniOperationResponseEvent, createQwenOmniToolOutputEvent,
  type QwenOmniToolResultContext } from './qwenOmniTools.js';
import { createDoubaoContext, createDoubaoSession, createDoubaoToolOutput,
  DOUBAO_AUDIO_CHUNK_MS, DOUBAO_FRAME_INTERVAL_MS, normalizeDoubaoEvents } from './doubaoRealtimeProtocol.js';
import { taskResultNotice } from './taskPrompts.js';
import type { RealtimeBrief } from './types.js';

export const DEFAULT_RESULT_SPEECH = '请查看任务卡片中的最新结果。';

/** Wire format and transport capabilities only. Task/card state belongs to the shared session. */
export class RealtimeProtocol {
  private readonly seed: boolean;
  private imageAttempted = false;
  private imageRejected = false;
  readonly media = new QwenOmniMediaSequencer();

  constructor(provider: 'qwen_omni' | 'doubao' = 'qwen_omni') {
    this.seed = provider === 'doubao';
  }

  get supportsImageInput(): boolean { return !this.imageRejected; }

  rejectUnsupportedImage(event: Record<string, unknown>): boolean {
    if (!this.imageAttempted) return false;
    const error = event.error && typeof event.error === 'object' ? event.error : event;
    const detail = typeof event.error === 'string' ? event.error : JSON.stringify(error);
    // Input validation, quota and authentication errors do not establish model capabilities.
    if (/format|encoding|base64|mime|resolution|dimensions|size|too.large|unauthor|forbidden|quota|rate.limit|401|403|429|格式|编码|尺寸|大小|分辨率|鉴权|权限|限流|额度/i.test(detail)) return false;
    const image = /image|video|vision|visual|图像|图片|视频|视觉/i.test(detail);
    const unsupported = /not.support|unsupported|doesn['’]t.support|not.enabled|unknown.event|不支持|不具备/i.test(detail);
    if (!image || !unsupported) return false;
    this.imageRejected = true;
    this.media.pauseVideo();
    return true;
  }

  get supportsTextInput(): boolean { return !this.seed; }
  get clientControlsReplies(): boolean { return !this.seed; }
  get directSpeech(): boolean { return this.seed; }
  get immediateAudio(): boolean { return this.seed; }
  get captureOptions(): AudioWorkletNodeOptions | undefined {
    return this.seed ? {processorOptions: {chunkMs: DOUBAO_AUDIO_CHUNK_MS}} : undefined;
  }
  get tickMs(): number { return this.seed ? DOUBAO_FRAME_INTERVAL_MS : 200; }
  static frameIntervalMs(provider?: 'qwen_omni' | 'doubao'): number {
    return provider === 'doubao' ? DOUBAO_FRAME_INTERVAL_MS : 1_000;
  }
  get name(): string { return this.seed ? '豆包' : '千问'; }

  session(options: QwenOmniSessionOptions, model: string | undefined, ready: boolean): Record<string, unknown> {
    return this.seed ? { ...createDoubaoSession(options, model),
      type: ready ? 'session.update' : 'session.create' } : createQwenOmniSessionUpdate(options);
  }

  waitForReady(restore: boolean): boolean { return this.seed || restore; }
  readyAck(type: string, restore: boolean): boolean {
    if (this.seed) return type === 'session.created';
    return type === 'session.updated' || (!restore && type === 'session.created');
  }

  normalize(event: Record<string, unknown>, responseId: string | null): Array<Record<string, unknown>> {
    return this.seed ? normalizeDoubaoEvents(event, responseId) : [event];
  }

  audio(audio: string, video: boolean, frame: string | null) {
    const batch = this.seed ? {events: [{type: 'input_audio_buffer.append', audio}], diagnostics: []}
      : this.media.createBatch(audio, video && this.supportsImageInput, frame);
    if (batch.events.some(event => event.type === 'input_image_buffer.append')) this.imageAttempted = true;
    return batch;
  }

  video(frame: string | null): Array<Record<string, unknown>> {
    if (!this.seed || !frame || !this.supportsImageInput) return [];
    this.imageAttempted = true;
    return [{type: 'input_image_buffer.append', image: frame}];
  }

  mute(muted: boolean): Array<Record<string, unknown>> {
    return this.seed ? [{type: muted ? 'input_audio_mute.commit' : 'input_audio_unmute.commit'}] : [];
  }

  text(text: string): Array<Record<string, unknown>> {
    return this.supportsTextInput ? createQwenOmniTextTurnEvents(text) : [];
  }

  context(text: string): Record<string, unknown> {
    return this.seed ? createDoubaoContext(text) : { type: 'conversation.item.create', item: {
      type: 'message', role: 'user', content: [{type: 'input_text', text}],
    } };
  }

  toolOutput(callId: string, output: string): Record<string, unknown> {
    return this.seed ? createDoubaoToolOutput(callId, output) : createQwenOmniToolOutputEvent(callId, output);
  }

  toolAvailable(name: string, tools?: Array<Record<string, unknown>>): boolean {
    return !tools || tools.some(tool => (tool.function as {name?: string} | undefined)?.name === name
      || tool.name === name);
  }

  response(operation = false, speech?: string): Record<string, unknown> {
    return this.seed ? {type: 'speech_text_buffer.commit', text: speech || DEFAULT_RESULT_SPEECH}
      : operation ? createQwenOmniOperationResponseEvent() : {type: 'response.create'};
  }

  toolResult(callId: string, brief: RealtimeBrief, context: QwenOmniToolResultContext,
    language: string | undefined, includeOutput: boolean): Array<Record<string, unknown>> {
    if (this.seed) return [this.context(taskResultNotice(brief, context.question, language, context.jobId)),
      this.response(false, brief.summary)];
    const events = createQwenOmniToolResultEvents(callId, brief, context, language);
    return includeOutput ? events : events.slice(1);
  }

  cancel(): Record<string, unknown> { return {type: 'response.cancel'}; }

  close(socket: WebSocket | null): void {
    if (!socket) return;
    if (this.seed && socket.readyState === WebSocket.OPEN) {
      // The bounded wait is transport cleanup, not a task/card lifetime limit.
      const timer = window.setTimeout(() => socket.close(1000, 'client stop'), 2_000);
      socket.onmessage = ({data}) => {
        try {
          if (JSON.parse(String(data)).type !== 'session.closed') return;
          window.clearTimeout(timer);
          socket.close(1000, 'client stop');
        } catch { /* Ignore unrelated events after local stop. */ }
      };
      socket.onclose = () => window.clearTimeout(timer);
      socket.send(JSON.stringify({type: 'session.close'}));
      return;
    }
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: 'session.finish'}));
    socket.close(1000, 'client stop');
  }
}
