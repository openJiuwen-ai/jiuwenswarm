import { createQwenOmniSessionUpdate, QwenOmniMediaSequencer } from './qwenProtocol.js';
import { normalizeWireEvent, textItem, toolOutput, response } from './contract.js';
import type { ProviderOptions, RealtimeProvider, WireEvent } from './contract.js';

export class QwenProvider implements RealtimeProvider {
  readonly inputRate = 16000;
  readonly outputRate = 24000;
  readonly localVad = true;
  readonly readyOnOpen = true;
  readonly truncatePlayback = false;
  private media = new QwenOmniMediaSequencer();
  configure(options: ProviderOptions) {
    return createQwenOmniSessionUpdate({
      ...options,
      inputRate: this.inputRate,
      outputRate: this.outputRate,
    });
  }
  normalize(event: WireEvent) {
    return normalizeWireEvent(event);
  }
  audio(audio: string, frame: string | null) {
    return this.media.createBatch(audio, true, frame);
  }
  text = textItem;
  toolOutput = toolOutput;
  response = response;
  cancel(_responseId: string | null) {
    return [{ type: 'response.cancel' }];
  }
  finish() {
    return [{ type: 'session.finish' }];
  }
  truncate(_responseId: string, _playedMs: number): WireEvent[] {
    return [];
  }
  played(_responseId: string) {}
  reset() {
    this.media.reset();
  }
  diagnostic(name: string) {
    const legacy = new Set([
      'realtime_audio_blocked_during_speech',
      'realtime_native_asr_completed',
      'realtime_provider_vad',
      'realtime_realtime_error',
      'realtime_response_cancelled',
      'realtime_response_finished',
      'realtime_response_interrupted_by_user',
      'realtime_text_input_dispatched',
      'realtime_tool_call_invalid',
      'realtime_tool_call_received',
      'realtime_tool_call_stale',
      'realtime_tool_result_returned',
      'realtime_tool_result_waiting',
      'realtime_tool_retry_stopped',
      'realtime_vad_candidate_rejected',
      'realtime_vad_error',
      'realtime_vad_loading',
      'realtime_vad_ready',
      'realtime_vad_resync',
    ]);
    return legacy.has(name) ? name.replace(/^realtime_/, 'qwen_') : name;
  }
  snapshot() {
    return { ...this.media.snapshot() };
  }
}
