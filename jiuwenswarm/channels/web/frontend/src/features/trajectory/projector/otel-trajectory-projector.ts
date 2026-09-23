// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Pure OTLP/GenAI to standalone trajectory read-model projection. */

import {
  exactAttributeMap,
  readStringAttribute,
} from '../semconv/attributes.ts'
import {
  GEN_AI_OPERATIONS, STANDARD_ATTRIBUTES,
} from '../semconv/constants.ts'
import {
  createTrajectoryV2Reducer,
  isTrajectoryV2Record,
  reduceTrajectoryV2,
  trajectoryV2SubjectIds,
} from './trajectory-v2-reducer.ts'
import type {
  TrajectoryV2EventProjection,
  TrajectoryV2Reducer,
} from './trajectory-v2-reducer.ts'
export { createTrajectoryV2Reducer, reduceTrajectoryV2 } from './trajectory-v2-reducer.ts'
import type {
  NormalizedTrajectoryAttributes, NormalizedTrajectoryStreamEvent,
} from './attribute-resolver.ts'
import {
  normalizeTrajectoryAttributes, normalizeTrajectoryStreamEvents,
} from './attribute-resolver.ts'
import type {
  OtlpExportTraceServiceRequest, OtlpSpan, OtlpSpanEvent,
} from '../shared/otlp.ts'
import type {
  TrajectoryPromptSnapshot,
  TrajectoryDiagnostic,
  TrajectoryGroupModel,
  TrajectoryRecordedFacts,
  TrajectoryRequest,
  TrajectoryRequestConfig,
  TrajectorySnapshot,
  TrajectorySystemMessage,
  TrajectoryToolSchema,
  TrajectoryTurnModel,
  TrajectoryUsage,
} from '../trajectory/model.ts'
import type {
  TrajectoryCell, TrajectorySourceBlock,
} from '../trajectory/record.ts'

interface ProjectedSpan {
  attributes: NormalizedTrajectoryAttributes
  endTimeUnixNano: bigint | undefined
  parentSpanId: string | undefined
  request: OtlpExportTraceServiceRequest
  identityRequestNumber: number | undefined
  requestNumber: number | undefined
  streamEvents: readonly NormalizedTrajectoryStreamEvent[]
  span: OtlpSpan
  startTimeUnixNano: bigint
  unresolvedAttributes: readonly string[]
  traceId: string
  turn: number
  turnKey: string
  lifecycle: 'running' | 'completed' | 'error'
  owningRequestRecordId?: string
}

/**
 * What retention left behind for one execution subject: the state the
 * session-wide derivations had reached over the turns it removed. Projecting
 * the remaining records from it renders every remaining turn exactly as it
 * rendered with the removed turns still loaded.
 */
export interface TrajectoryProjectionCheckpoint {
  turns: {
    /** Highest turn number a removed turn stated. */
    maxNumber: number
    /** Removed turns that were numbered after every stated number. */
    unnumbered: number
    /** Turn ids removed turns stated, by trace still holding records. */
    traceTurnIds: ReadonlyMap<string, readonly string[]>
  }
  /** Model requests removed, by `conversation\u0000subject`. */
  requestOffsets: ReadonlyMap<string, number>
  /** Removed requests later schema-v1 requests are still compared against, oldest first. */
  lineage: readonly TrajectoryLineageSeed[]
}

export interface TrajectoryLineageSeed {
  record: OtlpExportTraceServiceRequest
  /** Whether a schema-v2 commit handled the request, so only its output carries on. */
  handledByV2: boolean
}

export interface TrajectoryProjectionOptions {
  checkpoint?: TrajectoryProjectionCheckpoint
  lifecycleByRecordId?: ReadonlyMap<string, 'running' | 'completed' | 'error'>
  sessionCumulativeUsageByRequestIdentity?: ReadonlyMap<string, TrajectoryUsage>
  /**
   * Attribute keys whose addressed content could not be rebuilt, by
   * `traceId:spanId`.
   *
   * Records state their long attributes by reference and the content is
   * fetched separately. When it cannot be had -- the store aged it out -- the
   * attribute is absent for a reason worth telling apart from a model that
   * said nothing.
   */
  unresolvedAttributesByRecordId?: ReadonlyMap<string, readonly string[]>
  v2Reducer?: TrajectoryV2Reducer
}

/** Shared empty list, so a span with nothing missing keeps a stable identity. */
const NO_UNRESOLVED_ATTRIBUTES: readonly string[] = []

interface StructuredPart {
  arguments?: unknown
  content?: string
  id?: string
  name?: string
  response?: unknown
  type: string
}

interface StructuredMessage {
  inputIndex?: number
  inputKind?: 'prompt_attachment'
  parts: readonly StructuredPart[]
  promptAttachmentHistoryMode?: 'snapshot' | 'delta'
  role: string
}

interface MutableGroup {
  title: string
  description?: string
  cells: TrajectoryCell[]
  order: number
  step: number
}

interface MutableTurn {
  turn: number
  groups: Map<string, MutableGroup>
}

// What a trace states about the turn it belongs to: the identity that decides
// which spans are one turn, and the number that turn is shown as.
interface TurnFact {
  key: string
  number: number
  // Whether the turn's traces named it by id or number. A trace naming
  // neither ran outside every turn, such as a manual /compact.
  stated: boolean
  // When the earliest record of the turn began.
  startedAt: bigint
}

// The turns every record of one projection belongs to, resolved once so legacy
// spans and schema-v2 events land on one axis.
interface TurnIndex {
  byKey: ReadonlyMap<string, TurnFact>
  // The turn one record belongs to, by its own span identity.
  forRecord(traceId: string, spanId: string): TurnFact | undefined
  // The turn a record states by id, or its trace's turn when it states none.
  forTurn(traceId: string, turnId: string | undefined): TurnFact | undefined
}

// One rendered turn entry and when its turn began, so a run outside every
// turn can be placed between the turns it happened between.
interface TimedTurnEntry {
  model: TrajectoryTurnModel
  startedAt: bigint
}

interface InferenceInputProjection {
  messages: readonly StructuredMessage[]
  prompts: readonly PromptCellProjection[]
}

interface PromptCellProjection {
  prompt: TrajectoryPromptSnapshot
  previous?: TrajectoryPromptSnapshot
  systemMessageIndex?: number
  text: string
}

interface ToolResultFact {
  id?: string
  name?: string
  response: unknown
  traceId: string
}

interface IndexedMessage {
  inputIndex: number
  message: StructuredMessage
}

const NANOSECONDS_PER_MILLISECOND = 1_000_000n
const NANOSECONDS_PER_SECOND = 1_000_000_000n
const HASH_MASK = (1n << 52n) - 1n
const KNOWN_OPERATIONS = new Set<string>(Object.values(GEN_AI_OPERATIONS))

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function parsedJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown
  } catch {
    return value
  }
}

function nonNegativeSafeInteger(value: bigint | undefined): number | undefined {
  if (value === undefined || value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) return undefined
  return Number(value)
}

function positiveSafeInteger(value: bigint | undefined): number | undefined {
  const safe = nonNegativeSafeInteger(value)
  return safe === undefined || safe === 0 ? undefined : safe
}

function spansOf(record: OtlpExportTraceServiceRequest): readonly OtlpSpan[] {
  return record.resourceSpans.flatMap(resource =>
    (resource.scopeSpans ?? []).flatMap(scope => scope.spans ?? []))
}

function soleSpan(record: OtlpExportTraceServiceRequest): OtlpSpan {
  const spans = spansOf(record)
  if (spans.length !== 1) {
    throw new TypeError(`Trajectory records require exactly one Span; received ${spans.length}`)
  }
  const span = spans[0]
  if (span === undefined) throw new TypeError('Trajectory record contains no Span')
  return span
}

function compareBigint(left: bigint, right: bigint): number {
  return left < right ? -1 : left > right ? 1 : 0
}

function compareSpans(left: ProjectedSpan, right: ProjectedSpan): number {
  return compareBigint(left.startTimeUnixNano, right.startTimeUnixNano)
    || left.traceId.localeCompare(right.traceId)
    || left.span.spanId.localeCompare(right.span.spanId)
}

function stableIndex(identity: string): number {
  let hash = 0xcbf29ce484222325n
  for (const character of identity) {
    hash ^= BigInt(character.codePointAt(0) ?? 0)
    hash = (hash * 0x100000001b3n) & 0xffffffffffffffffn
  }
  return Number(hash & HASH_MASK)
}

function recordIdentity(span: ProjectedSpan, suffix: string): string {
  return `${span.traceId}:${span.span.spanId}:${suffix}`
}

function cellIndex(span: ProjectedSpan, suffix: string): number {
  return stableIndex(recordIdentity(span, suffix))
}

function requestRecordIdentity(span: ProjectedSpan): string | undefined {
  if (span.owningRequestRecordId !== undefined) return span.owningRequestRecordId
  if (span.attributes.inferenceId !== undefined) {
    return `${span.traceId}:inference:${span.attributes.inferenceId}`
  }
  if (span.identityRequestNumber !== undefined) {
    return `${span.traceId}:request:${span.identityRequestNumber}`
  }
  return isInference(span) ? `${span.traceId}:${span.span.spanId}:request` : undefined
}

function startedAt(span: ProjectedSpan): number {
  return Number(span.startTimeUnixNano / NANOSECONDS_PER_MILLISECOND)
}

function endedAt(span: ProjectedSpan): number | undefined {
  if (span.endTimeUnixNano === undefined) return undefined
  return Number(span.endTimeUnixNano / NANOSECONDS_PER_MILLISECOND)
}

function durationSeconds(span: ProjectedSpan): number | null {
  if (span.endTimeUnixNano === undefined) return null
  return Number(span.endTimeUnixNano - span.startTimeUnixNano) / Number(NANOSECONDS_PER_SECOND)
}

function formatted(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2) ?? String(value)
}

// Keyword arguments the runner injects into every tool invocation. They are
// call context, not model output, so a recorded invocation that carries only
// these beside the arguments unwraps to the arguments themselves.
const INJECTED_TOOL_KWARGS = new Set(['session', '_tool_callback_context'])

/**
 * The recorded arguments of one tool call, as the model sent them.
 *
 * Older runs recorded the invocation signature the harness captured — the
 * runner's ``(args, kwargs)`` call — so their spans carry the arguments
 * buried in ``[args, kwargs]`` beside injected call context. Unwrap that one
 * shape; anything else stays as recorded.
 */
/**
 * The recorded arguments of one tool call, as the model sent them.
 *
 * Older runs recorded the invocation signature the harness captured — the
 * runner's ``(args, kwargs)`` call — so their spans carry the arguments
 * buried in ``[args, kwargs]`` beside injected call context. Unwrap that one
 * shape; anything else stays as recorded.
 */
function toolArgumentsDetail(value: unknown): string | undefined {
  if (value === undefined) return undefined
  const positional = Array.isArray(value) && value.length === 2 && Array.isArray(value[0])
    ? value[0]
    : undefined
  const kwargs = Array.isArray(value) && value.length === 2 ? object(value[1]) : undefined
  if (
    positional !== undefined
    && kwargs !== undefined
    && positional.length === 1
    && Object.keys(kwargs).every(key => INJECTED_TOOL_KWARGS.has(key))
  ) {
    return formatted(positional[0])
  }
  return formatted(value)
}

