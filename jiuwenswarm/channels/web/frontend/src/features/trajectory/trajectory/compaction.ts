// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Human-facing reading of a schema-v2 compaction.completed payload.
 *
 * The payload states what a context compaction did in engine terms (phase,
 * processor, before/after metrics, modified_messages). The inspector needs the
 * same facts phrased for a reader: what triggered it, whether a model was
 * involved, what it saved, and which messages it rewrote or offloaded.
 */

/** Message and token size of the context on one side of a compaction. */
export interface CompactionMetric {
  messages: number | null
  tokens: number | null
}

/** One message the compaction rewrote in place. */
export interface CompactionModifiedMessage {
  /** The message's context_message_id, shared with window commits. */
  messageId: string
  role: string
  toolCallId?: string
  /** Handle that restores the original content; absent when nothing was offloaded. */
  offloadHandle?: string
  offloadType?: string
}

/** Reader-facing facts of one compaction. */
export interface CompactionFacts {
  processor?: string
  /** Why the compaction ran, phrased for a reader. */
  trigger?: string
  /** True when the payload states the compaction made no model call. */
  modelFree: boolean
  before?: CompactionMetric
  after?: CompactionMetric
  savedTokens?: number
  savedPercent?: number
  /** Engine-measured duration, for compactions whose row carries no span timing. */
  durationMs?: number
  modifiedMessages: readonly CompactionModifiedMessage[]
}

const PHASE_TRIGGERS: Readonly<Record<string, string>> = {
  add_messages: 'New messages added to context',
  get_context_window: 'Context prepared for a model request',
  active_compress: 'Compaction requested explicitly',
}

function record(value: unknown): Readonly<Record<string, unknown>> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : undefined
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function metric(value: unknown): CompactionMetric | undefined {
  const source = record(value)
  if (source === undefined) return undefined
  return { messages: finiteNumber(source.messages), tokens: finiteNumber(source.tokens) }
}

function modifiedMessage(value: unknown): CompactionModifiedMessage[] {
  const source = record(value)
  const messageId = nonEmptyString(source?.message_id)
  if (source === undefined || messageId === undefined) return []
  const toolCallId = nonEmptyString(source.tool_call_id)
  const offloadHandle = nonEmptyString(source.offload_handle)
  const offloadType = nonEmptyString(source.offload_type)
  return [{
    messageId,
    role: nonEmptyString(source.role) ?? '',
    ...(toolCallId === undefined ? {} : { toolCallId }),
    ...(offloadHandle === undefined ? {} : { offloadHandle }),
    ...(offloadType === undefined ? {} : { offloadType }),
  }]
}

/**
 * Read the reader-facing facts from a compaction payload.
 * @param detail - Untrusted compaction.completed payload.
 * @returns Facts with every unrecognized or malformed field omitted.
 */
export function compactionFacts(detail: Readonly<Record<string, unknown>>): CompactionFacts {
  const processor = nonEmptyString(detail.processor)
  const phase = nonEmptyString(detail.phase)
  const trigger = phase === undefined ? undefined : PHASE_TRIGGERS[phase]
  const before = metric(detail.before)
  const after = metric(detail.after)
  const saved = record(detail.saved)
  const savedTokens = finiteNumber(saved?.tokens)
  const savedPercent = finiteNumber(saved?.percent)
  const durationMs = finiteNumber(detail.duration_ms)
  const modifiedMessages = Array.isArray(detail.modified_messages)
    ? detail.modified_messages.flatMap(modifiedMessage)
    : []
  return {
    ...(processor === undefined ? {} : { processor }),
    ...(trigger === undefined ? {} : { trigger }),
    modelFree: Array.isArray(detail.model_requests) && detail.model_requests.length === 0,
    ...(before === undefined ? {} : { before }),
    ...(after === undefined ? {} : { after }),
    ...(savedTokens === null ? {} : { savedTokens }),
    ...(savedPercent === null ? {} : { savedPercent }),
    ...(durationMs === null ? {} : { durationMs }),
    modifiedMessages,
  }
}

/**
 * Explain in one sentence what a compaction did to the conversation.
 * @param facts - Facts read by {@link compactionFacts}.
 * @returns A sentence, or undefined when the payload states too little to explain.
 */
export function compactionExplanation(facts: CompactionFacts): string | undefined {
  const modified = facts.modifiedMessages.length
  const offloaded = facts.modifiedMessages.filter(message => message.offloadHandle !== undefined).length
  const beforeMessages = facts.before?.messages ?? null
  const afterMessages = facts.after?.messages ?? null
  const removed = beforeMessages !== null && afterMessages !== null ? beforeMessages - afterMessages : 0
  const parts: string[] = []
  if (removed > 0) parts.push(`${removed} ${removed === 1 ? 'message was' : 'messages were'} folded away`)
  if (offloaded > 0) {
    parts.push(
      `${offloaded} ${offloaded === 1 ? 'message was' : 'messages were'} shortened in place `
      + 'with the original offloaded; the model can reload it by handle',
    )
  }
  const rewritten = modified - offloaded
  if (rewritten > 0) {
    parts.push(`${rewritten} ${rewritten === 1 ? 'message was' : 'messages were'} rewritten in place`)
  }
  if (parts.length === 0) return undefined
  const sentence = parts.join('; ')
  return `${sentence.charAt(0).toUpperCase()}${sentence.slice(1)}.`
}

/**
 * Index the compaction that rewrote each tool result, keyed by tool call id.
 * @param cells - Projected cells in trajectory order.
 * @returns For each rewritten tool call, the index of the last compaction cell that rewrote it.
 */
export function compactionsByToolCall(
  cells: Iterable<{ index: number, compactionDetail?: Readonly<Record<string, unknown>> }>,
): ReadonlyMap<string, number> {
  const byCall = new Map<string, number>()
  for (const cell of cells) {
    if (cell.compactionDetail === undefined) continue
    for (const message of compactionFacts(cell.compactionDetail).modifiedMessages) {
      if (message.toolCallId !== undefined) byCall.set(message.toolCallId, cell.index)
    }
  }
  return byCall
}
