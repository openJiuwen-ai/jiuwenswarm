export const QWEN_OMNI_DELEGATE_TOOL_NAME = 'jiuwen_delegate';
const QWEN_OMNI_LEGACY_RESEARCH_TOOL_NAME = 'jiuwen_research';
export const QWEN_OMNI_TOOL_INSTRUCTIONS = [
  'The jiuwen_delegate function delegates work to the full Jiuwen Core Agent, which may use all tools and capabilities available in Jiuwen.',
  'Answer directly only when the request can be completed from the current audio, video, conversation, or an earlier Jiuwen result.',
  'If you cannot directly complete a request, MUST call jiuwen_delegate in the same turn instead of refusing, claiming that you lack a capability, asking the user to use another application, or merely saying that a tool is needed.',
  'When you decide to delegate, first give the user one brief, natural acknowledgement that you are handling the request, then call jiuwen_delegate in the same turn. Vary the wording to fit the conversation.',
  'That acknowledgement describes work in progress only. Before the function result arrives, never say the task is complete, provide a guessed result, or imply that the requested action succeeded.',
  'Delegate tasks that need web research, current facts, file access, document processing, calculation, code execution, browser or computer operations, or any other external action.',
  'The task argument must preserve the requested action, target, path or name, output format, and every user constraint. Resolve visual references when possible, but do not shorten the request to keywords.',
  "The client attaches the user's original instruction separately. Your task supplements it and must never replace or weaken it.",
  'Do not claim that delegated work succeeded before the function result arrives. After it arrives, answer the original request naturally from the result.',
].join('\n');

export interface QwenOmniFunctionCall {
  name: string;
  callId: string;
  arguments: string;
  task: string;
}

const QWEN_OMNI_DELEGATE_ARGUMENT_NAMES = ['task', 'query', 'instruction', 'request'] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function parseQwenOmniFunctionCall(event: Record<string, unknown>): QwenOmniFunctionCall | null {
  if (event.type !== 'response.function_call_arguments.done') return null;
  const name = String(event.name || '').trim();
  const callId = String(event.call_id || '').trim();
  const argumentsValue = event.arguments;
  const rawArguments =
    typeof argumentsValue === 'string' ? argumentsValue.trim() : JSON.stringify(argumentsValue || {});
  const isDelegate = name === QWEN_OMNI_DELEGATE_TOOL_NAME;
  const isLegacyResearch = name === QWEN_OMNI_LEGACY_RESEARCH_TOOL_NAME;
  if ((!isDelegate && !isLegacyResearch) || !callId || callId.length > 200 || !rawArguments) return null;
  try {
    const argumentsObject = asRecord(typeof argumentsValue === 'string' ? JSON.parse(rawArguments) : argumentsValue);
    if (!argumentsObject || Object.keys(argumentsObject).length !== 1) return null;
    const argumentName = isDelegate
      ? QWEN_OMNI_DELEGATE_ARGUMENT_NAMES.find((key) => typeof argumentsObject[key] === 'string')
      : 'query';
    if (!argumentName || typeof argumentsObject[argumentName] !== 'string') return null;
    const task = argumentsObject[argumentName].trim();
    if (!task || task.length > 2_000) return null;
    return { name, callId, arguments: rawArguments, task };
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

export function createQwenOmniDetachedToolResultEvent(callId: string): Record<string, unknown> {
  return createQwenOmniToolOutputEvent(
    callId,
    JSON.stringify({
      status: 'completed',
      delivery: 'direct_to_user',
      note: 'Jiuwen delivered this result directly to its original turn. Do not answer it again.',
    }),
  );
}

export function createQwenOmniToolFollowupEvent(): Record<string, unknown> {
  return {
    type: 'conversation.item.create',
    item: {
      type: 'message',
      role: 'user',
      content: [
        {
          type: 'input_text',
          text: [
            '[Jiuwen result delivery notice]',
            'The authoritative full answer is already visible in the Jiuwen interface.',
            'Speak only the summary from the function result in at most two natural Chinese sentences.',
            'Do not reconstruct code, citations, URLs, long details, or missing facts. Do not call any tool.',
          ].join('\n'),
        },
      ],
    },
  };
}

export function createQwenOmniBriefOutputEvent(
  callId: string,
  brief: RealtimeBrief,
): Record<string, unknown> {
  return createQwenOmniToolOutputEvent(callId, JSON.stringify(brief));
}

export function createQwenOmniResponseEvent(): Record<string, unknown> {
  return { type: 'response.create' };
}
import type { RealtimeBrief } from './types.js';