function structuralIdentity(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(structuralIdentity).join(',')}]`
  const valueObject = object(value)
  if (valueObject === undefined) return JSON.stringify(value) ?? String(value)
  return `{${Object.keys(valueObject).sort().map(key => (
    `${JSON.stringify(key)}:${structuralIdentity(valueObject[key])}`
  )).join(',')}}`
}

function structuredParts(value: unknown): readonly StructuredPart[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((candidate): StructuredPart[] => {
    const part = object(candidate)
    if (part === undefined || typeof part.type !== 'string') return []
    return [{
      type: part.type,
      ...(typeof part.content === 'string' ? { content: part.content } : {}),
      ...(typeof part.id === 'string' ? { id: part.id } : {}),
      ...(typeof part.name === 'string' ? { name: part.name } : {}),
      ...(part.arguments === undefined ? {} : { arguments: part.arguments }),
      ...(part.response === undefined ? {} : { response: part.response }),
    }]
  })
}

function structuredMessages(value: unknown): readonly StructuredMessage[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((candidate): StructuredMessage[] => {
    const message = object(candidate)
    if (message === undefined || typeof message.role !== 'string') return []
    const openjiuwen = object(message.openjiuwen)
    const historyMode = openjiuwen?.kind === 'prompt_attachment_history'
      && (openjiuwen.mode === 'snapshot' || openjiuwen.mode === 'delta')
      ? openjiuwen.mode
      : undefined
    return [{
      role: message.role,
      parts: structuredParts(message.parts),
      ...(historyMode === undefined ? {} : { promptAttachmentHistoryMode: historyMode }),
    }]
  })
}

function sourceBlocks(parts: readonly StructuredPart[]): readonly TrajectorySourceBlock[] {
  return parts.flatMap((part): TrajectorySourceBlock[] => {
    switch (part.type) {
      case 'text':
      case 'reasoning':
      case 'compaction':
        return [{ type: part.type, content: part.content ?? '' }]
      case 'tool_call':
        return [{
          type: 'tool-call',
          content: formatted(part.arguments ?? {}),
          ...(part.id === undefined ? {} : { callId: part.id }),
          ...(part.name === undefined ? {} : { toolName: part.name }),
        }]
      case 'tool_call_response':
        return [{
          type: 'tool-result',
          content: formatted(part.response),
          ...(part.id === undefined ? {} : { callId: part.id }),
          ...(part.name === undefined ? {} : { toolName: part.name }),
        }]
      default:
        return [{ type: part.type, content: part.content ?? formatted(part) }]
    }
  })
}

function streamParts(events: readonly NormalizedTrajectoryStreamEvent[]): readonly StructuredPart[] {
  const parts: StructuredPart[] = []
  const indexByIdentity = new Map<string, number>()
  const append = (identity: string, create: () => StructuredPart, delta: string | undefined) => {
    const existingIndex = indexByIdentity.get(identity)
    if (existingIndex === undefined) {
      const part = create()
      if (delta !== undefined) part.content = delta
      indexByIdentity.set(identity, parts.length)
      parts.push(part)
      return
    }
    if (delta === undefined) return
    const existing = parts[existingIndex]
    if (existing !== undefined) existing.content = `${existing.content ?? ''}${delta}`
  }
  for (const event of events) {
    if (event.kind === 'text-delta') {
      append('text', () => ({ type: 'text' }), event.text)
      continue
    }
    if (event.kind === 'reasoning-delta') {
      append('reasoning', () => ({ type: 'reasoning' }), event.text)
      continue
    }
    if (event.kind !== 'tool-call-delta') continue
    const identity = `tool:${event.toolCallId ?? event.toolName ?? 'unknown'}`
    const existingIndex = indexByIdentity.get(identity)
    if (existingIndex === undefined) {
      indexByIdentity.set(identity, parts.length)
      parts.push({
        type: 'tool_call',
        ...(event.toolCallId === undefined ? {} : { id: event.toolCallId }),
        ...(event.toolName === undefined ? {} : { name: event.toolName }),
        ...(event.argumentsDelta === undefined ? {} : { arguments: event.argumentsDelta }),
      })
      continue
    }
    if (event.argumentsDelta === undefined) continue
    const existing = parts[existingIndex]
    if (existing !== undefined) {
      existing.arguments = `${typeof existing.arguments === 'string' ? existing.arguments : ''}${event.argumentsDelta}`
    }
  }
  return parts
}

function partText(parts: readonly StructuredPart[], type: string): string | undefined {
  const text = parts.flatMap(part => part.type === type && part.content !== undefined
    ? [part.content]
    : []).join('\n\n')
  return text === '' ? undefined : text
}

function knownModel(...values: readonly (string | undefined)[]): string | undefined {
  return values.find(value => (
    value !== undefined
    && value.trim() !== ''
    && value.trim().toLowerCase() !== 'unknown'
  ))
}

function requestConfig(attributes: NormalizedTrajectoryAttributes): TrajectoryRequestConfig {
  const provider = attributes.providerName ?? 'unknown'
  const model = knownModel(attributes.requestModel, attributes.responseModel) ?? 'unknown'
  const stop = attributes.requestStopSequences
  const purpose = attributes.requestPurpose
  const reasoningEffort = attributes.requestReasoningLevel
  const temperature = attributes.requestTemperature
  const topP = attributes.requestTopP
  const stream = attributes.requestStream
  const maxTokens = nonNegativeSafeInteger(attributes.requestMaxTokens)
  return {
    provider,
    model,
    ...(purpose === undefined ? {} : { purpose }),
    ...(reasoningEffort === undefined ? {} : { reasoningEffort }),
    ...(temperature === undefined ? {} : { temperature }),
    ...(topP === undefined ? {} : { topP }),
    ...(maxTokens === undefined ? {} : { maxTokens }),
    ...(stop === undefined ? {} : { stop }),
    ...(stream === undefined ? {} : { stream }),
  }
}

function nonEmpty<T extends object>(value: T): T | undefined {
  return Object.keys(value).length === 0 ? undefined : value
}

function recordedFacts(
  attributes: NormalizedTrajectoryAttributes,
  rootAttributes: NormalizedTrajectoryAttributes | undefined,
): TrajectoryRecordedFacts | undefined {
  const root = rootAttributes ?? attributes
  const correlation = nonEmpty({
    ...(attributes.conversationId === undefined && root.conversationId === undefined
      ? {}
      : { sessionId: attributes.conversationId ?? root.conversationId }),
    ...(attributes.requestId === undefined && root.requestId === undefined
      ? {}
      : { requestId: attributes.requestId ?? root.requestId }),
    ...(attributes.runId === undefined && root.runId === undefined
      ? {}
      : { runId: attributes.runId ?? root.runId }),
    ...(attributes.turnId === undefined && root.turnId === undefined
      ? {}
      : { turnId: attributes.turnId ?? root.turnId }),
  })
  const agent = nonEmpty({
    ...(attributes.agentId === undefined ? {} : { id: attributes.agentId }),
    ...(attributes.agentName === undefined ? {} : { name: attributes.agentName }),
    ...(attributes.agentVersion === undefined ? {} : { version: attributes.agentVersion }),
    ...(attributes.agentDescription === undefined ? {} : { description: attributes.agentDescription }),
    ...(attributes.agentMode === undefined && root.agentMode === undefined
      ? {}
      : { mode: attributes.agentMode ?? root.agentMode }),
  })
  const response = nonEmpty({
    ...(attributes.responseId === undefined ? {} : { id: attributes.responseId }),
    ...(attributes.responseModel === undefined ? {} : { model: attributes.responseModel }),
    ...(attributes.responseFinishReasons === undefined
      ? {}
      : { finishReasons: attributes.responseFinishReasons }),
    ...(attributes.totalLatencyMs === undefined ? {} : { totalLatencyMs: attributes.totalLatencyMs }),
    ...(attributes.timePerOutputTokenMs === undefined
      ? {}
      : { timePerOutputTokenMs: attributes.timePerOutputTokenMs }),
    ...(attributes.promptTokenIds === undefined ? {} : { promptTokenIds: attributes.promptTokenIds }),
    ...(attributes.completionTokenIds === undefined
      ? {}
      : { completionTokenIds: attributes.completionTokenIds }),
    ...(attributes.logprobs === undefined ? {} : { logprobs: attributes.logprobs }),
    ...(attributes.parserResult === undefined ? {} : { parserResult: attributes.parserResult }),
    ...(attributes.providerMetadata === undefined
      ? {}
      : { providerMetadata: attributes.providerMetadata }),
  })
  const cost = nonEmpty({
    ...(attributes.inputCost === undefined ? {} : { input: attributes.inputCost }),
    ...(attributes.outputCost === undefined ? {} : { output: attributes.outputCost }),
    ...(attributes.totalCost === undefined ? {} : { total: attributes.totalCost }),
  })
  const trace = nonEmpty({
    ...(root.traceRoot === undefined ? {} : { root: root.traceRoot }),
    ...(root.trajectorySchemaVersion === undefined
      ? attributes.trajectorySchemaVersion === undefined
        ? {}
        : { schemaVersion: attributes.trajectorySchemaVersion }
      : { schemaVersion: root.trajectorySchemaVersion }),
    ...(root.traceComplete === undefined ? {} : { complete: root.traceComplete }),
    ...(root.traceForcedClose === undefined ? {} : { forcedClose: root.traceForcedClose }),
  })
  return nonEmpty({
    ...(correlation === undefined ? {} : { correlation }),
    ...(agent === undefined ? {} : { agent }),
    ...(response === undefined ? {} : { response }),
    ...(cost === undefined ? {} : { cost }),
    ...(trace === undefined ? {} : { trace }),
  })
}

function toolSchemas(value: unknown): readonly TrajectoryToolSchema[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((candidate): TrajectoryToolSchema[] => {
    const tool = object(candidate)
    if (tool === undefined) return []
    const functionDefinition = object(tool.function)
    const definition = functionDefinition ?? tool
    if (typeof definition.name !== 'string') return []
    const parameters = definition.parameters
    return [{
      name: definition.name,
      description: typeof definition.description === 'string' ? definition.description : '',
      parameters: (typeof parameters === 'object' && parameters !== null)
        ? parameters as object | unknown[]
        : {},
    }]
  })
}

function promptSnapshot(attributes: NormalizedTrajectoryAttributes): TrajectoryPromptSnapshot | undefined {
  const instructions = structuredParts(attributes.systemInstructions)
  const requestSystemMessages: TrajectorySystemMessage[] = []
  let promptAttachmentSlot: number | undefined
  structuredMessages(attributes.requestMessages).forEach((message, index) => {
    if (message.role !== 'system') return
    const content = message.parts
      .flatMap(part => part.content === undefined ? [] : [part.content])
      .join('\n')
    const historyMode = message.promptAttachmentHistoryMode
      ?? promptAttachmentHistoryModeFromContent(content)
    if (historyMode === 'snapshot') {
      promptAttachmentSlot ??= index
    }
    if (historyMode === 'delta' && promptAttachmentSlot !== undefined) {
      const slotIndex = requestSystemMessages.findIndex(candidate => (
        candidate.index === promptAttachmentSlot
      ))
      const replacement = { index: promptAttachmentSlot, content }
      if (slotIndex === -1) requestSystemMessages.push(replacement)
      else requestSystemMessages[slotIndex] = replacement
      return
    }
    requestSystemMessages.push({ index, content })
  })
  const systemMessages = requestSystemMessages.length > 0
    ? requestSystemMessages
    : instructions.length === 0
      ? []
      : [{
          index: 0,
          content: instructions
            .flatMap(part => part.content === undefined ? [] : [part.content])
            .join('\n\n'),
        }]
  const tools = toolSchemas(attributes.toolDefinitions)
  if (systemMessages.length === 0 && tools.length === 0) return undefined
  return {
    config: requestConfig(attributes),
    system: systemMessages.map(message => message.content).join('\n\n'),
    systemMessages,
    tools,
  }
}

function promptAttachmentHistoryModeFromContent(
  content: string,
): 'snapshot' | 'delta' | undefined {
  if (
    content.startsWith('以下动态上下文当前有效')
    || content.startsWith('The following dynamic context is currently active.')
  ) return 'snapshot'
  if (
    content.startsWith('以下动态上下文已经变化')
    || content.startsWith('The following dynamic context has changed.')
  ) return 'delta'
  return undefined
}

function usage(attributes: NormalizedTrajectoryAttributes): TrajectoryUsage {
  const input = nonNegativeSafeInteger(attributes.usageInputTokens)
  const cacheRead = nonNegativeSafeInteger(attributes.usageCacheReadTokens)
  const cacheWrite = nonNegativeSafeInteger(attributes.usageCacheWriteTokens)
  const output = nonNegativeSafeInteger(attributes.usageOutputTokens)
  const reasoning = nonNegativeSafeInteger(attributes.usageReasoningTokens)
  const rawTotal = input === undefined || output === undefined ? undefined : input + output
  const total = rawTotal !== undefined && Number.isSafeInteger(rawTotal) ? rawTotal : undefined
  return {
    ...(input === undefined ? {} : { input }),
    ...(cacheRead === undefined ? {} : { cacheRead }),
    ...(cacheWrite === undefined ? {} : { cacheWrite }),
    ...(output === undefined ? {} : { output }),
    ...(reasoning === undefined ? {} : { reasoning }),
    ...(total === undefined ? {} : { total }),
  }
}

function status(projected: ProjectedSpan): 'complete' | 'running' | 'error' {
  if (projected.lifecycle === 'running') return 'running'
  if (projected.lifecycle === 'error') return 'error'
  return projected.span.status?.code === 2 || projected.attributes.spanForcedClose === true
    ? 'error'
    : 'complete'
}

function validErrorText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed === '' ? undefined : trimmed
}

function firstExceptionReason(
  events: readonly OtlpSpanEvent[] | undefined,
): string | undefined {
  const exceptionEvents = (events ?? []).filter(event => event.name === 'exception')
  for (const attributeKey of [
    STANDARD_ATTRIBUTES.exceptionMessage,
    STANDARD_ATTRIBUTES.exceptionType,
  ]) {
    for (const exception of exceptionEvents) {
      const reason = validErrorText(
        readStringAttribute(exactAttributeMap(exception.attributes), attributeKey),
      )
      if (reason !== undefined) return reason
    }
  }
  return undefined
}

function statusError(projected: ProjectedSpan): string | undefined {
  const statusIsError = projected.span.status?.code === 2
  const forcedClose = projected.attributes.spanForcedClose === true
  if (!statusIsError && !forcedClose && projected.lifecycle !== 'error') return undefined
  return validErrorText(statusIsError ? projected.span.status?.message : undefined)
    ?? firstExceptionReason(projected.span.events)
    ?? validErrorText(statusIsError ? projected.attributes.errorType : undefined)
    ?? (forcedClose
      ? validErrorText(projected.attributes.spanForcedCloseReason)
        ?? 'Span was force-closed during trace finalization'
      : undefined)
    ?? (projected.lifecycle === 'error' ? 'Trajectory operation reported an error' : undefined)
    ?? 'OpenTelemetry Span reported an error'
}

function operation(span: ProjectedSpan): string | undefined {
  return span.attributes.operationName
}

function knownOperation(span: ProjectedSpan): string | undefined {
  const name = operation(span)
  return name !== undefined && KNOWN_OPERATIONS.has(name) ? name : undefined
}

function recordKind(span: ProjectedSpan): string | undefined {
  return span.attributes.trajectoryKind
}

function isInference(span: ProjectedSpan): boolean {
  const kind = recordKind(span)
  if (kind !== undefined) return kind === 'inference'
  const operationName = operation(span)
  if (operationName !== undefined) return operationName === GEN_AI_OPERATIONS.chat
    || operationName === GEN_AI_OPERATIONS.generateContent
    || operationName === GEN_AI_OPERATIONS.textCompletion
  const observationType = span.attributes.langfuseObservationType
  if (observationType !== undefined) return observationType === 'generation'
  return span.span.name === 'llm.call' && (
    span.attributes.inferenceId !== undefined
      || span.attributes.inputMessages !== undefined
      || span.attributes.outputMessages !== undefined
  )
}

function isTool(span: ProjectedSpan): boolean {
  const kind = recordKind(span)
  if (kind !== undefined) return kind === 'tool'
  const name = knownOperation(span)
  if (name !== undefined) return name === GEN_AI_OPERATIONS.executeTool
  if (span.attributes.langfuseObservationType === 'tool') return true
  return span.span.name.startsWith('tool.') || span.span.name.startsWith('execute_tool')
}

function isRoutedAskUserResult(
  span: ProjectedSpan,
  root: ProjectedSpan | undefined,
): boolean {
  const attributes = span.attributes
  const conversationId = attributes.conversationId
  const subjectId = attributes.executionSubjectId
  const subjectKind = attributes.executionSubjectKind
  const subjectSessionId = attributes.executionSubjectSessionId
  const mainSubject = subjectId === 'main'
    && subjectKind === 'main_agent'
    && subjectSessionId === conversationId
  const subagentSubject = subjectKind === 'subagent'
    && attributes.executionSubjectParentId !== undefined
    && subjectSessionId !== undefined
    && conversationId !== undefined
    && (subjectSessionId === conversationId
      || subjectSessionId.startsWith(`${conversationId}_sub_`))
  const rootAttributes = root?.attributes
  const sameRootOwner = rootAttributes !== undefined
    && rootAttributes.conversationId === conversationId
    && rootAttributes.executionSubjectId === subjectId
    && rootAttributes.executionSubjectKind === subjectKind
    && rootAttributes.executionSubjectSessionId === subjectSessionId
  return attributes.toolAuthoritative === true
    && span.attributes.toolName === 'ask_user'
    && attributes.toolCallId !== undefined
    && attributes.toolCallResult !== undefined
    && conversationId !== undefined
    && subjectId !== undefined
    && attributes.requestId !== undefined
    && (mainSubject || subagentSubject)
    && sameRootOwner
}

function spanCellBase(span: ProjectedSpan, suffix: string): Pick<
  TrajectoryCell,
  'index' | 'recordId' | 'sourceSeq' | 'startedAt' | 'status' | 'timeSeconds' | 'traceDetail'
> & Pick<TrajectoryCell, 'requestRecordId'> {
  return {
    index: cellIndex(span, suffix),
    recordId: recordIdentity(span, suffix),
    startedAt: startedAt(span),
    timeSeconds: durationSeconds(span),
    status: status(span),
    traceDetail: span.request,
    ...(requestRecordIdentity(span) === undefined
      ? {}
      : { requestRecordId: requestRecordIdentity(span) }),
  }
}

// How a turn that ended in failure is shown. A native run records the failure
// on the record that hit it — the inference or tool whose span carries the
// error — and the row for that record is drawn in error. An external CLI has
// no such record: the call the gateway throttled produced no response body, so
// nothing was ever reported for it, and the only place the failure is stated
// is the span the turn itself ran on. That span is therefore shown too, at the
// end of the turn it closes.
//
// `withInput` is for the turn that failed before its first model call: it has
// no rows at all, and a turn without rows is not drawn, so the numbers around
// it appear to skip with no sign that a turn ran. Its input is stated only on
// that same span; a turn that did reach a model call already shows what it was
// handed, on the call that read it.
function failedTurnCells(root: ProjectedSpan | undefined, withInput: boolean): TrajectoryCell[] {
  if (root === undefined) return []
  const reason = statusError(root)
  if (reason === undefined) return []
  const cells: TrajectoryCell[] = []
  const input = root.attributes.spanInput
  if (withInput && input !== undefined && input.trim() !== '') {
    cells.push({
      ...spanCellBase(root, 'turn:input'),
      timeSeconds: null,
      status: 'complete',
      kind: 'user',
      text: input,
      inputDetail: input,
    })
  }
  cells.push({
    ...spanCellBase(root, 'turn:error'),
    // The span opened with the turn, so its own start would sort this row
    // ahead of everything the turn went on to do; it belongs where the turn
    // ended.
    startedAt: endedAt(root) ?? startedAt(root),
    kind: 'message',
    status: 'error',
    text: reason,
    outputDetail: reason,
    isError: true,
  })
  return cells
}

// Place a turn's own failure at the end of the turn, in the last group it
// reached, or in a group of its own when the turn never opened one.
function withTurnFailure(
  groups: readonly TrajectoryGroupModel[],
  cells: readonly TrajectoryCell[],
): TrajectoryGroupModel[] {
  if (cells.length === 0) return [...groups]
  if (groups.length === 0) return [{ title: 'Step 1', cells: [...cells] }]
  const last = groups[groups.length - 1]
  return [...groups.slice(0, -1), { ...last, cells: [...last.cells, ...cells] }]
}

function inputCells(
  span: ProjectedSpan,
  projection: InferenceInputProjection,
): TrajectoryCell[] {
  const cells: TrajectoryCell[] = []
  for (const promptProjection of projection.prompts) {
    cells.push({
      ...spanCellBase(
        span,
        promptProjection.systemMessageIndex === undefined
          ? 'system:tools'
          : `system:${promptProjection.systemMessageIndex}`,
      ),
      timeSeconds: null,
      kind: 'system',
      text: promptProjection.text,
      promptDetail: promptProjection.prompt,
      ...(promptProjection.systemMessageIndex === undefined
        ? {}
        : { promptSystemMessageIndex: promptProjection.systemMessageIndex }),
      ...(promptProjection.previous === undefined
        ? {}
        : { previousPromptDetail: promptProjection.previous }),
    })
  }
  projection.messages.forEach((message, index) => {
    const blocks = sourceBlocks(message.parts)
    const text = partText(message.parts, 'text')
      ?? partText(message.parts, 'compaction')
      ?? blocks.map(block => block.content).filter(Boolean).join('\n')
    const attachment = message.inputKind === 'prompt_attachment'
    const user = message.role === 'user' && !attachment
    cells.push({
      ...spanCellBase(span, `input:${message.inputIndex ?? index}`),
      timeSeconds: null,
      kind: user ? 'user' : 'context',
      text,
      inputDetail: text,
      sourceBlocks: blocks,
      messageSource: {
        role: message.role,
        kind: attachment ? 'prompt_attachment' : message.role,
        inputIndex: message.inputIndex ?? index,
        ...(attachment ? { scope: 'request' } : {}),
      },
    })
  })
  return cells
}

function assistantParts(span: ProjectedSpan): readonly StructuredPart[] {
  const messages = structuredMessages(span.attributes.outputMessages)
  const completedParts = messages.flatMap(message => message.parts)
  const replayParts = streamParts(span.streamEvents)
  const completedTypes = new Set(completedParts.map(part => part.type))
  return [
    ...completedParts,
    ...replayParts.filter(part => !completedTypes.has(part.type)),
  ]
}

function assistantCell(span: ProjectedSpan): TrajectoryCell {
  const parts = assistantParts(span)
  const blocks = sourceBlocks(parts)
  const output = partText(parts, 'text')
  const thinking = partText(parts, 'reasoning')
  const toolCalls = blocks.filter(block => block.type === 'tool-call')
  // An answer this reader could not rebuild is not an answer that was never
  // given. Saying so is the difference between a model that stayed silent and
  // content the store no longer holds.
  const outputExpired = span.unresolvedAttributes.includes(STANDARD_ATTRIBUTES.outputMessages)
  const text = output
    ?? thinking
    ?? (toolCalls.length > 0
      ? 'Tool call only'
      : outputExpired
        ? 'Output content is no longer stored'
        : span.lifecycle === 'running' ? 'Waiting for model response…' : 'No output content')
  const usageValue = usage(span.attributes)
  const firstChunkSeconds = span.attributes.responseTimeToFirstChunkSeconds
  const start = startedAt(span)
  const error = statusError(span)
  return {
    ...spanCellBase(span, 'assistant'),
    kind: 'message',
    text,
    ...(output === undefined ? {} : { outputDetail: output, previewMarkdown: output }),
    ...(thinking === undefined ? {} : { thinkingDetail: thinking }),
    sourceBlocks: blocks,
    outputBlocks: blocks.filter(block => block.type === 'text' || block.type === 'reasoning'),
    assistantMetrics: {
      timingRecorded: true,
      streaming: span.attributes.requestStream ?? null,
      stepStartTime: start,
      firstTokenTime: firstChunkSeconds === undefined ? null : start + firstChunkSeconds * 1_000,
      completedTime: span.endTimeUnixNano === undefined
        ? null
        : Number(span.endTimeUnixNano / NANOSECONDS_PER_MILLISECOND),
      usageProvided: Object.keys(usageValue).length > 0,
      outputTokens: usageValue.output ?? null,
    },
    ...(error === undefined ? {} : { isError: true, result: error }),
    ...(usageValue.input === undefined ? {} : { input: usageValue.input }),
    ...(usageValue.cacheRead === undefined ? {} : { cacheRead: usageValue.cacheRead }),
    ...(usageValue.cacheWrite === undefined ? {} : { cacheWrite: usageValue.cacheWrite }),
    ...(usageValue.output === undefined ? {} : { output: usageValue.output }),
    ...(usageValue.reasoning === undefined ? {} : { think: usageValue.reasoning }),
    ...(usageValue.total === undefined ? {} : { total: usageValue.total }),
  }
}

/**
 * Project one tool call.
 *
 * A tool call has two results. What the invocation returned is recorded on
 * the tool span; what the model was given is the tool message the harness
 * rendered from it, which can drop or reshape fields. The model's view is the
 * call's Result, since that is what the next step acted on, and the raw return
 * is kept beside it. A call whose tool message was never recorded has only
 * its raw return.
 *
 * Args:
 *   span: The tool span.
 *   toolSpanIds: Span ids of every tool span, to tell a nested call.
 *   schemaDetail: The tool's definition from its request, when recorded.
 *   modelResult: The tool message content the model read for this call.
 */
function toolCell(
  span: ProjectedSpan,
  toolSpanIds: ReadonlySet<string>,
  schemaDetail: string | undefined,
  modelResult: unknown,
): TrajectoryCell {
  const name = span.attributes.toolName ?? span.span.name
  const callId = span.attributes.toolCallId ?? `${span.traceId}:${span.span.spanId}`
  const input = span.attributes.toolCallArguments
  const rawResult = span.attributes.toolCallResult
  const inputText = toolArgumentsDetail(input)
  const modelResultText = modelResult === undefined ? undefined : formatted(modelResult)
  const rawResultText = rawResult === undefined ? undefined : formatted(rawResult)
  const resultText = modelResultText ?? rawResultText
  const nested = span.parentSpanId !== undefined && toolSpanIds.has(span.parentSpanId)
  const error = statusError(span)
  const description = span.attributes.toolDescription
  const toolMetadata = nonEmpty({
    ...(description === undefined ? {} : { description }),
    ...(span.attributes.toolType === undefined
      ? {}
      : { type: span.attributes.toolType }),
    ...(span.attributes.toolResourceId === undefined
      ? {}
      : { resourceId: span.attributes.toolResourceId }),
    ...(span.attributes.toolProtocol === undefined
      ? {}
      : { protocol: span.attributes.toolProtocol }),
    ...(span.attributes.toolAuthoritative === undefined
      ? {}
      : { authoritative: span.attributes.toolAuthoritative }),
  })
  const detail = schemaDetail === undefined && toolMetadata === undefined
    ? undefined
    : JSON.stringify({
      ...(schemaDetail === undefined ? {} : { definition: parsedJson(schemaDetail) }),
      ...(toolMetadata === undefined ? {} : { metadata: toolMetadata }),
    }, null, 2)
  return {
    ...spanCellBase(span, 'tool'),
    kind: nested ? 'subtool' : 'tool',
    ...(requestRecordIdentity(span) === undefined ? { requestless: true } : {}),
    text: inputText === undefined ? name : `${name} · ${inputText.replace(/\s+/g, ' ').slice(0, 160)}`,
    callId,
    ...(inputText === undefined ? {} : { inputDetail: inputText }),
    ...(modelResultText === undefined ? {} : { outputDetail: modelResultText }),
    ...(rawResultText === undefined ? {} : { rawOutputDetail: rawResultText }),
    // The row's preview prefers what the model read and falls back to the
    // raw return, so a call whose tool message was not recorded still shows.
    ...(resultText === undefined ? {} : { result: resultText }),
    ...(error === undefined ? {} : { isError: true, result: error }),
    ...(detail === undefined ? {} : { schemaDetail: detail }),
  }
}

function occurrenceTokens(messages: readonly IndexedMessage[]): readonly string[] {
  const occurrences = new Map<string, number>()
  return messages.map(({ message }) => {
    const identity = structuralIdentity(message)
    const occurrence = (occurrences.get(identity) ?? 0) + 1
    occurrences.set(identity, occurrence)
    return `${identity}\u0000${occurrence}`
  })
}

function lcsInsertedInputIndexes(
  previous: readonly IndexedMessage[],
  current: readonly IndexedMessage[],
): ReadonlySet<number> {
  const previousTokens = occurrenceTokens(previous)
  const currentTokens = occurrenceTokens(current)
  let prefix = 0
  while (
    prefix < Math.min(previousTokens.length, currentTokens.length)
    && previousTokens[prefix] === currentTokens[prefix]
  ) prefix += 1
  let suffix = 0
  const suffixLimit = Math.min(previousTokens.length, currentTokens.length) - prefix
  while (
    suffix < suffixLimit
    && previousTokens[previousTokens.length - suffix - 1]
      === currentTokens[currentTokens.length - suffix - 1]
  ) suffix += 1

  const previousEnd = previousTokens.length - suffix
  const currentEnd = currentTokens.length - suffix
  const previousLength = previousEnd - prefix
  const currentLength = currentEnd - prefix
  const lengths = Array.from(
    { length: previousLength + 1 },
    () => new Uint32Array(currentLength + 1),
  )
  for (let left = previousLength - 1; left >= 0; left -= 1) {
    for (let right = currentLength - 1; right >= 0; right -= 1) {
      lengths[left][right] = previousTokens[prefix + left] === currentTokens[prefix + right]
        ? 1 + lengths[left + 1][right + 1]
        : Math.max(lengths[left + 1][right], lengths[left][right + 1])
    }
  }

  const inserted = new Set<number>()
  let left = 0
  let right = 0
  while (left < previousLength && right < currentLength) {
    if (previousTokens[prefix + left] === currentTokens[prefix + right]) {
      left += 1
      right += 1
      continue
    }
    if (lengths[left + 1][right] >= lengths[left][right + 1]) {
      left += 1
      continue
    }
    const entry = current[prefix + right]
    if (entry !== undefined) inserted.add(entry.inputIndex)
    right += 1
  }
  while (right < currentLength) {
    const entry = current[prefix + right]
    if (entry !== undefined) inserted.add(entry.inputIndex)
    right += 1
  }
  return inserted
}

function legacyRequestScopedAttachment(message: StructuredMessage): boolean {
  return message.parts.some(part => (
    part.type === 'prompt_attachment'
    || (
      part.type === 'text'
      && part.content?.includes('<system-reminder>') === true
      && part.content.includes('<prompt-attachment')
    )
  ))
}

function provenanceAttachmentIndexes(
  value: unknown,
): ReadonlySet<number> | undefined {
  if (value === undefined) return undefined
  if (!Array.isArray(value)) return new Set<number>()
  const indexes = value.flatMap((candidate): number[] => {
    const provenance = object(candidate)
    if (
      provenance?.kind !== 'prompt_attachment'
      || provenance.scope !== 'request'
    ) return []
    const index = provenance.input_message_index
    if (typeof index === 'number' && Number.isSafeInteger(index) && index >= 0) return [index]
    if (typeof index !== 'string' || !/^\d+$/.test(index)) return []
    const parsed = Number(index)
    return Number.isSafeInteger(parsed) ? [parsed] : []
  })
  return new Set(indexes)
}

function spanIdentity(span: ProjectedSpan): string {
  return `${span.traceId}\u0000${span.span.spanId}`
}

function behaviorLineage(
  span: ProjectedSpan,
  spanByIdentity: ReadonlyMap<string, ProjectedSpan>,
): readonly string[] {
  const lineage: string[] = []
  const visited = new Set<string>()
  let parentSpanId = span.parentSpanId
  while (parentSpanId !== undefined) {
    const identity = `${span.traceId}\u0000${parentSpanId}`
    if (visited.has(identity)) break
    visited.add(identity)
    const parent = spanByIdentity.get(identity)
    if (parent === undefined) break
    if (isTool(parent)) lineage.push(parent.span.spanId)
    parentSpanId = parent.parentSpanId
  }
  return lineage.reverse()
}

function behaviorSessionKey(
  span: Pick<ProjectedSpan, 'attributes' | 'traceId'>,
  lineage: readonly string[],
): string {
  const conversation = span.attributes.conversationId ?? `trace:${span.traceId}`
  const subject = span.attributes.executionSubjectId ?? 'legacy-main'
  return `${conversation}\u0000${subject}\u0000${lineage.join('/')}`
}

function compareInferenceBehavior(left: ProjectedSpan, right: ProjectedSpan): number {
  return compareBigint(left.startTimeUnixNano, right.startTimeUnixNano)
    || (left.requestNumber ?? 0) - (right.requestNumber ?? 0)
    || left.traceId.localeCompare(right.traceId)
    || left.span.spanId.localeCompare(right.span.spanId)
}

function comparePhysicalInference(left: ProjectedSpan, right: ProjectedSpan): number {
  return compareBigint(left.startTimeUnixNano, right.startTimeUnixNano)
    || left.traceId.localeCompare(right.traceId)
    || left.span.spanId.localeCompare(right.span.spanId)
}

function executionSubjectKey(span: ProjectedSpan): string {
  const session = span.attributes.conversationId ?? 'legacy-session'
  const subject = span.attributes.executionSubjectId ?? 'legacy-main'
  return `${session}\u0000${subject}`
}

function assignSubjectRequestNumbers(
  spans: readonly ProjectedSpan[],
  requestOffsets: ReadonlyMap<string, number>,
): ProjectedSpan[] {
  const inferenceGroups = new Map<string, ProjectedSpan[]>()
  for (const span of spans) {
    if (!isInference(span)) continue
    const key = executionSubjectKey(span)
    const group = inferenceGroups.get(key) ?? []
    group.push(span)
    inferenceGroups.set(key, group)
  }
  const displayNumberByIdentity = new Map<string, number>()
  for (const [key, group] of inferenceGroups) {
    // Requests retention removed still count: numbering continues after them.
    const offset = requestOffsets.get(key) ?? 0
    const ordered = [...group].sort(comparePhysicalInference)
    const explicitNumbers = ordered.map(span => (
      positiveSafeInteger(span.attributes.executionSubjectRequestNumber)
    ))
    const hasCompleteMonotonicSequence = explicitNumbers.every((number, index) => (
      number === offset + index + 1
    ))
    for (const [index, span] of ordered.entries()) {
      // A partially upgraded or malformed subject must be rebuilt as one
      // chronological sequence. Mixing explicit and inferred values can
      // otherwise create duplicates or gaps while a live archive is loading.
      const number = hasCompleteMonotonicSequence
        ? explicitNumbers[index] as number
        : offset + index + 1
      displayNumberByIdentity.set(spanIdentity(span), number)
    }
  }
  return spans.map(span => (
    !isInference(span)
      ? span
      : { ...span, requestNumber: displayNumberByIdentity.get(spanIdentity(span)) ?? 1 }
  ))
}

function consumePriorOutput(
  pending: StructuredMessage[],
  message: StructuredMessage,
): boolean {
  if (message.role !== 'assistant') return false
  const index = pending.findIndex(candidate => outputReplayMatches(candidate, message))
  if (index < 0) return false
  pending.splice(index, 1)
  return true
}

function partContents(message: StructuredMessage, type: string): readonly string[] {
  return message.parts.flatMap(part => (
    part.type === type && part.content !== undefined && part.content !== ''
      ? [part.content]
      : []
  ))
}

function sameSequence(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function stableToolArguments(value: unknown): string | undefined {
  if (value === undefined) return undefined
  if (typeof value === 'string' && value.trim() === '') return undefined
  if (typeof value !== 'string') return structuralIdentity(value)
  const parsed = parsedJson(value)
  return structuralIdentity(parsed)
}

function toolCallsMatch(output: StructuredPart, input: StructuredPart): boolean {
  if (output.id !== undefined && input.id !== undefined) {
    if (output.id !== input.id) return false
    if (output.name !== undefined && input.name !== undefined && output.name !== input.name) return false
    const outputArguments = stableToolArguments(output.arguments)
    const inputArguments = stableToolArguments(input.arguments)
    return outputArguments === undefined
      || inputArguments === undefined
      || outputArguments === inputArguments
  }
  if (output.name === undefined || input.name === undefined || output.name !== input.name) return false
  const outputArguments = stableToolArguments(output.arguments)
  const inputArguments = stableToolArguments(input.arguments)
  return outputArguments !== undefined
    && inputArguments !== undefined
    && outputArguments === inputArguments
}

function replayToolCalls(message: StructuredMessage): readonly StructuredPart[] {
  const ids = new Set<string>()
  return message.parts.flatMap((part): StructuredPart[] => {
    if (part.type !== 'tool_call') return []
    if (part.id === undefined) return [part]
    if (ids.has(part.id)) return []
    ids.add(part.id)
    return [part]
  })
}

function outputReplayMatches(
  output: StructuredMessage,
  input: StructuredMessage,
): boolean {
  if (output.role !== 'assistant' || input.role !== 'assistant') return false
  const outputText = partContents(output, 'text')
  const inputText = partContents(input, 'text')
  const hasText = outputText.length > 0 || inputText.length > 0
  if (hasText && !sameSequence(outputText, inputText)) return false

  const outputReasoning = partContents(output, 'reasoning')
  const inputReasoning = partContents(input, 'reasoning')
  if (
    outputReasoning.length > 0
    && inputReasoning.length > 0
    && !sameSequence(outputReasoning, inputReasoning)
  ) return false

  const outputCalls = replayToolCalls(output)
  const inputCalls = replayToolCalls(input)
  if (outputCalls.length > 0 && inputCalls.length > 0) {
    if (outputCalls.length !== inputCalls.length) return false
    if (!outputCalls.every((part, index) => {
      const inputPart = inputCalls[index]
      return inputPart !== undefined && toolCallsMatch(part, inputPart)
    })) return false
  }
  if (hasText) return true

  if (outputCalls.length === 0 || inputCalls.length === 0) return false
  return outputCalls.every((part, index) => (
    part.id !== undefined && part.id === inputCalls[index]?.id
  ))
}

function promptBehaviorIdentity(prompt: TrajectoryPromptSnapshot): string {
  return structuralIdentity({ systemMessages: prompt.systemMessages, tools: prompt.tools })
}

function promptCellProjections(
  prompt: TrajectoryPromptSnapshot | undefined,
  previous: TrajectoryPromptSnapshot | undefined,
): readonly PromptCellProjection[] {
  if (prompt === undefined) return []
  if (previous === undefined) {
    if (prompt.systemMessages.length > 0) {
      return prompt.systemMessages.map(message => ({
        prompt,
        systemMessageIndex: message.index,
        text: message.content,
      }))
    }
    return [{
      prompt,
      text: `${prompt.tools.length} tool definition${prompt.tools.length === 1 ? '' : 's'}`,
    }]
  }
  if (promptBehaviorIdentity(prompt) === promptBehaviorIdentity(previous)) return []

  const beforeByIndex = new Map(previous.systemMessages.map(message => [message.index, message]))
  const afterByIndex = new Map(prompt.systemMessages.map(message => [message.index, message]))
  const indexes = [...new Set([...beforeByIndex.keys(), ...afterByIndex.keys()])]
    .sort((left, right) => left - right)
  const changed = indexes.flatMap((index): PromptCellProjection[] => {
    const before = beforeByIndex.get(index)
    const after = afterByIndex.get(index)
    if (before?.content === after?.content) return []
    return [{
      prompt,
      previous,
      systemMessageIndex: index,
      text: after?.content ?? 'System message removed',
    }]
  })
  if (structuralIdentity(previous.tools) !== structuralIdentity(prompt.tools)) {
    changed.push({
      prompt,
      previous,
      text: `${prompt.tools.length} tool definition${prompt.tools.length === 1 ? '' : 's'}`,
    })
  }
  return changed
}

interface InferenceInputMessages {
  /** Every input message the request states. */
  currentInputs: readonly StructuredMessage[]
  /** Indexes of the request-scoped prompt attachments among them. */
  attachmentIndexSet: ReadonlySet<number>
  /** The ordinary messages a later request of the lineage is compared against. */
  currentOrdinary: readonly IndexedMessage[]
}

function inferenceInputMessages(attributes: NormalizedTrajectoryAttributes): InferenceInputMessages {
  const currentInputs = structuredMessages(attributes.inputMessages)
  const attachmentIndexes = provenanceAttachmentIndexes(attributes.inputMessageProvenance)
  const attachmentIndexSet = new Set(currentInputs.flatMap((message, index): number[] => {
    const attachment = attachmentIndexes === undefined
      ? legacyRequestScopedAttachment(message)
      : attachmentIndexes.has(index)
    return attachment ? [index] : []
  }))
  const currentOrdinary = currentInputs.flatMap((message, inputIndex): IndexedMessage[] => (
    attachmentIndexSet.has(inputIndex) ? [] : [{ inputIndex, message }]
  ))
  return { currentInputs, attachmentIndexSet, currentOrdinary }
}

function projectInferenceInputs(
  spans: readonly ProjectedSpan[],
  v2InferenceIds: ReadonlySet<string>,
  lineageSeeds: readonly TrajectoryLineageSeed[],
): {
  inputsBySpanId: ReadonlyMap<string, InferenceInputProjection>
  toolResultById: ReadonlyMap<string, ToolResultFact>
  toolResultBySpanId: ReadonlyMap<string, ToolResultFact>
  diagnostics: readonly TrajectoryDiagnostic[]
} {
  const inputsBySpanId = new Map<string, InferenceInputProjection>()
  const previousInputsBySession = new Map<string, readonly IndexedMessage[]>()
  const previousPromptBySession = new Map<string, TrajectoryPromptSnapshot>()
  const previousOutputsBySession = new Map<string, readonly StructuredMessage[]>()
  const toolResults: ToolResultFact[] = []
  const diagnostics: TrajectoryDiagnostic[] = []
  const spanByIdentity = new Map(spans.map(span => [spanIdentity(span), span]))

  // Requests retention removed leave what the next request of their root
  // lineage is compared against, and nothing else: they render no cell and
  // lend no tool result.
  for (const seed of lineageSeeds) {
    const span = soleSpan(seed.record)
    const attributes = normalizeTrajectoryAttributes(span.attributes)
    const sessionKey = behaviorSessionKey({ attributes, traceId: span.traceId }, [])
    previousOutputsBySession.set(sessionKey, structuredMessages(attributes.outputMessages))
    if (seed.handledByV2) continue
    const prompt = promptSnapshot(attributes)
    if (prompt !== undefined) previousPromptBySession.set(sessionKey, prompt)
    previousInputsBySession.set(sessionKey, inferenceInputMessages(attributes).currentOrdinary)
  }

  for (const span of spans.filter(isInference).sort(compareInferenceBehavior)) {
    const lineage = behaviorLineage(span, spanByIdentity)
    const sessionKey = behaviorSessionKey(span, lineage)
    const parentSessionKey = lineage.length === 0
      ? undefined
      : behaviorSessionKey(span, lineage.slice(0, -1))
    const handledByV2 = v2InferenceIds.has(span.span.spanId)
      || (span.attributes.inferenceId !== undefined
        && v2InferenceIds.has(span.attributes.inferenceId))
    if (handledByV2) {
      inputsBySpanId.set(span.span.spanId, { messages: [], prompts: [] })
      previousOutputsBySession.set(sessionKey, structuredMessages(span.attributes.outputMessages))
      continue
    }
    const previousInputs = previousInputsBySession.get(sessionKey)
      ?? (parentSessionKey === undefined ? [] : previousInputsBySession.get(parentSessionKey) ?? [])
    const { currentInputs, attachmentIndexSet, currentOrdinary } = inferenceInputMessages(span.attributes)
    const insertedIndexes = lcsInsertedInputIndexes(previousInputs, currentOrdinary)
    const replayableOutputs = [...(
      previousOutputsBySession.get(sessionKey)
      ?? (parentSessionKey === undefined ? [] : previousOutputsBySession.get(parentSessionKey) ?? [])
    )]
    const messages = currentInputs.flatMap((message, index): StructuredMessage[] => {
      const attachment = attachmentIndexSet.has(index)
      if (!insertedIndexes.has(index) && !attachment) return []
      for (const part of message.parts) {
        if (part.type !== 'tool_call_response') continue
        toolResults.push({
          traceId: span.traceId,
          response: part.response,
          ...(part.id === undefined ? {} : { id: part.id }),
          ...(part.name === undefined ? {} : { name: part.name }),
        })
      }
      if (!attachment && consumePriorOutput(replayableOutputs, message)) return []
      const parts = message.parts.filter(part => (
        part.type !== 'tool_call' && part.type !== 'tool_call_response'
      ))
      if (parts.length === 0) return []
      return [{
        ...message,
        parts,
        inputIndex: index,
        ...(attachment ? { inputKind: 'prompt_attachment' as const } : {}),
      }]
    })
    const prompt = promptSnapshot(span.attributes)
    const previousPrompt = previousPromptBySession.get(sessionKey)
      ?? (parentSessionKey === undefined ? undefined : previousPromptBySession.get(parentSessionKey))
    inputsBySpanId.set(span.span.spanId, {
      messages,
      prompts: promptCellProjections(prompt, previousPrompt),
    })
    if (prompt !== undefined) previousPromptBySession.set(sessionKey, prompt)
    previousInputsBySession.set(sessionKey, currentOrdinary)
    const outputs = structuredMessages(span.attributes.outputMessages)
    previousOutputsBySession.set(sessionKey, outputs)
  }

  const toolResultById = new Map<string, ToolResultFact>()
  const fallbackByTrace = new Map<string, ToolResultFact[]>()
  for (const result of toolResults) {
    if (result.id !== undefined) {
      toolResultById.set(`${result.traceId}\u0000${result.id}`, result)
    }
    const facts = fallbackByTrace.get(result.traceId) ?? []
    facts.push(result)
    fallbackByTrace.set(result.traceId, facts)
  }

  const toolResultBySpanId = new Map<string, ToolResultFact>()
  const consumedByTrace = new Map<string, Set<number>>()
  for (const span of spans.filter(isTool)) {
    if (span.attributes.toolCallId !== undefined) continue
    const candidates = fallbackByTrace.get(span.traceId) ?? []
    const consumed = consumedByTrace.get(span.traceId) ?? new Set<number>()
    const name = span.attributes.toolName ?? span.span.name
    let resultIndex = candidates.findIndex((candidate, index) => (
      !consumed.has(index) && candidate.name !== undefined && candidate.name === name
    ))
    if (resultIndex < 0) resultIndex = candidates.findIndex((_, index) => !consumed.has(index))
    const result = candidates[resultIndex]
    if (result !== undefined) {
      consumed.add(resultIndex)
      toolResultBySpanId.set(span.span.spanId, result)
      consumedByTrace.set(span.traceId, consumed)
    }
  }
  return { inputsBySpanId, toolResultById, toolResultBySpanId, diagnostics }
}

/**
 * Mark one model call a compaction spent that no compaction event accounts for.
 *
 * The call is not a reply, so it takes no assistant row. It marks its request
 * instead, and the request detail holds what the call recorded. A call that a
 * compaction.completed event names needs no marker of its own: the event's
 * COMPACTED cell already marks that request.
 */
function compactionRequestCell(span: ProjectedSpan): TrajectoryCell {
  const error = statusError(span)
  return {
    ...spanCellBase(span, 'compaction-request'),
    kind: 'compacted',
    text: '',
    requestOnly: true,
    messageSource: {
      kind: 'trajectory_compaction_request',
      ...(span.attributes.contextOperationId === undefined
        ? {}
        : { operationId: span.attributes.contextOperationId }),
      ...(span.attributes.inferenceId === undefined
        ? {}
        : { inferenceId: span.attributes.inferenceId }),
    },
    ...(error === undefined ? {} : { isError: true, result: error }),
  } as TrajectoryCell
}

/**
 * Split compaction cells into one group per compaction operation.
 *
 * One run can compact more than once: a manual /compact runs every processor
 * that applies, and each one is a compaction of its own with its own model
 * calls and outcome. Each becomes a group titled by its number, its request
 * markers ahead of its outcome.
 */
function compactionGroups(
  cells: readonly TrajectoryCell[],
  numberByOperationId: ReadonlyMap<string, number>,
): TrajectoryGroupModel[] {
  const cellsByOperationId = new Map<string, TrajectoryCell[]>()
  for (const cell of [...cells].sort(compareTrajectoryCells)) {
    const source = object(cell.messageSource)
    const operationId = typeof source?.operationId === 'string' ? source.operationId : ''
    const operationCells = cellsByOperationId.get(operationId) ?? []
    operationCells.push(cell)
    cellsByOperationId.set(operationId, operationCells)
  }
  return [...cellsByOperationId].map(([operationId, operationCells]) => {
    const number = numberByOperationId.get(operationId)
    return {
      title: number === undefined ? 'Compaction' : `Compaction #${number}`,
      cells: operationCells,
    }
  })
}

