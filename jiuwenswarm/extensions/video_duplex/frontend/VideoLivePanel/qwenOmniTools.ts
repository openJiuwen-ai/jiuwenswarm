export * from '../realtime/taskBridge.js';
import { parseTaskCall } from '../realtime/taskBridge.js';
export function parseQwenOmniFunctionCall(event: Record<string, unknown>) {
  if (event.type !== 'response.function_call_arguments.done') return null;
  return parseTaskCall({...event, type: 'tool_call'});
}
export { RealtimeFunctionCall as QwenOmniFunctionCall, ToolResultContext as QwenOmniToolResultContext, createToolOutputEvent as createQwenOmniToolOutputEvent, createToolFollowupEvent as createQwenOmniToolFollowupEvent, createBriefOutputEvent as createQwenOmniBriefOutputEvent, createResponseEvent as createQwenOmniResponseEvent, createOperationResponseEvent as createQwenOmniOperationResponseEvent } from '../realtime/taskBridge.js';
export { REALTIME_TOOL_INSTRUCTIONS as QWEN_OMNI_TOOL_INSTRUCTIONS } from '../realtime/taskPrompts.js';
