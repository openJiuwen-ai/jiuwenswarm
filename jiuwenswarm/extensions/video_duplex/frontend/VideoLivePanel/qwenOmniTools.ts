export const QWEN_OMNI_RESEARCH_TOOL_NAME = 'jiuwen_research';
export const QWEN_OMNI_TOOL_INSTRUCTIONS = [
  'The jiuwen_research function is available in this session and is the only valid source for external, current, or otherwise unverified facts.',
  'When such facts are needed, you MUST call jiuwen_research in the same turn before producing any spoken or written answer.',
  'Never say that you need to search, cannot search, do not know, or suggest that the user check another source when jiuwen_research is available.',
  'For example, questions about today\'s weather MUST produce a jiuwen_research function call, not a natural-language preamble.',
  'Use a self-contained query with the resolved subject, place, and date. Do not call the tool for facts clearly visible in the current frame.',
  'After receiving the function result, answer naturally using only facts supported by that result.',
].join('\n');

export interface QwenOmniFunctionCall {
  name: string;
  callId: string;
  arguments: string;
  query: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

export function parseQwenOmniFunctionCall(event: Record<string, unknown>): QwenOmniFunctionCall | null {
  if (event.type !== 'response.function_call_arguments.done') return null;
  const name = String(event.name || '').trim();
  const callId = String(event.call_id || '').trim();
  const rawArguments = String(event.arguments || '').trim();
  if (name !== QWEN_OMNI_RESEARCH_TOOL_NAME || !callId || callId.length > 200 || !rawArguments) return null;
  try {
    const argumentsObject = asRecord(JSON.parse(rawArguments));
    if (typeof argumentsObject?.query !== 'string') return null;
    const query = argumentsObject.query.trim();
    if (!query || query.length > 500) return null;
    return { name, callId, arguments: rawArguments, query };
  } catch {
    return null;
  }
}

export function createQwenOmniToolOutputEvent(callId: string, output: string): Record<string, unknown> {
  return {
    type: 'conversation.item.create',
    item: {
      type: 'function_call_output',
      call_id: callId,
      output,
    },
  };
}

export function createQwenOmniToolFollowupEvent(question: string): Record<string, unknown> {
  return {
    type: 'conversation.item.create',
    item: {
      type: 'message',
      role: 'user',
      content: [{
        type: 'input_text',
        text: [
          '[Jiuwen tool result is ready]',
          `The completed tool call belongs to this earlier user request: ${question}`,
          'Answer that request now using the function result immediately above.',
          'Do not answer a newer conversation turn and do not repeat this tool call.',
        ].join('\n'),
      }],
    },
  };
}

export function createQwenOmniResponseEvent(): Record<string, unknown> {
  return { type: 'response.create' };
}