function requestFor(
  span: ProjectedSpan,
  purpose: 'assistant' | 'compaction',
  rootAttributes: NormalizedTrajectoryAttributes | undefined,
  sessionCumulativeUsageByRequestIdentity: ReadonlyMap<string, TrajectoryUsage> | undefined,
): TrajectoryRequest {
  const requestNumber = span.requestNumber ?? 1
  const step = positiveSafeInteger(span.attributes.stepNumber) ?? 1
  const usageValue = usage(span.attributes)
  const facts = recordedFacts(span.attributes, rootAttributes)
  const base = {
    recordId: requestRecordIdentity(span),
    // The request's own record, so its raw data stays reachable from the
    // request detail even when no row in the trajectory carries it.
    traceDetail: span.request,
    group: purpose === 'compaction' ? 'Compaction' : `Step ${step}`,
    number: requestNumber,
    status: status(span),
    startedAt: startedAt(span),
    completedAt: span.endTimeUnixNano === undefined
      ? null
      : Number(span.endTimeUnixNano / NANOSECONDS_PER_MILLISECOND),
    ...(statusError(span) === undefined ? {} : { error: statusError(span) }),
    provider: span.attributes.providerName,
    model: knownModel(span.attributes.requestModel, span.attributes.responseModel),
    requestConfig: requestConfig(span.attributes),
    recordedFacts: facts,
    usage: usageValue,
    cumulativeUsage: span.attributes.inferenceId === undefined
      ? undefined
      : sessionCumulativeUsageByRequestIdentity?.get(
          `${span.traceId}\u0000${span.attributes.inferenceId}`,
        ),
  }
  const defined = Object.fromEntries(Object.entries(base).filter(([, value]) => value !== undefined))
  return purpose === 'compaction'
    ? { ...defined, purpose, turn: span.turn, step } as TrajectoryRequest
    : { ...defined, purpose, turn: span.turn, step } as TrajectoryRequest
}

