import type { OfficeApp, Surface } from './appContract';
import type { ClientToolExecutor, ClientToolRequest, ClientToolResult, ContentBlock } from './clientAttachment';
import { withMutationRecord } from './clientToolOutcome';
import { isRecord, type JsonObject } from './gatewayClient';
import type { HostWriteQueue } from './hostWriteQueue';
import { fitToolResultJson } from './toolResultBudget';

const MAX_TOOL_RESULT_CHARS = 9500;
/**
 * A call the host has not finished by then is answered as timed out. This only
 * guards against a host that hangs: a write keeps the write queue until the
 * host settles it, so answering a merely slow call early gains nothing.
 */
export const CALL_DEADLINE_MS = 280_000;
/** What a write changes, as a timed-out write tells the model to look at first. */
const PARTS: Record<Surface, string> = { presentation: 'slides', document: 'paragraphs', workbook: 'ranges' };
const FAILURE_STATUS = new Map<string, ClientToolResult['status']>(
  [['CLIENT_TOOL_CANCELLED', 'cancelled'], ['CLIENT_TOOL_TIMEOUT', 'timeout']]);

/** Executes one contract tool in the host document. */
export interface ClientToolRuntime<Name extends string = string> {
  /** The tools it implements; the pane declares no other. */
  readonly tools: ReadonlySet<Name>;
  execute(call: {
    /** The call's request id, which names the call to the turn's undo. */
    callId: string;
    toolName: Name;
    input: JsonObject;
    downloads: Readonly<Record<string, string>>;
  }): Promise<ClientToolResponse>;
}

/** One runtime of several, each running the tools it declares. */
export function joinRuntimes(...runtimes: ClientToolRuntime[]): ClientToolRuntime {
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
 * Runs the host calls of the attached conversation: reads at once and
 * concurrently, writes one at a time in the host write queue.
 */
export class ClientToolDispatcher implements ClientToolExecutor {
  /** Advanced by ``cancelAll``; a write still queued from before does not run. */
  private generation = 0;

  constructor(
    private readonly app: OfficeApp, private readonly runtime: ClientToolRuntime,
    private readonly writes: HostWriteQueue,
  ) {}

  cancelAll(): void {
    this.generation += 1;
  }

  async execute(request: ClientToolRequest): Promise<ClientToolResult> {
    const { toolName } = request;
    const { contract, host, surface } = this.app;
    if (!contract.has(toolName)) {
      return this.answer(toolName, notApplied('UNSUPPORTED_CLIENT_TOOL', `${toolName} is not a ${host} tool`),
        { elapsed_ms: 0, queue_wait_ms: 0, host: {} });
    }
    const started = Date.now();
    const generation = this.generation;
    const mutating = contract.isMutating(toolName);
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
      deadline = setTimeout(() => resolve(timedOut(host, PARTS[surface], mutating, hostStarted)), CALL_DEADLINE_MS);
    })]);
    clearTimeout(deadline);
    answered = true;
    const elapsed = Date.now() - started;
    const telemetry = {
      elapsed_ms: elapsed, queue_wait_ms: hostStarted ? queueWait : elapsed, host: response.hostTimings ?? {},
    };
    try {
      return this.answer(toolName, response, telemetry);
    } catch (error) {
      // The host's result does not serialize (a BigInt, a cycle); the call is answered all the same.
      return this.answer(toolName, unreadable(host, mutating, error), telemetry);
    }
  }

  declineHeldElsewhere(request: ClientToolRequest): ClientToolResult {
    return this.answer(request.toolName, notApplied(
      'CLIENT_TOOL_DISPLACED',
      `This ${this.app.surface} is open in another window, which now runs its ${this.app.host} tools; `
        + 'nothing was changed. Tell the user to continue in that window.',
      'conflict',
    ), { elapsed_ms: 0, queue_wait_ms: 0, host: {} });
  }

  private answer(toolName: string, response: ClientToolResponse, telemetry: ToolTelemetry): ClientToolResult {
    return toolResult(response, telemetry, this.app.flatResults?.has(toolName) ?? false);
  }
}

