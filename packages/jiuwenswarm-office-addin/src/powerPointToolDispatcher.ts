import type { ClientToolExecutor, ClientToolRequest, ClientToolResult, ContentBlock } from './clientAttachment';
import { withMutationRecord } from './clientToolOutcome';
import { isRecord, type JsonObject } from './gatewayClient';
import type { HostWriteQueue } from './hostWriteQueue';
import { isContractTool, isMutatingTool } from './presentationContract';
import type {
  ClientToolError, ClientToolResponse, HostTimings, PresentationToolName,
} from './presentationToolTypes';
import { fitToolResultJson } from './toolResultBudget';

const MAX_TOOL_RESULT_CHARS = 9500;
/**
 * A call the host has not finished by then is answered as timed out. This only
 * guards against a host that hangs: a write keeps the write queue until the
 * host settles it, so answering a merely slow call early gains nothing.
 */
export const CALL_DEADLINE_MS = 280_000;
/** Answers with their fields at the top level: the deck inventory, and the slide capture the backend's review reads. */
const FLAT_RESULT_TOOLS: ReadonlySet<string> = new Set<PresentationToolName>(
  ['read_presentation_context', 'verify_slide_visual']);
const FAILURE_STATUS = new Map<string, ClientToolResult['status']>(
  [['CLIENT_TOOL_CANCELLED', 'cancelled'], ['CLIENT_TOOL_TIMEOUT', 'timeout']]);

/** Executes one contract tool in the host document. */
export interface PresentationToolRuntime {
  /** The tools it implements; the pane declares no other. */
  readonly tools: ReadonlySet<PresentationToolName>;
  execute(call: {
    /** The call's request id, which names the call to the turn's undo. */
    callId: string;
    toolName: PresentationToolName;
    input: JsonObject;
    downloads: Readonly<Record<string, string>>;
  }): Promise<ClientToolResponse>;
}

/** One runtime of several, each running the tools it declares. */
export function joinRuntimes(...runtimes: PresentationToolRuntime[]): PresentationToolRuntime {
  const owners = new Map(runtimes.flatMap(runtime => [...runtime.tools].map(tool => [tool, runtime] as const)));
  return {
    tools: new Set(owners.keys()),
    execute: call => {
      const owner = owners.get(call.toolName);
      if (!owner) throw new Error(`${call.toolName} is not a tool of this pane`);
      return owner.execute(call);
    },
  };
}

/** How long a call took, for the answer's ``data``; the model never sees it. */
export interface ToolTelemetry {
  elapsed_ms: number;
  queue_wait_ms: number;
  host: HostTimings;
}

/**
 * Runs the PowerPoint calls of the attached conversation: reads at once and
 * concurrently, writes one at a time in the host write queue.
 */
export class PowerPointToolDispatcher implements ClientToolExecutor {
  /** Advanced by ``cancelAll``; a write still queued from before does not run. */
  private generation = 0;

  constructor(private readonly runtime: PresentationToolRuntime, private readonly writes: HostWriteQueue) {}

  cancelAll(): void {
    this.generation += 1;
  }

  async execute(request: ClientToolRequest): Promise<ClientToolResult> {
    const { toolName } = request;
    if (!isContractTool(toolName)) {
      return toolResult(toolName, notApplied('UNSUPPORTED_CLIENT_TOOL', `${toolName} is not a PowerPoint tool`),
        { elapsed_ms: 0, queue_wait_ms: 0, host: {} });
    }
    const started = Date.now();
    const generation = this.generation;
    const mutating = isMutatingTool(toolName);
    let hostStarted = false;
    let answered = false;
    const run = async (): Promise<ClientToolResponse> => {
      // Cancelled, or already answered at its deadline while it waited: it never reaches the host.
      if (answered || generation !== this.generation) return notApplied('CLIENT_TOOL_CANCELLED', 'Tool call cancelled');
      hostStarted = true;
      try {
        return await this.runtime.execute(
          { callId: request.requestId, toolName, input: request.input, downloads: request.downloads });
      } catch (error) {
        return { ok: false, error: {
          code: 'CLIENT_TOOL_EXECUTION_FAILED', message: error instanceof Error ? error.message : String(error),
          retryable: false, category: 'host', details: { outcome: 'unknown' },
        } };
      }
    };
    let queueWait = 0;
    const settled = mutating
      ? this.writes.exclusive(() => {
        queueWait = Date.now() - started;
        return run();
      }).then(withMutationRecord)
      : run();
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const response = await Promise.race([settled, new Promise<ClientToolResponse>(resolve => {
      deadline = setTimeout(() => resolve(timedOut(mutating, hostStarted)), CALL_DEADLINE_MS);
    })]);
    clearTimeout(deadline);
    answered = true;
    const elapsed = Date.now() - started;
    const telemetry = {
      elapsed_ms: elapsed, queue_wait_ms: hostStarted ? queueWait : elapsed, host: response.hostTimings ?? {},
    };
    try {
      return toolResult(toolName, response, telemetry);
    } catch (error) {
      // The host's result does not serialize (a BigInt, a cycle); the call is answered all the same.
      return toolResult(toolName, unreadable(mutating, error), telemetry);
    }
  }