// A turn is one complete ReAct loop, and it can span several traces: when the
// agent stops to ask (ask_user / permission / confirm), the answer arrives as
// its own request and runs in its own trace while continuing the same loop.
// It can also share a trace with other turns: a Team run keeps one trace for
// every member, and each member takes on several turns inside it.
//
// Everything here is read from what the spans state. `openjiuwen.turn.id` is
// the identity, and it is read per span — spans sharing one are one turn
// whichever traces they sit in and however they are numbered, and spans with
// different ids stay apart even inside one trace or when they claim the same
// number. `openjiuwen.turn.number` is only what the turn is shown as. A span
// that states no id takes its nearest ancestor's, then the id its trace states
// when the trace states exactly one. Traces predating turn ids fall back to the
// number as identity, and a trace stating neither belongs to no turn at all (a
// span opened outside any loop, such as a manual /compact) and stands alone.
//
// Schema-v2 events and legacy spans are projected down separate paths but share
// one turn axis, so this is resolved once over every record and handed to both.
function resolveTurns(
  records: readonly OtlpExportTraceServiceRequest[],
  checkpoint: TrajectoryProjectionCheckpoint['turns'] | undefined,
): TurnIndex {
  const statedById = new Map<string, { parentSpanId?: string, turnId?: string }>()
  // Turn ids of removed turns still count for the traces they were stated in,
  // so a span naming no turn stays apart from the turns its trace kept.
  const turnIdsByTrace = new Map<string, Set<string>>(
    [...checkpoint?.traceTurnIds ?? []].map(([traceId, turnIds]) => [traceId, new Set(turnIds)]),
  )
  const numberByTrace = new Map<string, number>()
  const spans = records.map((record) => {
    const span = soleSpan(record)
    const attributes = normalizeTrajectoryAttributes(span.attributes)
    const recordId = `${span.traceId}:${span.spanId}`
    statedById.set(recordId, { parentSpanId: span.parentSpanId, turnId: attributes.turnId })
    if (attributes.turnId !== undefined) {
      const turnIds = turnIdsByTrace.get(span.traceId) ?? new Set<string>()
      turnIds.add(attributes.turnId)
      turnIdsByTrace.set(span.traceId, turnIds)
    }
    const stated = positiveSafeInteger(attributes.turnNumber)
    if (stated !== undefined) numberByTrace.set(span.traceId, stated)
    return { recordId, span, stated, startedAt: BigInt(span.startTimeUnixNano) }
  })
  const traceKey = (traceId: string): string => {
    const turnIds = [...turnIdsByTrace.get(traceId) ?? []]
    if (turnIds.length === 1) return `id:${turnIds[0]}`
    // A trace holding several turns cannot vouch for a span that names none.
    if (turnIds.length > 1) return `trace:${traceId}`
    const stated = numberByTrace.get(traceId)
    return stated === undefined ? `trace:${traceId}` : `number:${stated}`
  }
  const ancestorTurnId = (traceId: string, parentSpanId: string | undefined): string | undefined => {
    const visited = new Set<string>()
    let current = parentSpanId
    while (current !== undefined && !visited.has(current)) {
      visited.add(current)
      const stated = statedById.get(`${traceId}:${current}`)
      if (stated === undefined) return undefined
      if (stated.turnId !== undefined) return stated.turnId
      current = stated.parentSpanId
    }
    return undefined
  }
  const keyByRecord = new Map<string, string>()
  const numberByKey = new Map<string, number>()
  const startByKey = new Map<string, bigint>()
  for (const { recordId, span, stated, startedAt } of spans) {
    const turnId = statedById.get(recordId)?.turnId ?? ancestorTurnId(span.traceId, span.parentSpanId)
    const key = turnId === undefined ? traceKey(span.traceId) : `id:${turnId}`
    keyByRecord.set(recordId, key)
    const prior = startByKey.get(key)
    if (prior === undefined || startedAt < prior) startByKey.set(key, startedAt)
    if (key.startsWith('number:')) {
      numberByKey.set(key, numberByTrace.get(span.traceId) ?? 1)
    } else if (key.startsWith('id:') && stated !== undefined) {
      numberByKey.set(key, stated)
    }
  }
  // Only a turn that states no number of its own gets one here, and it is
  // placed after every stated number so it can never take one of theirs.
  // Removed turns keep their numbers: stated ones raise the floor, and the
  // unnumbered ones were counted before any that remain.
  let unnumbered = Math.max(0, checkpoint?.maxNumber ?? 0, ...numberByKey.values())
    + (checkpoint?.unnumbered ?? 0)
  for (const [key] of [...startByKey].sort((left, right) =>
    compareBigint(left[1], right[1]) || left[0].localeCompare(right[0]))) {
    if (!numberByKey.has(key)) {
      unnumbered += 1
      numberByKey.set(key, unnumbered)
    }
  }
  const byKey = new Map([...startByKey].map(([key, startedAt]): [string, TurnFact] => [key, {
    key,
    number: numberByKey.get(key) ?? 1,
    stated: !key.startsWith('trace:'),
    startedAt,
  }]))
  const factFor = (key: string): TurnFact | undefined => byKey.get(key)
  return {
    byKey,
    forRecord: (traceId, spanId) => {
      const key = keyByRecord.get(`${traceId}:${spanId}`)
      return key === undefined ? undefined : factFor(key)
    },
    forTurn: (traceId, turnId) => factFor(turnId === undefined ? traceKey(traceId) : `id:${turnId}`),
  }
}

