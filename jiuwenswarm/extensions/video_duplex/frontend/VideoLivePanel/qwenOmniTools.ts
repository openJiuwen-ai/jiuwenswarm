import { TASK_OPERATION_INSTRUCTIONS, taskResultNotice } from './taskPrompts.js';
export const QWEN_OMNI_DELEGATE_TOOL_NAME = 'jiuwen_delegate';
const QWEN_OMNI_LEGACY_RESEARCH_TOOL_NAME = 'jiuwen_research';
export { QWEN_OMNI_TOOL_INSTRUCTIONS } from './taskPrompts.js';

export interface QwenOmniFunctionCall {
  name: string;
  callId: string;
  arguments: string;
  task: string;
  originalInstruction?: string;
  inputId?: string;
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
  const isManagement = [
    'jiuwen_task_query',
    'jiuwen_task_cancel',
    'jiuwen_task_modify',
    'jiuwen_task_reorder',
    'jiuwen_task_answer',
  ].includes(name);
  if ((!isDelegate && !isLegacyResearch && !isManagement) || !callId || callId.length > 200 || !rawArguments)
    return null;
  try {
    const argumentsObject = asRecord(typeof argumentsValue === 'string' ? JSON.parse(rawArguments) : argumentsValue);
    if (!argumentsObject) return null;
    if (isManagement) return { name, callId, arguments: rawArguments, task: name };
    const schedulingKeys = ['independent', 'depends_on', 'resources'];
    const taskKeys = Object.keys(argumentsObject).filter((key) => !schedulingKeys.includes(key));
    if (taskKeys.length !== 1 || (isLegacyResearch && Object.keys(argumentsObject).length !== 1)) return null;
    if ('independent' in argumentsObject && typeof argumentsObject.independent !== 'boolean') return null;
    for (const key of ['depends_on', 'resources']) {
      if (!(key in argumentsObject)) continue;
      const values = argumentsObject[key];
      if (
        !Array.isArray(values) ||
        values.length > 32 ||
        values.some((value) => typeof value !== 'string' || !value.trim() || value.length > 256)
      )
        return null;
    }
    const argumentName = isDelegate
      ? QWEN_OMNI_DELEGATE_ARGUMENT_NAMES.find((key) => typeof argumentsObject[key] === 'string')
      : 'query';
    if (!argumentName || typeof argumentsObject[argumentName] !== 'string') return null;
    const task = argumentsObject[argumentName].trim();
    if (!task || task.length > 16_000) return null;
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

export interface QwenOmniToolResultContext {
  jobId: string;
  turnId?: string;
  question: string;
}

export function createQwenOmniToolFollowupEvent(
  brief: RealtimeBrief,
  context?: QwenOmniToolResultContext,
): Record<string, unknown> {
  return {
    type: 'conversation.item.create',
    item: {
      type: 'message',
      role: 'user',
      content: [
        {
          type: 'input_text',
          text: taskResultNotice(brief, context?.question),
        },
      ],
    },
  };
}

export function createQwenOmniBriefOutputEvent(
  callId: string,
  brief: RealtimeBrief,
  context?: QwenOmniToolResultContext,
): Record<string, unknown> {
  return createQwenOmniToolOutputEvent(
    callId,
    JSON.stringify({
      ...brief,
      ...(context
        ? {
            task_context: {
              job_id: context.jobId,
              turn_id: context.turnId,
              original_question: context.question.slice(0, 1_000),
            },
          }
        : {}),
    }),
  );
}

export function createQwenOmniResponseEvent(): Record<string, unknown> {
  return { type: 'response.create' };
}

export function createQwenOmniOperationResponseEvent(): Record<string, unknown> {
  return {
    type: 'response.create',
    response: {
      instructions: TASK_OPERATION_INSTRUCTIONS,
    },
  };
}
import type { RealtimeBrief } from './types.js';
