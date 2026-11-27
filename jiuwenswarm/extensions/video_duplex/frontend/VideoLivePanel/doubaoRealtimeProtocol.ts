import { realtimeInstructions, type QwenOmniSessionOptions } from './qwenOmniProtocol.js';

export const DOUBAO_AUDIO_CHUNK_MS = 20;
export const DOUBAO_FRAME_INTERVAL_MS = 500;

export function createDoubaoSession(options: QwenOmniSessionOptions, model: string) {
  return {
    type: 'session.create',
    session: {
      model,
      instructions: realtimeInstructions(options),
      audio: {
        input: { format: { type: 'pcm', rate: options.inputRate } },
        output: { format: { type: 'pcm_s16le', rate: options.outputRate }, voice: options.voice },
      },
      tools: (options.tools || []).map(tool => tool.function
        ? {type: 'function', ...(tool.function as Record<string, unknown>)} : tool),
    },
    extension: { video: { frame_format: 'JPEG', frame_rate: 2 } },
  };
}

export function createDoubaoToolOutput(callId: string, output: string) {
  return {
    type: 'conversation.item.create',
    items: [{ role: 'tool', call_id: callId, content: [{ type: 'input_text', text: output }] }],
  };
}

export function normalizeDoubaoEvents(event: Record<string, unknown>, activeResponseId: string | null) {
  const type = String(event.type || '');
  // Seed creates the response with a question ID; the audio's response ID arrives later.
  // Use the question ID for both text and playback throughout the turn.
  const responseId = event.question_id || activeResponseId || event.response_id;
  const beginsResponse = type === 'response.output_audio.started' || type.startsWith('response.output_text.')
    || type === 'response.function_call_arguments.done';
  const started = beginsResponse && responseId && responseId !== activeResponseId
    ? [{type: 'response.created', response: {id: responseId}}] : [];
  if (type === 'response.function_call_arguments.done' && Array.isArray(event.items)) {
    return [...started, ...event.items.filter(item => item && typeof item === 'object')
      .map(item => ({ ...item, type, response_id: responseId }) as Record<string, unknown>)];
  }
  // Seed ASR is display-only; never infer provider playback states from it.
  if (type === 'conversation.item.input_audio_transcription.completed') {
    return [{ ...event, transcript: event.transcript || event.text }];
  }
  if (!type.startsWith('response.')) return [event];
  const normalized = {
    ...event,
    type: type.replace(/^response\.output_text\./, 'response.text.')
      .replace(/^response\.canceled$/, 'response.cancelled'),
    response_id: responseId,
  };
  if (type === 'response.output_audio.started' || type.startsWith('response.output_text.')) {
    // Seed sends media without an output-item event. Declare it as assistant output for the shared player.
    return [...started, { type: 'response.output_item.added', response_id: responseId, item: { type: 'message' } }, normalized];
  }
  return [normalized];
}


/** Seed accepts complete QA pairs for silent context updates; this starts no model turn. */
export function createDoubaoContext(text: string) {
  return {
    type: 'conversation.item.create',
    items: [
      { type: 'message', role: 'user', content: [{ type: 'input_text', text }] },
      { type: 'message', role: 'assistant', content: [{ type: 'input_text',
        text: 'Jiuwen background received. This is context only, not a new user request or proof of task completion.',
      }] },
    ],
  };
}