function assignTurns(
  spans: readonly Omit<ProjectedSpan, 'turn' | 'turnKey'>[],
  turns: TurnIndex,
): ProjectedSpan[] {
  return spans.map(span => {
    const fact = turns.forRecord(span.traceId, span.span.spanId)
    return {
      ...span,
      turn: fact?.number ?? 1,
      turnKey: fact?.key ?? `trace:${span.traceId}`,
    }
  })
}

function normalize(
  records: readonly OtlpExportTraceServiceRequest[],
  options: TrajectoryProjectionOptions,
  turns: TurnIndex,
): ProjectedSpan[] {
  const spans = assignTurns(records.map((record): Omit<ProjectedSpan, 'turn' | 'turnKey'> => {
    const span = soleSpan(record)
    const attributes = normalizeTrajectoryAttributes(span.attributes)
    const lifecycle = options.lifecycleByRecordId?.get(`${span.traceId}:${span.spanId}`)
      ?? (span.endTimeUnixNano === undefined ? 'running' : 'completed')
    return {
      attributes,
      request: record,
      span,
      traceId: span.traceId,
      parentSpanId: span.parentSpanId,
      startTimeUnixNano: BigInt(span.startTimeUnixNano),
      endTimeUnixNano: lifecycle === 'running' || span.endTimeUnixNano === undefined
        ? undefined
        : BigInt(span.endTimeUnixNano),
      identityRequestNumber: positiveSafeInteger(attributes.requestNumber),
      requestNumber: undefined,
      streamEvents: normalizeTrajectoryStreamEvents(span.events),
      unresolvedAttributes: options.unresolvedAttributesByRecordId
        ?.get(`${span.traceId}:${span.spanId}`)
        ?? NO_UNRESOLVED_ATTRIBUTES,
      lifecycle,
    }
  }), turns).sort(compareSpans)
  const numbered = assignSubjectRequestNumbers(spans, options.checkpoint?.requestOffsets ?? new Map())
  return assignRequestOwnership(numbered)
}

