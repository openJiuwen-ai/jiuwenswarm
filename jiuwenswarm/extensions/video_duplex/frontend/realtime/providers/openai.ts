import { REALTIME_INSTRUCTIONS } from '../instructions.js';
import { normalizeReplyLanguage, speakLanguageInstruction } from '../replyLanguage.js';
import { normalizeWireEvent, textItem, toolOutput, response } from './contract.js';
import type { ProviderOptions, RealtimeProvider, WireEvent } from './contract.js';

interface AudioPart {
  itemId: string;
  contentIndex: number;
  durationMs: number;
}

export class OpenAIProvider implements RealtimeProvider {
  readonly inputRate = 24000;
  readonly outputRate = 24000;
  readonly localVad = false;
  readonly readyOnOpen = false;
  readonly truncatePlayback = true;
  private parts = new Map<string, AudioPart[]>();
  private discarded = new Set<string>();
  private truncatedParts = new Set<string>();
  private imageId: string | null = null;
  private imageSequence = 0;
  private lastImageAt = -Infinity;

  configure(options: ProviderOptions): WireEvent {
    return {
      type: 'session.update',
      session: {
        type: 'realtime',
        output_modalities: ['audio'],
        instructions:
          REALTIME_INSTRUCTIONS + '\n' + speakLanguageInstruction(normalizeReplyLanguage(options.replyLanguage)),
        audio: {
          input: {
            format: { type: 'audio/pcm', rate: this.inputRate },
            transcription: { model: 'gpt-4o-mini-transcribe' },
            turn_detection: {
              type: 'server_vad',
              threshold: 0.5,
              prefix_padding_ms: 300,
              silence_duration_ms: 500,
              create_response: true,
              interrupt_response: true,
            },
          },
          output: {
            format: { type: 'audio/pcm', rate: this.outputRate },
            voice: options.voice || 'marin',
          },
        },
        tools: options.tools || [],
        tool_choice: 'auto',
      },
    };
  }
  normalize(event: WireEvent) {
    const events = normalizeWireEvent(event);
    // Some endpoints provide complete calls only in response.done.
    if (event.type === 'response.done' && !['cancelled', 'failed', 'incomplete'].includes(event.response?.status)) {
      for (const item of event.response?.output || []) {
        if (item.type === 'function_call')
          events.unshift({
            type: 'tool_call',
            response_id: event.response.id,
            call_id: item.call_id,
            name: item.name,
            arguments: item.arguments,
          });
      }
    }
    if (event.type === 'response.output_audio.delta' && event.item_id && event.response_id && event.delta) {
      const key = `${event.response_id}:${event.item_id}:${event.content_index || 0}`;
      if (this.truncatedParts.has(key)) return events.filter((e) => e.type !== 'audio_chunk');
      const parts = this.parts.get(event.response_id) || [];
      const bytes = atob(event.delta).length;
      const part = parts.at(-1);
      const index = event.content_index || 0;
      if (part && part.itemId === event.item_id && part.contentIndex === index) part.durationMs += bytes / 48;
      else
        parts.push({
          itemId: event.item_id,
          contentIndex: index,
          durationMs: bytes / 48,
        });
      this.parts.set(event.response_id, parts);
      if (this.discarded.has(event.response_id))
        events.unshift({ type: 'unheard_audio', response_id: event.response_id });
    }
    return this.discarded.has(event.response_id) ? events.filter((e) => e.type !== 'audio_chunk') : events;
  }
  audio(audio: string, frame: string | null) {
    const events: WireEvent[] = [{ type: 'input_audio_buffer.append', audio }];
    // null means no new sample, not removal of the previous image.
    if (frame === '') {
      if (this.imageId)
        events.push({
          type: 'conversation.item.delete',
          item_id: this.imageId,
        });
      this.imageId = null;
    } else if (frame && frame.length <= 256 * 1024 && Date.now() - this.lastImageAt >= 1000) {
      if (this.imageId)
        events.push({
          type: 'conversation.item.delete',
          item_id: this.imageId,
        });
      this.imageId = 'frame_' + ++this.imageSequence;
      events.push({
        type: 'conversation.item.create',
        item: {
          id: this.imageId,
          type: 'message',
          role: 'user',
          content: [
            {
              type: 'input_image',
              image_url: 'data:image/jpeg;base64,' + frame,
            },
          ],
        },
      });
      this.lastImageAt = Date.now();
    }
    return { events, diagnostics: [] };
  }
  text = textItem;
  toolOutput = toolOutput;
  response = response;
  cancel(responseId: string | null) {
    return [
      {
        type: 'response.cancel',
        ...(responseId ? { response_id: responseId } : {}),
      },
    ];
  }
  finish(): WireEvent[] {
    return [];
  }
  truncate(responseId: string, playedMs: number): WireEvent[] {
    const parts = this.parts.get(responseId) || [];
    this.parts.delete(responseId);
    this.discarded.add(responseId);
    let remaining = Math.max(0, playedMs);
    return parts.flatMap((part, index) => {
      this.truncatedParts.add(`${responseId}:${part.itemId}:${part.contentIndex}`);
      const heard = Math.min(remaining, part.durationMs);
      remaining -= heard;
      return heard < part.durationMs || index === parts.length - 1
        ? [
            {
              type: 'conversation.item.truncate',
              item_id: part.itemId,
              content_index: part.contentIndex,
              audio_end_ms: Math.floor(heard),
            },
          ]
        : [];
    });
  }
  played(responseId: string) {
    this.parts.delete(responseId);
  }
  reset() {
    this.parts.clear();
    this.discarded.clear();
    this.truncatedParts.clear();
    this.imageId = null;
    this.lastImageAt = -Infinity;
  }
  diagnostic(name: string) {
    return name.replace(/^qwen_/, 'realtime_');
  }
  snapshot() {
    return {};
  }
}