function toolResult(response: ClientToolResponse, telemetry: ToolTelemetry, flat: boolean): ClientToolResult {
  const mutation = response.mutation ? { mutation: response.mutation } : {};
  let value: JsonObject;
  if (!response.ok) {
    const { error } = response;
    value = {
      success: false, error: error.message, code: error.code, retryable: error.retryable, category: error.category,
      ...(error.details ? { details: error.details } : {}), ...mutation,
    };
  } else if (flat) {
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
function unreadable(host: string, mutating: boolean, error: unknown): ClientToolResponse {
  const response: ClientToolResponse = { ok: false, error: {
    code: 'CLIENT_TOOL_RESULT_UNREADABLE',
    message: `${host}'s result could not be read: ${error instanceof Error ? error.message : String(error)}`,
    retryable: false, category: 'host',
    details: mutating ? { outcome: 'unknown', native_mutation_started: true } : { outcome: 'not_applied' },
  } };
  return mutating ? withMutationRecord(response) : response;
}

/** The answer at the deadline. A write the host is still running may yet land. */
function timedOut(host: string, parts: string, mutating: boolean, hostStarted: boolean): ClientToolResponse {
  const writing = mutating && hostStarted;
  const response: ClientToolResponse = { ok: false, error: {
    code: 'CLIENT_TOOL_TIMEOUT',
    message: writing
      ? `${host} did not finish this change in time; it may still complete. Inspect the affected ${parts} first.`
      : `${host} did not finish this call in time`,
    retryable: false, category: 'timeout',
    details: { outcome: writing ? 'unknown' : 'not_applied', native_mutation_started: writing },
  } };
  return mutating ? withMutationRecord(response) : response;
}

export interface ClientToolError {
  code: string;
  message: string;
  retryable: boolean;
  category: 'capability' | 'validation' | 'conflict' | 'transport' | 'timeout' | 'host';
  details?: Record<string, unknown>;
}

/** What a write did to the document, as the agent reads it; the slide fields are PowerPoint's. */
export interface MutationRecord {
  state: 'not_applied' | 'applied' | 'partial' | 'uncertain';
  native_mutation_started: boolean;
  last_completed_stage?: string;
  created_slide_ids?: string[];
  deleted_slide_ids?: string[];
  affected_slide_ids?: string[];
  replacement_slide_ids?: Record<string, string>;
  verification_targets?: string[];
  /**
   * Which slide_ref each already resolved to on the live document, in the
   * order they resolved. Present on a partial/uncertain outcome so the
   * caller's next patch can reuse the real slide_id for a slide_ref instead
   * of re-deriving position/role bookkeeping from a stale assumption after
   * a mid-batch failure (2026-09-19 live finding: an unreported orphaned
   * slide desynced the caller's slide_ref → slide_id map from the document).
   */
  resolved_slides?: { slide_ref: string; slide_id: string; created: boolean }[];
  safe_to_retry: boolean;
}

/** Time a tool spent in the host, as the runtime measured it; goes to the answer's ``data``, not to the model. */
export interface HostTimings {
  export_ms?: number;
  insert_ms?: number;
  render_ms?: number;
  sync_count?: number;
}

interface ResponseRecords {
  /** What a write did to the deck, as the runtime saw it; ``withMutationRecord`` turns it into ``mutation``. */
  mutationEvidence?: Record<string, unknown>;
  mutation?: MutationRecord;
  hostTimings?: HostTimings;
}

/** What a tool runtime returns for one call. */
export type ClientToolResponse = ResponseRecords & (
  | {
    ok: true;
    result?: unknown;
    successFields?: Record<string, unknown>;
    /** A PNG the tool produced (a slide render), returned as an image block. */
    imageBase64?: string;
  }
  | { ok: false; error: ClientToolError }
);