function sameStep(left: ProjectedSpan, right: ProjectedSpan): boolean {
  if (left.traceId !== right.traceId) return false
  if (left.attributes.stepId !== undefined || right.attributes.stepId !== undefined) {
    return left.attributes.stepId !== undefined
      && left.attributes.stepId === right.attributes.stepId
  }
  const leftStep = positiveSafeInteger(left.attributes.stepNumber)
  const rightStep = positiveSafeInteger(right.attributes.stepNumber)
  return leftStep !== undefined && leftStep === rightStep
}

function inferenceRequestIdentity(span: ProjectedSpan): string {
  if (span.attributes.inferenceId !== undefined) {
    return `${span.traceId}:inference:${span.attributes.inferenceId}`
  }
  if (span.identityRequestNumber !== undefined) {
    return `${span.traceId}:request:${span.identityRequestNumber}`
  }
  return `${span.traceId}:${span.span.spanId}:request`
}

function assignRequestOwnership(spans: readonly ProjectedSpan[]): ProjectedSpan[] {
  const spanByIdentity = new Map(spans.map(span => [spanIdentity(span), span]))
  const inferenceById = new Map(spans.filter(isInference).flatMap((span): Array<[string, ProjectedSpan]> => (
    span.attributes.inferenceId === undefined
      ? []
      : [[`${span.traceId}\u0000${span.attributes.inferenceId}`, span]]
  )))
  const inferences = spans.filter(isInference)
  return spans.map((span) => {
    if (!isTool(span)) return span
    if (span.attributes.inferenceId !== undefined) {
      const inference = inferenceById.get(`${span.traceId}\u0000${span.attributes.inferenceId}`)
      return {
        ...span,
        owningRequestRecordId: inference === undefined
          ? `${span.traceId}:inference:${span.attributes.inferenceId}`
          : inferenceRequestIdentity(inference),
      }
    }
    const visited = new Set<string>()
    let parentSpanId = span.parentSpanId
    while (parentSpanId !== undefined) {
      const identity = `${span.traceId}\u0000${parentSpanId}`
      if (visited.has(identity)) break
      visited.add(identity)
      const parent = spanByIdentity.get(identity)
      if (parent === undefined) break
      if (isInference(parent)) {
        return { ...span, owningRequestRecordId: inferenceRequestIdentity(parent) }
      }
      parentSpanId = parent.parentSpanId
    }
    const candidates = inferences.filter(inference => (
      sameStep(inference, span)
      && inference.startTimeUnixNano <= span.startTimeUnixNano
    )).sort(compareInferenceBehavior)
    const inference = candidates.at(-1)
    return inference === undefined
      ? span
      : { ...span, owningRequestRecordId: inferenceRequestIdentity(inference) }
  })
}

