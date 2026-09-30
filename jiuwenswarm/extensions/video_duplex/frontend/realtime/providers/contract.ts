/** Wire data stays at the protocol boundary; the runtime consumes normalized events. */
export type WireEvent = Record<string, any>;
export interface ProviderOptions {
  voice?: string;
  tools?: Array<Record<string, unknown>>;
  replyLanguage?: string;
}
export interface ProviderEvent {
  type: string;
  response_id?: string;
  response?: { id?: string; status?: string; status_details?: any };
  item_id?: string;
  content_index?: number;
  call_id?: string;
  name?: string;
  arguments?: unknown;
  delta?: string;
  text?: string;
  transcript?: string;
  stash?: string;
  error?: any;
  reason?: string;
  code?: string;
  errorCategory?: 'response_busy' | 'other';
}
export interface VisualFrame {
  data: string;
  sourceId: string;
  timestamp: number;
}
export interface RealtimeProvider {
  readonly inputRate: number;
  readonly outputRate: number;
  readonly localVad: boolean;
  readonly readyOnOpen: boolean;
  readonly truncatePlayback: boolean;
  configure(options: ProviderOptions): WireEvent;
  normalize(event: WireEvent): ProviderEvent[];
  audio(
    audio: string,
    frame: string | null,
  ): {
    events: WireEvent[];
    diagnostics: Array<{ event: string; details: Record<string, unknown> }>;
  };
  text(text: string): WireEvent;
  toolOutput(callId: string, output: string): WireEvent;
  response(instructions?: string): WireEvent;
  cancel(responseId: string | null): WireEvent[];
  finish(): WireEvent[];
  truncate(responseId: string, playedMs: number): WireEvent[];
  played(responseId: string): void;
  reset(): void;
  diagnostic(name: string): string;
  snapshot(): Record<string, unknown>;
}

const eventTypes: Record<string, string> = {
  'session.created': 'session_created',
  'session.updated': 'session_ready',
  'session.closed': 'session_closed',
  'session.queue_done': 'queue_ready',
  queue_done: 'queue_ready',
  'response.created': 'response_started',
  'response.done': 'response_finished',
  'response.text.delta': 'text_delta',
  'response.output_text.delta': 'text_delta',
  'response.text.done': 'text_done',
  'response.output_text.done': 'text_done',
  'response.audio.delta': 'audio_chunk',
  'response.output_audio.delta': 'audio_chunk',
  'response.audio.done': 'audio_finished',
  'response.output_audio.done': 'audio_finished',
  'response.audio_transcript.delta': 'transcript_delta',
  'response.output_audio_transcript.delta': 'transcript_delta',
  'response.audio_transcript.done': 'transcript_done',
  'response.output_audio_transcript.done': 'transcript_done',
  'audio.cancelled': 'response_cancelled',
  'response.audio.cancelled': 'response_cancelled',
  'response.cancelled': 'response_cancelled',
  'input_audio_buffer.speech_started': 'speech_started',
  'input_audio_buffer.speech_stopped': 'speech_stopped',
  'conversation.item.input_audio_transcription.delta': 'input_transcript_delta',
  'conversation.item.input_audio_transcription.completed': 'input_transcript_done',
  'response.function_call_arguments.done': 'tool_call',
  'conversation.item.truncated': 'context_truncated',
  error: 'error',
};

export function normalizeWireEvent(event: WireEvent): ProviderEvent[] {
  const type = eventTypes[String(event.type)];
  if (!type) return [];
  return [
    {
      type,
      response_id: event.response_id || event.response?.id,
      response: event.response,
      item_id: event.item_id,
      content_index: event.content_index,
      call_id: event.call_id,
      name: event.name,
      arguments: event.arguments,
      delta: event.delta || event.audio,
      text: event.text,
      transcript: event.transcript,
      stash: event.stash,
      error: event.error,
      reason: event.reason,
      code: event.code,
      errorCategory:
        event.error?.code === 'conversation_already_has_active_response' ||
        String(event.error?.message || '').includes('Conversation already has an active response')
          ? 'response_busy'
          : 'other',
    },
  ];
}

/** The two JSON protocols share these messages, not their audio/session schemas. */
export const textItem = (text: string): WireEvent => ({
  type: 'conversation.item.create',
  item: {
    type: 'message',
    role: 'user',
    content: [{ type: 'input_text', text }],
  },
});
export const toolOutput = (callId: string, output: string): WireEvent => ({
  type: 'conversation.item.create',
  item: { type: 'function_call_output', call_id: callId, output },
});
export const response = (instructions?: string): WireEvent => ({
  type: 'response.create',
  ...(instructions ? { response: { instructions } } : {}),
});