  declineHeldElsewhere(request: ClientToolRequest): ClientToolResult {
    return toolResult(request.toolName, notApplied(
      'CLIENT_TOOL_DISPLACED',
      'This presentation is open in another window, which now runs its PowerPoint tools; nothing was changed. '
        + 'Tell the user to continue in that window.',
      'conflict',
    ), { elapsed_ms: 0, queue_wait_ms: 0, host: {} });
  }
}

function toolResult(toolName: string, response: ClientToolResponse, telemetry: ToolTelemetry): ClientToolResult {
  const mutation = response.mutation ? { mutation: response.mutation } : {};
  let value: JsonObject;
  if (!response.ok) {
    const { error } = response;
    value = {
      success: false, error: error.message, code: error.code, retryable: error.retryable, category: error.category,
      ...(error.details ? { details: error.details } : {}), ...mutation,
    };
  } else if (FLAT_RESULT_TOOLS.has(toolName)) {
    value = { ...(isRecord(response.result) ? response.result : { result: response.result }),
      ...response.successFields, ...mutation };
  } else {
    value = { success: true, ...response.successFields,
      ...(response.result === undefined ? {} : { result: response.result }), ...mutation };
  }
  const content: ContentBlock[] = [{ type: 'text', text: fitToolResultJson(value, MAX_TOOL_RESULT_CHARS) }];
  if (response.ok && response.imageBase64) {
    content.push({ type: 'image', source: { type: 'base64', media_type: 'image/png', data: response.imageBase64 } });
  }
  return {
    status: response.ok ? 'completed' : FAILURE_STATUS.get(response.error.code) ?? 'error',
    content,
    is_error: !response.ok,
    data: { telemetry },
  };
}

function notApplied(
  code: string, message: string, category: ClientToolError['category'] = 'validation',
): ClientToolResponse {
  return { ok: false, error: { code, message, retryable: false, category,
    details: { outcome: 'not_applied', native_mutation_started: false } } };
}

/** The answer when the host's result cannot be read. A write the host ran may have landed. */
function unreadable(mutating: boolean, error: unknown): ClientToolResponse {
  const response: ClientToolResponse = { ok: false, error: {
    code: 'CLIENT_TOOL_RESULT_UNREADABLE',
    message: `PowerPoint's result could not be read: ${error instanceof Error ? error.message : String(error)}`,
    retryable: false, category: 'host',
    details: mutating ? { outcome: 'unknown', native_mutation_started: true } : { outcome: 'not_applied' },
  } };
  return mutating ? withMutationRecord(response) : response;
}

/** The answer at the deadline. A write the host is still running may yet land. */
function timedOut(mutating: boolean, hostStarted: boolean): ClientToolResponse {
  const writing = mutating && hostStarted;
  const response: ClientToolResponse = { ok: false, error: {
    code: 'CLIENT_TOOL_TIMEOUT',
    message: writing
      ? 'PowerPoint did not finish this change in time; it may still complete. Inspect the affected slides first.'
      : 'PowerPoint did not finish this call in time',
    retryable: false, category: 'timeout',
    details: { outcome: writing ? 'unknown' : 'not_applied', native_mutation_started: writing },
  } };
  return mutating ? withMutationRecord(response) : response;
}