function group(
  turn: MutableTurn,
  step: number,
  stepId: string | undefined,
  order: number,
): MutableGroup {
  const key = stepId === undefined ? `number:${step}` : `id:${stepId}`
  const existing = turn.groups.get(key)
  if (existing !== undefined) {
    existing.order = Math.min(existing.order, order)
    return existing
  }
  const created: MutableGroup = { title: `Step ${step}`, cells: [], order, step }
  turn.groups.set(key, created)
  return created
}

function finalizeRequests(requests: readonly TrajectoryRequest[]): TrajectoryRequest[] {
  return [...requests]
    .sort((left, right) => (left.startedAt ?? 0) - (right.startedAt ?? 0) || left.number - right.number)
}

function requestBehaviorPhase(cell: TrajectoryCell): number {
  if (cell.kind === 'system' || cell.kind === 'user' || cell.kind === 'context') return 0
  if (cell.kind === 'message') return 1
  if (cell.kind === 'tool' || cell.kind === 'subtool') return 2
  return 3
}

function legacyKindOrder(cell: TrajectoryCell): number {
  if (cell.kind === 'system') return 0
  if (cell.kind === 'user' || cell.kind === 'context') return 1
  if (cell.kind === 'message') return 2
  if (cell.kind === 'tool' || cell.kind === 'subtool') return 3
  return 4
}

function isSchemaV2BehaviorCell(cell: TrajectoryCell): boolean {
  const source = object(cell.messageSource)
  return source?.kind === 'trajectory_context_delta'
    || source?.kind === 'trajectory_compaction'
    || source?.kind === 'trajectory_compaction_request'
}

function isSchemaV2CompactionCell(cell: TrajectoryCell): boolean {
  const source = object(cell.messageSource)
  return source?.kind === 'trajectory_compaction'
}

function requestInputSource(cell: TrajectoryCell): Record<string, unknown> | undefined {
  const source = object(cell.messageSource)
  return source?.kind === 'trajectory_context_delta' ? source : undefined
}

function preModelPreparationOrder(cell: TrajectoryCell): number | undefined {
  const source = requestInputSource(cell)
  if (cell.kind === 'system' && source?.transitionKind === 'epoch_baseline') return 0
  if (cell.kind === 'user' && source !== undefined) return 1
  if (cell.requestless === true && (cell.kind === 'tool' || cell.kind === 'subtool')) return 2
  if (source !== undefined && (cell.kind === 'system' || cell.kind === 'context')) return 3
  return undefined
}

function compareTrajectoryCells(left: TrajectoryCell, right: TrajectoryCell): number {
  const leftPreparationOrder = preModelPreparationOrder(left)
  const rightPreparationOrder = preModelPreparationOrder(right)
  if ((leftPreparationOrder === 2 || rightPreparationOrder === 2)
    && leftPreparationOrder !== undefined
    && rightPreparationOrder !== undefined) {
    const preparationOrder = leftPreparationOrder - rightPreparationOrder
    if (preparationOrder !== 0) return preparationOrder
  }
  if (isSchemaV2BehaviorCell(left)
    && isSchemaV2BehaviorCell(right)
    && left.behaviorOrder !== undefined
    && right.behaviorOrder !== undefined) {
    const eventOrder = left.behaviorOrder - right.behaviorOrder
    if (eventOrder !== 0) return eventOrder
  }
  const sameRequest = left.requestRecordId !== undefined
    && left.requestRecordId === right.requestRecordId
  if (sameRequest) {
    // Compaction occurs inside a long physical request, between ordinary
    // assistant/tool cells and the output context checkpoint. Phase-first
    // ordering would always push it behind every USER/MESSAGE/TOOL and creates
    // a non-transitive cycle with schema-v2 sequence ordering. Its recorded
    // event time is therefore the primary cross-source position.
    if (isSchemaV2CompactionCell(left) || isSchemaV2CompactionCell(right)) {
      const physicalOrder = (left.startedAt ?? 0) - (right.startedAt ?? 0)
      if (physicalOrder !== 0) return physicalOrder
      if (left.behaviorOrder !== undefined && right.behaviorOrder !== undefined) {
        const logicalOrder = left.behaviorOrder - right.behaviorOrder
        if (logicalOrder !== 0) return logicalOrder
      }
    }
    if (isSchemaV2BehaviorCell(left)
      && isSchemaV2BehaviorCell(right)
      && left.behaviorOrder !== undefined
      && right.behaviorOrder !== undefined) {
      const eventOrder = left.behaviorOrder - right.behaviorOrder
      if (eventOrder !== 0) return eventOrder
    }
    const phase = requestBehaviorPhase(left) - requestBehaviorPhase(right)
    if (phase !== 0) return phase
    if (left.behaviorOrder !== undefined && right.behaviorOrder !== undefined) {
      const logicalOrder = left.behaviorOrder - right.behaviorOrder
      if (logicalOrder !== 0) return logicalOrder
    }
    if (left.behaviorOrder === undefined && right.behaviorOrder === undefined) {
      const kindOrder = legacyKindOrder(left) - legacyKindOrder(right)
      if (kindOrder !== 0) return kindOrder
    }
  }
  const leftStart = left.startedAt ?? 0
  const rightStart = right.startedAt ?? 0
  if (leftStart !== rightStart) return leftStart - rightStart
  if (left.behaviorOrder !== undefined && right.behaviorOrder !== undefined) {
    const logicalOrder = left.behaviorOrder - right.behaviorOrder
    if (logicalOrder !== 0) return logicalOrder
  }
  const kindOrder = legacyKindOrder(left) - legacyKindOrder(right)
  if (kindOrder !== 0) return kindOrder
  return 0
}

