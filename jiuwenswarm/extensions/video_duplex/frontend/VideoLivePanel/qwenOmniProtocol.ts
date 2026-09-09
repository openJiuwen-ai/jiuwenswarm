import {
  createQwenOmniResponseEvent,
  createQwenOmniToolFollowupEvent,
  createQwenOmniToolOutputEvent,
  parseQwenOmniFunctionCall,
  QWEN_OMNI_TOOL_INSTRUCTIONS,
} from './qwenOmniTools.js';

export type { QwenOmniFunctionCall } from './qwenOmniTools.js';
export { parseQwenOmniFunctionCall };

const QWEN_MAX_BASE64_IMAGE_BYTES = 256 * 1024;
const QWEN_SESSION_INSTRUCTIONS = [
  'Streaming Omni Conversation.',
  '你是九问实时视觉助手。',
  '始终结合当前会话中的近期聊天、近期画面和最新画面回答；最新画面优先，不得把已经消失的物体当成仍在画面中。',
  '只把当前可见画面、当前可辨语音、用户明确提供的信息和九问工具结果作为事实依据。画面模糊、文字不完整、对象无法确认时，明确说明无法确认或请用户调整画面，不得猜测品牌、文字、人物、数量或状态。',
  '收到九问工具结果后，只回答结果正文能够直接支持的内容。结果表示材料不足、存在冲突或无法确认时，必须保留该不确定性，不得自行补齐结论。',
  QWEN_OMNI_TOOL_INSTRUCTIONS,
].join('\n');

export interface QwenOmniSessionOptions {
  voice?: string;
  tools?: Array<Record<string, unknown>>;
  inputRate: number;
  outputRate: number;
}

export interface QwenOmniMediaBatch {
  events: Array<Record<string, unknown>>;
  diagnostics: Array<{ event: string; details: Record<string, unknown> }>;
}

export interface QwenOmniMediaSnapshot {
  audioAppendSequence: number;
  imageAppendSequence: number;
  hasDeferredImage: boolean;
}

export function createQwenOmniSessionUpdate(
  options: QwenOmniSessionOptions,
): Record<string, unknown> {
  return {
    type: 'session.update',
    session: {
      modalities: ['audio', 'text'],
      voice: options.voice || 'Ethan',
      instructions: QWEN_SESSION_INSTRUCTIONS,
      audio: {
        input: { format: { type: 'pcm', sample_rate: options.inputRate } },
        output: { format: { type: 'pcm', sample_rate: options.outputRate } },
      },
      turn_detection: {
        type: 'semantic_vad',
        threshold: 0.5,
        silence_duration_ms: 800,
      },
      enable_input_audio_transcription: true,
      tools: options.tools || [],
    },
  };
}

export function createQwenOmniTextTurnEvents(text: string): Array<Record<string, unknown>> {
  return [
    {
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text }],
      },
    },
    createQwenOmniResponseEvent(),
  ];
}

export function createQwenOmniToolResultEvents(
  callId: string,
  result: string,
  question: string,
): Array<Record<string, unknown>> {
  return [
    createQwenOmniToolOutputEvent(callId, result),
    createQwenOmniToolFollowupEvent(question),
    createQwenOmniResponseEvent(),
  ];
}

export function createQwenOmniCancelResponseEvent(): Record<string, unknown> {
  return { type: 'response.cancel' };
}

export function createQwenOmniFinishSessionEvent(): Record<string, unknown> {
  return { type: 'session.finish' };
}

export class QwenOmniMediaSequencer {
  private audioAppendSequence = 0;
  private imageAppendSequence = 0;
  private deferredVideoFrame: string | null = null;

  createBatch(audio: string, includeVideo: boolean, frame: string | null): QwenOmniMediaBatch {
    const events: Array<Record<string, unknown>> = [{
      type: 'input_audio_buffer.append',
      audio,
    }];
    const diagnostics: QwenOmniMediaBatch['diagnostics'] = [];
    this.audioAppendSequence += 1;
    if (!includeVideo) return { events, diagnostics };

    const deferredFrame = this.deferredVideoFrame;
    this.deferredVideoFrame = null;
    if (frame && frame.length > QWEN_MAX_BASE64_IMAGE_BYTES) {
      diagnostics.push({
        event: 'realtime_video_frame_dropped',
        details: { reason: 'qwen_image_too_large', base64_bytes: frame.length },
      });
    } else if (frame) {
      this.deferredVideoFrame = frame;
    }

    // DashScope requires an established audio segment before a video frame.
    // Holding each frame until the next audio flush prevents a first-frame race.
    if (!deferredFrame) {
      if (this.audioAppendSequence === 1 && this.deferredVideoFrame) {
        diagnostics.push({
          event: 'qwen_first_image_deferred',
          details: { audio_append_sequence: this.audioAppendSequence },
        });
      }
      return { events, diagnostics };
    }

    events.push({ type: 'input_image_buffer.append', image: deferredFrame });
    this.imageAppendSequence += 1;
    return { events, diagnostics };
  }

  snapshot(): QwenOmniMediaSnapshot {
    return {
      audioAppendSequence: this.audioAppendSequence,
      imageAppendSequence: this.imageAppendSequence,
      hasDeferredImage: Boolean(this.deferredVideoFrame),
    };
  }

  reset(): void {
    this.audioAppendSequence = 0;
    this.imageAppendSequence = 0;
    this.deferredVideoFrame = null;
  }
}