/** Fold one bounded OTLP record window into the trajectory UI's closed read model. */
export function projectOtelTrajectory(
  records: readonly OtlpExportTraceServiceRequest[],
  options: TrajectoryProjectionOptions = {},
): TrajectorySnapshot {
  const v2Records = records.filter(isTrajectoryV2Record)
  const legacyRecords = records.filter(record => !isTrajectoryV2Record(record))
  const v2Reduction = options.v2Reducer?.apply(v2Records) ?? reduceTrajectoryV2(v2Records)
  const currentV2SubjectIds = new Set(v2Records.flatMap(trajectoryV2SubjectIds))
  const v2Subjects = [...v2Reduction.subjects.values()].filter(subject => (
    currentV2SubjectIds.has(subject.subjectId)
  ))
  const v2InferenceIds = new Set(v2Subjects.flatMap(subject => (
    [...subject.handledInferenceIds]
  )))
  const turnIndex = resolveTurns(records, options.checkpoint?.turns)
  const spans = normalize(legacyRecords, options, turnIndex)
  const mutableTurns = new Map<string, MutableTurn>()
  const requests: TrajectoryRequest[] = []
  const inputProjection = projectInferenceInputs(spans, v2InferenceIds, options.checkpoint?.lineage ?? [])
  const modelToolResultByCallId = new Map(v2Subjects.flatMap(subject => (
    [...subject.modelToolResults]
  )))
  const toolSpanIds = new Set(spans.filter(isTool).map(span => span.span.spanId))
  const toolBySpanId = new Map(spans.filter(isTool).map(span => [span.span.spanId, span]))
  const authoritativeToolCallIds = new Set(spans
    .filter(span => isTool(span) && span.attributes.toolAuthoritative === true)
    .flatMap(span => span.attributes.toolCallId === undefined
      ? []
      : [`${span.traceId}\u0000${span.attributes.toolCallId}`]))
  const rootByTrace = new Map<string, ProjectedSpan>()
  for (const span of spans) {
    if (span.attributes.traceRoot !== true && span.parentSpanId !== undefined) continue
    const existing = rootByTrace.get(span.traceId)
    if (
      existing === undefined
      || (span.attributes.traceRoot === true && existing.attributes.traceRoot !== true)
      || (
        span.attributes.traceRoot === existing.attributes.traceRoot
        && span.startTimeUnixNano < existing.startTimeUnixNano
      )
    ) rootByTrace.set(span.traceId, span)
  }
  // The span each turn ran on. A turn that failed before its first model call
  // recorded nothing else, and this is what states why it ended.
  const turnRootByKey = new Map<string, ProjectedSpan>()
  for (const span of spans) {
    if (recordKind(span) !== 'turn') continue
    const existing = turnRootByKey.get(span.turnKey)
    if (existing === undefined || span.startTimeUnixNano < existing.startTimeUnixNano) {
      turnRootByKey.set(span.turnKey, span)
    }
  }
  const compactionInferences = new Map<string, ProjectedSpan[]>()
  // The number each compaction operation was given, read from its model calls.
  const compactionNumberByOperationId = new Map<string, number>()
  // Turns that made a conversational model call. A run that names no turn and
  // made none of these ran outside every turn.
  const conversationTurnKeys = new Set<string>()
  const toolSchemaByTurnAndName = new Map<string, string>()
  const inferenceById = new Map(spans.filter(isInference).flatMap((span): Array<[
    string,
    ProjectedSpan,
  ]> => span.attributes.inferenceId === undefined
    ? []
    : [[`${span.traceId}\u0000${span.attributes.inferenceId}`, span]]))
  const inferenceByToolCallId = new Map(spans.filter(isInference).flatMap(span => (
    assistantParts(span).flatMap((part): Array<[string, ProjectedSpan]> => (
      part.type !== 'tool_call' || part.id === undefined
        ? []
        : [[`${span.traceId}\u0000${part.id}`, span]]
    ))
  )))

  for (const span of spans.filter(isInference)) {
    for (const schema of toolSchemas(span.attributes.toolDefinitions)) {
      toolSchemaByTurnAndName.set(`${span.turnKey}\u0000${schema.name}`, JSON.stringify(schema))
    }
  }

  for (const span of spans) {
    const turn = mutableTurns.get(span.turnKey) ?? { turn: span.turn, groups: new Map() }
    mutableTurns.set(span.turnKey, turn)
    const step = positiveSafeInteger(span.attributes.stepNumber) ?? 1
    if (isInference(span)) {
      const purpose = span.attributes.requestPurpose
      if (purpose === 'compaction') {
        // Every attempt is kept. A throttled compaction is retried, and
        // showing only the last one hid both the retries and the reason the
        // request numbers around it appeared to skip.
        const attempts = compactionInferences.get(span.turnKey) ?? []
        attempts.push(span)
        compactionInferences.set(span.turnKey, attempts)
        const compactionNumber = positiveSafeInteger(span.attributes.compactionNumber)
        if (span.attributes.contextOperationId !== undefined && compactionNumber !== undefined) {
          compactionNumberByOperationId.set(span.attributes.contextOperationId, compactionNumber)
        }
        requests.push(requestFor(
          span,
          'compaction',
          rootByTrace.get(span.traceId)?.attributes,
          options.sessionCumulativeUsageByRequestIdentity,
        ))
        continue
      }
      conversationTurnKeys.add(span.turnKey)
      const target = group(turn, step, span.attributes.stepId, startedAt(span))
      const inputs = inputProjection.inputsBySpanId.get(span.span.spanId)
        ?? { messages: [], prompts: [] }
      target.cells.push(
        ...inputCells(span, inputs),
        assistantCell(span),
      )
      requests.push(requestFor(
        span,
        'assistant',
        rootByTrace.get(span.traceId)?.attributes,
        options.sessionCumulativeUsageByRequestIdentity,
      ))
      continue
    }
    if (isTool(span)) {
      const parentTool = span.parentSpanId === undefined
        ? undefined
        : toolBySpanId.get(span.parentSpanId)
      const duplicateMcpLifecycle = span.attributes.toolAuthoritative !== true
        && parentTool?.attributes.toolAuthoritative === true
        && parentTool.attributes.toolProtocol === 'mcp'
        && parentTool.attributes.toolResourceId !== undefined
        && parentTool.attributes.toolResourceId === span.attributes.toolResourceId
      if (duplicateMcpLifecycle) continue
      if (
        span.attributes.toolAuthoritative === true
        && span.attributes.toolCallId !== undefined
        && span.owningRequestRecordId === undefined
        && !isRoutedAskUserResult(span, rootByTrace.get(span.traceId))
      ) {
        // A schema-v1 agent tool call must belong to a physical model
        // request.  Root-level late callbacks have no such owner; rendering
        // them as Main Agent activity would let a cancelled conversation's
        // subagent leak into whichever run root happened to be alive next.
        continue
      }
      if (
        span.attributes.toolAuthoritative !== true
        && span.attributes.toolCallId !== undefined
        && authoritativeToolCallIds.has(`${span.traceId}\u0000${span.attributes.toolCallId}`)
      ) continue
      const name = span.attributes.toolName ?? span.span.name
      // What the model was given for this call: the tool message of the first
      // request that read it. Joined by tool call id; a call with no id has
      // only the legacy input projection's positional match.
      const modelResult = span.attributes.toolCallId === undefined
        ? inputProjection.toolResultBySpanId.get(span.span.spanId)?.response
        : modelToolResultByCallId.get(span.attributes.toolCallId)
          ?? inputProjection.toolResultById.get(`${span.traceId}\u0000${span.attributes.toolCallId}`)?.response
      group(turn, step, span.attributes.stepId, startedAt(span)).cells.push(toolCell(
        span,
        toolSpanIds,
        toolSchemaByTurnAndName.get(`${span.turnKey}\u0000${name}`),
        modelResult,
      ))
      continue
    }
  }

  const v2CompactionEvents: TrajectoryV2EventProjection[] = []
  // Model calls a schema-v2 cell already marks, by trace and inference id.
  const markedInferenceKeys = new Set<string>()
  for (const event of v2Subjects.flatMap(subject => [...subject.events])) {
    for (const cell of event.cells) {
      if (cell.physicalInferenceId !== undefined) {
        markedInferenceKeys.add(`${event.traceId}\u0000${cell.physicalInferenceId}`)
      }
    }
    if (event.turn === null) {
      v2CompactionEvents.push(event)
      continue
    }
    const askUserSource = object(event.cells[0]?.messageSource)
    const askUserCallId = askUserSource?.kind === 'trajectory_ask_user'
      && typeof askUserSource.callId === 'string'
      ? askUserSource.callId
      : undefined
    const anchorInferenceId = event.cells.at(-1)?.physicalInferenceId
    const inference = anchorInferenceId === undefined
      ? askUserCallId === undefined
        ? undefined
        : inferenceByToolCallId.get(`${event.traceId}\u0000${askUserCallId}`)
      : inferenceById.get(`${event.traceId}\u0000${anchorInferenceId}`)
    // The turn this event states (its trace's, when it states none), so v2
    // events and legacy spans land on one axis. An anchoring inference already carries the resolved
    // turn, so prefer it and keep the event beside the request it belongs to.
    const eventFact = turnIndex.forTurn(event.traceId, event.turnId)
    const eventTurnKey = inference?.turnKey ?? eventFact?.key ?? `trace:${event.traceId}`
    const eventTurnNumber = inference?.turn ?? eventFact?.number ?? event.turn
    const eventStep = inference === undefined
      ? event.step
      : positiveSafeInteger(inference.attributes.stepNumber) ?? event.step
    const inferencePrompt = inference === undefined
      ? undefined
      : promptSnapshot(inference.attributes)
    const turn = mutableTurns.get(eventTurnKey)
      ?? { turn: eventTurnNumber, groups: new Map() }
    mutableTurns.set(eventTurnKey, turn)
    const eventOrder = inference === undefined
      ? Math.min(...event.cells.map(cell => cell.startedAt ?? 0))
      : startedAt(inference)
    group(turn, eventStep, inference?.attributes.stepId, eventOrder).cells.push(...event.cells.map((cell) => {
      const physicalInferenceId = cell.physicalInferenceId
      const cellInference = physicalInferenceId === undefined
        ? undefined
        : inferenceById.get(`${event.traceId}\u0000${physicalInferenceId}`)
      return {
        ...cell,
      ...(cell.promptDetail === undefined || inferencePrompt === undefined
        ? {}
        : {
            promptDetail: {
              ...cell.promptDetail,
              config: inferencePrompt.config,
              tools: inferencePrompt.tools,
            },
            ...(cell.previousPromptDetail === undefined
              ? {}
              : {
                  previousPromptDetail: {
                    ...cell.previousPromptDetail,
                    config: inferencePrompt.config,
                    tools: inferencePrompt.tools,
                  },
                }),
          }),
        ...(physicalInferenceId === undefined
          ? inference === undefined || askUserCallId === undefined
            ? {}
            : { requestRecordId: inferenceRequestIdentity(inference) }
          : {
              requestRecordId: cellInference === undefined
                ? `${event.traceId}:inference:${physicalInferenceId}`
                : inferenceRequestIdentity(cellInference),
            }),
      }
    }))
  }

  const turns: TrajectoryTurnModel[] = []
  const turnEntries: TimedTurnEntry[] = []
  const betweenTurnEntries: TimedTurnEntry[] = []
  const orderedTurns = [...mutableTurns.entries()]
    .sort((left, right) => left[1].turn - right[1].turn || left[0].localeCompare(right[0]))
  for (const [turnKey, turn] of orderedTurns) {
    const fact = turnIndex.byKey.get(turnKey)
    const startedAt = fact?.startedAt ?? 0n
    const groups = [...turn.groups.entries()]
      .sort((left, right) => left[1].order - right[1].order
        || left[1].step - right[1].step
        || left[0].localeCompare(right[0]))
      .map(([, value]) => ({
        title: value.title,
        ...(value.description === undefined ? {} : { description: value.description }),
        cells: [...value.cells].sort(compareTrajectoryCells),
      }))
      .filter(value => value.cells.length > 0)
    // Every attempt is kept, as a marker on its request, unless a compaction
    // event already marks that request.
    const requestCells = (compactionInferences.get(turnKey) ?? [])
      .filter(span => !markedInferenceKeys.has(`${span.traceId}\u0000${span.attributes.inferenceId}`))
      .sort(comparePhysicalInference)
      .map(compactionRequestCell)
    if (fact?.stated === false && !conversationTurnKeys.has(turnKey)) {
      // A run that names no turn and made no conversational model call ran
      // outside every turn, such as a manual /compact. It takes no turn
      // number of its own: it shows between the turns it happened between,
      // and the context it rewrote shows at the next request that reads it.
      const runCells = [...requestCells, ...groups.flatMap(value => value.cells)]
      const betweenGroups = runCells.some(cell => cell.kind === 'compacted')
        ? compactionGroups(runCells, compactionNumberByOperationId)
        : groups
      if (betweenGroups.length > 0) {
        betweenTurnEntries.push({ startedAt, model: { turn: null, groups: betweenGroups } })
      }
      continue
    }
    // A turn that ended in failure shows it, whether it failed on its first
    // call or after several that answered.
    const turnGroups = withTurnFailure(
      groups,
      failedTurnCells(turnRootByKey.get(turnKey), groups.length === 0),
    )
    if (turnGroups.length > 0) {
      turnEntries.push({ startedAt, model: { turn: turn.turn, groups: turnGroups } })
    }
    if (requestCells.length > 0) {
      turnEntries.push({
        startedAt,
        model: { turn: null, groups: compactionGroups(requestCells, compactionNumberByOperationId) },
      })
    }
  }
  // Turns keep their numbered order. A run outside every turn goes ahead of
  // the first turn that began after it.
  betweenTurnEntries.sort((left, right) => compareBigint(left.startedAt, right.startedAt))
  let nextBetweenTurnEntry = 0
  for (const entry of turnEntries) {
    while (nextBetweenTurnEntry < betweenTurnEntries.length
      && betweenTurnEntries[nextBetweenTurnEntry].startedAt < entry.startedAt) {
      turns.push(betweenTurnEntries[nextBetweenTurnEntry].model)
      nextBetweenTurnEntry += 1
    }
    turns.push(entry.model)
  }
  turns.push(...betweenTurnEntries.slice(nextBetweenTurnEntry).map(entry => entry.model))
  for (const event of v2CompactionEvents) {
    if (event.cells.length === 0) continue
    turns.push({
      turn: null,
      groups: [{ title: 'Compaction', cells: [...event.cells] }],
    })
  }

  const diagnostics = [
    ...v2Reduction.diagnostics.filter(item => (
      item.subjectId === undefined || currentV2SubjectIds.has(item.subjectId)
    )),
    ...v2Subjects.flatMap(subject => [...subject.diagnostics]),
    ...inputProjection.diagnostics,
  ]

  return {
    turns,
    requests: finalizeRequests(requests),
    ...(diagnostics.length === 0 ? {} : { diagnostics }),
  }
}

/** Reusable projector boundary independent from any concrete transport. */
export interface TrajectoryProjector {
  project(records: readonly OtlpExportTraceServiceRequest[]): TrajectorySnapshot
}

/** Stateless projector suitable for an HTTP polling or incremental trace store. */
export class OtelGenAiTrajectoryProjector implements TrajectoryProjector {
  private readonly v2Reducer = createTrajectoryV2Reducer()

  /** Project the current bounded record window. */
  project(records: readonly OtlpExportTraceServiceRequest[]): TrajectorySnapshot {
    return projectOtelTrajectory(records, { v2Reducer: this.v2Reducer })
  }
}
