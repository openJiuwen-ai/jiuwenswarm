// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Idempotent reducer for canonical OpenJiuwen trajectory schema-v2 events. */

import { OPENJIUWEN_ATTRIBUTES } from '../semconv/constants.ts'
import { attributeMap } from '../shared/otlp.ts'
import type { OtlpExportTraceServiceRequest, OtlpSpan } from '../shared/otlp.ts'
import type { TrajectoryDiagnostic, TrajectoryPromptSnapshot } from '../trajectory/model.ts'
import type { TrajectoryCell, TrajectoryCellKind } from '../trajectory/record.ts'

export const TRAJECTORY_V2_SCHEMA_VERSION = '2'

interface ContextMessage {
  message_id: string
  role: string
  origin: 'external_user' | 'harness_internal'
  source_kind?: string
  content?: unknown
  tool_calls?: unknown
  tool_call_id?: string
  metadata?: unknown
}

interface ContextDelta {
  op: 'insert' | 'remove' | 'move' | 'replace'
  message_id: string
  index?: number
  from_index?: number
  message?: ContextMessage
  /** Projection-only predecessor when a remove+insert pair represents one logical slot. */
  display_previous_message?: ContextMessage
}

interface ContextCommitPayload {
  baseline_reason?: string
  window_id: string
  base_window_id: string | null
  complete: true
  messages: ContextMessage[]
  delta: ContextDelta[]
  request_purpose?: string
  correlation_kind?: string
  transition_kind?: string
  caused_by_operation_id?: string
  input_window_id?: string | null
  output_window_id?: string
}

interface ParsedEvent {
  eventId: string
  eventKind: string
  inferenceIds: readonly string[]
  payload: Record<string, unknown>
  recordedAt: bigint
  record: OtlpExportTraceServiceRequest
  requestId?: string
  sequence: number
  sequenceEpoch: string
  span: OtlpSpan
  step: number
  stepId?: string
  subjectId: string
  traceId: string
  turn: number
  turnId?: string
}

export interface TrajectoryV2EventProjection {
  cells: readonly TrajectoryCell[]
  eventId: string
  eventKind: string
  requestId?: string
  sequence: number
  step: number
  stepId?: string
  traceId: string
  turn: number | null
  turnId?: string
}

export interface TrajectoryV2SubjectProjection {
  diagnostics: readonly TrajectoryDiagnostic[]
  events: readonly TrajectoryV2EventProjection[]
  handledInferenceIds: ReadonlySet<string>
  subjectId: string
}

export interface TrajectoryV2Reduction {
  diagnostics: readonly TrajectoryDiagnostic[]
  subjects: ReadonlyMap<string, TrajectoryV2SubjectProjection>
}

export interface TrajectoryV2Reducer {
  apply(records: readonly OtlpExportTraceServiceRequest[]): TrajectoryV2Reduction
  clear(): void
}

interface SubjectAccumulator {
  diagnostics: TrajectoryDiagnostic[]
  events: TrajectoryV2EventProjection[]
  handledInferenceIds: Set<string>
  windows: Map<string, ContextMessage[]>
}

interface ContextPromptMaterialization {
  slotByMessageId: ReadonlyMap<string, number>
  snapshot: TrajectoryPromptSnapshot
}

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function spansOf(record: OtlpExportTraceServiceRequest): readonly OtlpSpan[] {
  return record.resourceSpans.flatMap(resource =>
    (resource.scopeSpans ?? []).flatMap(scope => scope.spans ?? []))
}

function soleSpan(record: OtlpExportTraceServiceRequest): OtlpSpan | undefined {
  const spans = spansOf(record)
  return spans.length === 1 ? spans[0] : undefined
}

function textAttribute(attributes: ReadonlyMap<string, unknown>, key: string): string | undefined {
  const value = attributes.get(key)
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

function bigintAttribute(attributes: ReadonlyMap<string, unknown>, key: string): bigint | undefined {
  const value = attributes.get(key)
  if (typeof value !== 'string' && typeof value !== 'number') return undefined
  try {
    return BigInt(value)
  } catch {
    return undefined
  }
}

function safePositiveInteger(value: bigint | undefined): number | undefined {
  if (value === undefined || value <= 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) return undefined
  return Number(value)
}

function payloadAttribute(attributes: ReadonlyMap<string, unknown>): Record<string, unknown> | undefined {
  const value = attributes.get(OPENJIUWEN_ATTRIBUTES.trajectoryPayload)
  if (typeof value === 'string') {
    try {
      return object(JSON.parse(value) as unknown)
    } catch {
      return undefined
    }
  }
  return object(value)
}

function physicalInferenceIds(
  eventKind: string,
  payload: Readonly<Record<string, unknown>>,
  span: OtlpSpan,
): string[] {
  if (eventKind === 'context.window.commit') {
    return typeof span.parentSpanId === 'string' && span.parentSpanId.trim() !== ''
      ? [span.parentSpanId.trim()]
      : []
  }
  if (eventKind !== 'compaction.completed' || !Array.isArray(payload.model_requests)
    || payload.model_requests.length === 0) return []
  const inferenceIds = payload.model_requests.flatMap((value): string[] => {
    const request = object(value)
    return request === undefined || typeof request.inference_id !== 'string'
      || request.inference_id.trim() === ''
      ? []
      : [request.inference_id.trim()]
  })
  return inferenceIds.length === payload.model_requests.length
    && new Set(inferenceIds).size === inferenceIds.length
    ? inferenceIds
    : []
}

/** Detect schema v2 without consulting any compatibility or LangFuse attribute. */
export function isTrajectoryV2Record(record: OtlpExportTraceServiceRequest): boolean {
  const span = soleSpan(record)
  if (span === undefined) return false
  if (textAttribute(attributeMap(span.attributes), OPENJIUWEN_ATTRIBUTES.trajectorySchemaVersion)
    === TRAJECTORY_V2_SCHEMA_VERSION) return true
  return (span.events ?? []).some(event => (
    textAttribute(attributeMap(event.attributes), OPENJIUWEN_ATTRIBUTES.trajectorySchemaVersion)
      === TRAJECTORY_V2_SCHEMA_VERSION
  ))
}

export function trajectoryV2SubjectIds(record: OtlpExportTraceServiceRequest): string[] {
  const span = soleSpan(record)
  if (span === undefined) return []
  const values = [attributeMap(span.attributes), ...(span.events ?? []).map(event => (
    attributeMap(event.attributes)
  ))]
  return [...new Set(values.flatMap((attributes): string[] => {
    const subjectId = textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectorySubjectId)
    return subjectId === undefined ? [] : [subjectId]
  }))]
}

function trajectoryV2EventRecords(
  record: OtlpExportTraceServiceRequest,
): OtlpExportTraceServiceRequest[] {
  const span = soleSpan(record)
  if (span === undefined) return []
  const records: OtlpExportTraceServiceRequest[] = []
  if (textAttribute(attributeMap(span.attributes), OPENJIUWEN_ATTRIBUTES.trajectorySchemaVersion)
    === TRAJECTORY_V2_SCHEMA_VERSION) records.push(record)
  for (const event of span.events ?? []) {
    if (textAttribute(attributeMap(event.attributes), OPENJIUWEN_ATTRIBUTES.trajectorySchemaVersion)
      !== TRAJECTORY_V2_SCHEMA_VERSION) continue
    const syntheticSpan: OtlpSpan = {
      ...span,
      name: event.name,
      startTimeUnixNano: event.timeUnixNano,
      endTimeUnixNano: event.timeUnixNano,
      attributes: [...(span.attributes ?? []), ...(event.attributes ?? [])],
      events: [],
    }
    records.push({ resourceSpans: [{ scopeSpans: [{ spans: [syntheticSpan] }] }] })
  }
  return records
}

function diagnostic(
  code: string,
  message: string,
  subjectId?: string,
  eventId?: string,
  sequence?: number,
): TrajectoryDiagnostic {
  return {
    code,
    message,
    ...(subjectId === undefined ? {} : { subjectId }),
    ...(eventId === undefined ? {} : { eventId }),
    ...(sequence === undefined ? {} : { sequence }),
  }
}

function appendUniqueDiagnostic(
  target: TrajectoryDiagnostic[],
  value: TrajectoryDiagnostic,
): void {
  const identity = `${value.subjectId ?? ''}:${value.eventId ?? ''}:${value.sequence ?? ''}:${value.code}`
  if (!target.some(item => (
    `${item.subjectId ?? ''}:${item.eventId ?? ''}:${item.sequence ?? ''}:${item.code}` === identity
  ))) target.push(value)
}

function parseEvent(record: OtlpExportTraceServiceRequest): ParsedEvent | TrajectoryDiagnostic {
  const span = soleSpan(record)
  if (span === undefined) {
    return diagnostic('v2.invalid_span_count', 'Schema-v2 records require exactly one Span.')
  }
  const attributes = attributeMap(span.attributes)
  const subjectId = textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectorySubjectId)
  const eventId = textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectoryEventId)
  const eventKind = textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectoryEventKind)
  const sequence = safePositiveInteger(bigintAttribute(
    attributes,
    OPENJIUWEN_ATTRIBUTES.trajectorySubjectSequence,
  ))
  const sequenceEpoch = textAttribute(
    attributes,
    OPENJIUWEN_ATTRIBUTES.trajectorySequenceEpoch,
  )
  const recordedAt = bigintAttribute(
    attributes,
    OPENJIUWEN_ATTRIBUTES.trajectoryRecordedAtUnixNano,
  )
  const sessionId = textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectorySessionId)
  const payload = payloadAttribute(attributes)
  if (subjectId === undefined || eventId === undefined || eventKind === undefined
    || sequence === undefined || sequenceEpoch === undefined
    || recordedAt === undefined || recordedAt < 0n
    || sessionId === undefined || payload === undefined) {
    return diagnostic(
      'v2.incomplete_envelope',
      'Schema-v2 event envelope is incomplete; the last valid view was retained.',
      subjectId,
      eventId,
      sequence,
    )
  }
  return {
    subjectId,
    eventId,
    eventKind,
    inferenceIds: physicalInferenceIds(eventKind, payload, span),
    sequence,
    sequenceEpoch,
    recordedAt,
    payload,
    record,
    span,
    traceId: span.traceId,
    requestId: textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectoryRequestId),
    turn: safePositiveInteger(bigintAttribute(attributes, OPENJIUWEN_ATTRIBUTES.turnNumber)) ?? 1,
    step: safePositiveInteger(bigintAttribute(attributes, OPENJIUWEN_ATTRIBUTES.stepNumber)) ?? 1,
    stepId: textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectoryStepId),
    turnId: textAttribute(attributes, OPENJIUWEN_ATTRIBUTES.trajectoryTurnId),
  }
}

function contextMessage(value: unknown): ContextMessage | undefined {
  const candidate = object(value)
  if (candidate === undefined || typeof candidate.message_id !== 'string'
    || candidate.message_id.trim() === '' || typeof candidate.role !== 'string'
    || candidate.role.trim() === ''
    || (candidate.origin !== 'external_user' && candidate.origin !== 'harness_internal')
    || (candidate.source_kind !== undefined
      && (typeof candidate.source_kind !== 'string' || candidate.source_kind.trim() === ''))) {
    return undefined
  }
  return {
    message_id: candidate.message_id,
    role: candidate.role,
    origin: candidate.origin,
    ...(typeof candidate.source_kind !== 'string'
      ? {}
      : { source_kind: candidate.source_kind.trim() }),
    ...(candidate.content === undefined ? {} : { content: candidate.content }),
    ...(candidate.tool_calls === undefined ? {} : { tool_calls: candidate.tool_calls }),
    ...(typeof candidate.tool_call_id !== 'string' ? {} : { tool_call_id: candidate.tool_call_id }),
    ...(candidate.metadata === undefined ? {} : { metadata: candidate.metadata }),
  }
}

function contextDelta(value: unknown): ContextDelta | undefined {
  const candidate = object(value)
  if (candidate === undefined
    || !['insert', 'remove', 'move', 'replace'].includes(String(candidate.op))
    || typeof candidate.message_id !== 'string' || candidate.message_id.trim() === '') return undefined
  const message = candidate.message === undefined ? undefined : contextMessage(candidate.message)
  if ((candidate.op === 'insert' || candidate.op === 'replace') && message === undefined) return undefined
  return {
    op: candidate.op as ContextDelta['op'],
    message_id: candidate.message_id,
    ...(Number.isSafeInteger(candidate.index) && Number(candidate.index) >= 0
      ? { index: Number(candidate.index) }
      : {}),
    ...(Number.isSafeInteger(candidate.from_index) && Number(candidate.from_index) >= 0
      ? { from_index: Number(candidate.from_index) }
      : {}),
    ...(message === undefined ? {} : { message }),
  }
}

function contextCommitPayload(value: Record<string, unknown>): ContextCommitPayload | undefined {
  if (typeof value.window_id !== 'string' || value.window_id.trim() === ''
    || value.complete !== true || !Array.isArray(value.messages) || !Array.isArray(value.delta)
    || !(value.base_window_id === null || typeof value.base_window_id === 'string')) return undefined
  const optionalTextFields = [
    value.request_purpose,
    value.baseline_reason,
    value.correlation_kind,
    value.transition_kind,
    value.caused_by_operation_id,
    value.output_window_id,
  ]
  if (optionalTextFields.some(field => field !== undefined && typeof field !== 'string')) {
    return undefined
  }
  if (value.input_window_id !== undefined && value.input_window_id !== null
    && typeof value.input_window_id !== 'string') return undefined
  const messages = value.messages.map(contextMessage)
  const delta = value.delta.map(contextDelta)
  if (messages.some(message => message === undefined) || delta.some(operation => operation === undefined)) {
    return undefined
  }
  const messageIds = (messages as ContextMessage[]).map(message => message.message_id)
  if (new Set(messageIds).size !== messageIds.length) return undefined
  const epochBaseline = value.transition_kind === 'epoch_baseline'
  if (epochBaseline && (value.base_window_id !== null
    || value.baseline_reason !== 'runtime_epoch_start'
    || delta.length !== 0)) return undefined
  if (!epochBaseline && value.baseline_reason !== undefined) return undefined
  return {
    window_id: value.window_id,
    base_window_id: value.base_window_id,
    complete: true,
    messages: messages as ContextMessage[],
    delta: delta as ContextDelta[],
    ...(typeof value.baseline_reason === 'string' ? { baseline_reason: value.baseline_reason } : {}),
    ...(typeof value.request_purpose === 'string' ? { request_purpose: value.request_purpose } : {}),
    ...(typeof value.correlation_kind === 'string'
      ? { correlation_kind: value.correlation_kind }
      : {}),
    ...(typeof value.transition_kind === 'string' ? { transition_kind: value.transition_kind } : {}),
    ...(typeof value.caused_by_operation_id === 'string'
      ? { caused_by_operation_id: value.caused_by_operation_id }
      : {}),
    ...(value.input_window_id === null
      ? { input_window_id: null }
      : typeof value.input_window_id === 'string'
        ? { input_window_id: value.input_window_id }
        : {}),
    ...(typeof value.output_window_id === 'string' ? { output_window_id: value.output_window_id } : {}),
  }
}

function displayContent(message: ContextMessage | undefined): string {
  if (message === undefined) return ''
  if (typeof message.content === 'string') return message.content
  if (message.content !== undefined) return JSON.stringify(message.content, null, 2) ?? ''
  if (message.tool_calls !== undefined) return JSON.stringify(message.tool_calls, null, 2) ?? ''
  return ''
}

function cellKind(message: ContextMessage | undefined): TrajectoryCellKind {
  if (message?.role === 'system') return 'system'
  if (message?.role === 'user' && message.origin === 'external_user') return 'user'
  return 'context'
}

function promptAttachmentHistoryMode(
  message: ContextMessage | undefined,
): 'snapshot' | 'delta' | undefined {
  if (message === undefined) return undefined
  const metadata = object(message.metadata)
  if (metadata?._openjiuwen_prompt_attachment_history !== true) return undefined
  return metadata.mode === 'snapshot' || metadata.mode === 'delta'
    ? metadata.mode
    : undefined
}

interface LogicalSystemSlot {
  index: number
  key: string
  message: ContextMessage
}

function logicalSystemSlots(messages: readonly ContextMessage[]): LogicalSystemSlot[] {
  const slots: LogicalSystemSlot[] = []
  let attachmentSlot: number | undefined
  messages.forEach((message, index) => {
    if (message.role !== 'system') return
    if (promptAttachmentHistoryMode(message) !== undefined) {
      if (attachmentSlot === undefined) {
        attachmentSlot = slots.length
        slots.push({ index, key: 'prompt-attachment', message })
      } else {
        const previous = slots[attachmentSlot]
        if (previous !== undefined) slots[attachmentSlot] = { ...previous, message }
      }
      return
    }
    slots.push({ index, key: `message:${message.message_id}`, message })
  })
  return slots
}

function systemSlotFingerprint(message: ContextMessage): string {
  const metadata = object(message.metadata)
  const attachment = promptAttachmentHistoryMode(message) !== undefined
  const normalizedMetadata = !attachment || metadata === undefined
    ? metadata
    : Object.fromEntries(Object.entries(metadata).filter(([key]) => (
        key !== 'context_message_id' && key !== 'session_id'
      )))
  return JSON.stringify({
    role: message.role,
    content: message.content,
    tool_calls: message.tool_calls,
    tool_call_id: message.tool_call_id,
    metadata: normalizedMetadata,
  })
}

const EPOCH_INPUT_SOURCE_KINDS = new Set(['query', 'resume', 'steering'])

function epochBaselineCells(
  event: ParsedEvent,
  payload: ContextCommitPayload,
  previous: readonly ContextMessage[],
  behaviorOrder: number,
): TrajectoryCell[] {
  const previousSlots = new Map(logicalSystemSlots(previous).map(slot => [slot.key, slot]))
  const operations: ContextDelta[] = []
  for (const slot of logicalSystemSlots(payload.messages)) {
    const previousSlot = previousSlots.get(slot.key)
    if (previousSlot !== undefined
      && systemSlotFingerprint(previousSlot.message) === systemSlotFingerprint(slot.message)) continue
    operations.push({
      op: previousSlot === undefined ? 'insert' : 'replace',
      message_id: slot.message.message_id,
      index: slot.index,
      message: slot.message,
    })
  }
  payload.messages.forEach((message, index) => {
    if (message.role !== 'user' || message.origin !== 'external_user'
      || message.source_kind === undefined
      || !EPOCH_INPUT_SOURCE_KINDS.has(message.source_kind)) return
    operations.push({
      op: 'insert',
      message_id: message.message_id,
      index,
      message,
    })
  })
  return contextCells(
    event,
    { ...payload, delta: operations },
    previous,
    undefined,
    behaviorOrder,
  )
}

function contextPromptMaterialization(
  messages: readonly ContextMessage[],
): ContextPromptMaterialization {
  const systemMessages: { index: number, content: string }[] = []
  const slotByMessageId = new Map<string, number>()
  let promptAttachmentSlot: number | undefined
  messages.forEach((message, index) => {
    if (message.role !== 'system') return
    const content = displayContent(message)
    const historyMode = promptAttachmentHistoryMode(message)
    if (historyMode === 'snapshot') promptAttachmentSlot ??= index
    if (historyMode !== undefined && promptAttachmentSlot !== undefined) {
      const slotIndex = systemMessages.findIndex(candidate => (
        candidate.index === promptAttachmentSlot
      ))
      const replacement = { index: promptAttachmentSlot, content }
      if (slotIndex === -1) systemMessages.push(replacement)
      else systemMessages[slotIndex] = replacement
      slotByMessageId.set(message.message_id, promptAttachmentSlot)
      return
    }
    systemMessages.push({ index, content })
    slotByMessageId.set(message.message_id, index)
  })
  return {
    slotByMessageId,
    snapshot: {
      config: { provider: '', model: '' },
      system: systemMessages.map(message => message.content).join('\n\n'),
      systemMessages,
      tools: [],
    },
  }
}

function stableIndex(value: string): number {
  let hash = 2166136261
  for (const character of value) {
    hash ^= character.codePointAt(0) ?? 0
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

function eventCell(
  event: ParsedEvent,
  suffix: string,
  kind: TrajectoryCellKind,
  text: string,
  messageSource: unknown,
): TrajectoryCell {
  const startedAt = Number(event.recordedAt / 1_000_000n)
  const recordId = `v2:${event.subjectId}:${event.eventId}:${suffix}`
  return {
    index: stableIndex(recordId),
    recordId,
    kind,
    text,
    inputDetail: text,
    messageSource,
    sourceSeq: event.sequence,
    startedAt,
    timeSeconds: null,
    status: 'complete',
    traceDetail: event.record,
  }
}

function applyContextDelta(
  base: readonly ContextMessage[],
  operations: readonly ContextDelta[],
): ContextMessage[] | undefined {
  const messages = [...base]
  for (const operation of operations) {
    const currentIndex = messages.findIndex(message => message.message_id === operation.message_id)
    if (operation.op === 'insert') {
      if (operation.message === undefined || operation.index === undefined
        || operation.index > messages.length || currentIndex !== -1) return undefined
      messages.splice(operation.index, 0, operation.message)
      continue
    }
    if (currentIndex === -1) return undefined
    if (operation.op === 'remove') {
      messages.splice(currentIndex, 1)
      continue
    }
    if (operation.op === 'replace') {
      if (operation.message === undefined) return undefined
      messages[currentIndex] = operation.message
      continue
    }
    if (operation.index === undefined || operation.index >= messages.length) return undefined
    const [moved] = messages.splice(currentIndex, 1)
    if (moved === undefined) return undefined
    messages.splice(operation.index, 0, moved)
  }
  return messages
}

function sameCheckpoint(
  left: readonly ContextMessage[],
  right: readonly ContextMessage[],
): boolean {
  const identity = (value: unknown): string => {
    if (Array.isArray(value)) return `[${value.map(identity).join(',')}]`
    const candidate = object(value)
    if (candidate === undefined) return JSON.stringify(value) ?? String(value)
    return `{${Object.keys(candidate).sort().map(key => (
      `${JSON.stringify(key)}:${identity(candidate[key])}`
    )).join(',')}}`
  }
  return identity(left) === identity(right)
}

function ephemeralContextSlot(message: ContextMessage | undefined): string | undefined {
  const metadata = object(message?.metadata)
  if (metadata?.browser_working_context === true) return 'browser-working-context'
  if (metadata?.browser_state_context === true) return 'browser-state'
  if (metadata?.browser_state_progress_context === true) return 'browser-state-progress'
  return undefined
}

function sameLogicalContextMessage(left: ContextMessage, right: ContextMessage): boolean {
  const normalized = (message: ContextMessage): ContextMessage => {
    const metadata = object(message.metadata)
    const normalizedMetadata = metadata === undefined
      ? undefined
      : Object.fromEntries(Object.entries(metadata).filter(([key]) => key !== 'context_message_id'))
    return {
      ...message,
      message_id: 'logical-context-slot',
      ...(normalizedMetadata === undefined ? {} : { metadata: normalizedMetadata }),
    }
  }
  return sameCheckpoint([normalized(left)], [normalized(right)])
}

function contextDisplayOperations(
  operations: readonly ContextDelta[],
  base: readonly ContextMessage[],
): ContextDelta[] {
  const baseById = new Map(base.map(message => [message.message_id, message]))
  const insertedSlots = new Set(operations.flatMap((operation): string[] => {
    if (operation.op !== 'insert') return []
    const slot = ephemeralContextSlot(operation.message)
    return slot === undefined ? [] : [slot]
  }))
  const removedBySlot = new Map<string, ContextMessage>()
  for (const operation of operations) {
    if (operation.op !== 'remove') continue
    const removed = baseById.get(operation.message_id)
    const slot = ephemeralContextSlot(removed)
    if (slot !== undefined && insertedSlots.has(slot) && removed !== undefined) {
      removedBySlot.set(slot, removed)
    }
  }
  return operations.flatMap((operation): ContextDelta[] => {
    // A move only changes the position of an existing occurrence inside the
    // context window. Keep it in the raw checkpoint delta, but do not replay
    // unchanged SYSTEM/USER/CONTEXT content as a fresh timeline row.
    if (operation.op === 'move') return []
    if (operation.op === 'remove') {
      const slot = ephemeralContextSlot(baseById.get(operation.message_id))
      return slot !== undefined && removedBySlot.has(slot) ? [] : [operation]
    }
    if (operation.op !== 'insert' || operation.message === undefined) return [operation]
    const slot = ephemeralContextSlot(operation.message)
    const removed = slot === undefined ? undefined : removedBySlot.get(slot)
    if (removed === undefined) return [operation]
    if (sameLogicalContextMessage(removed, operation.message)) return []
    return [{ ...operation, op: 'replace', display_previous_message: removed }]
  })
}

function contextCells(
  event: ParsedEvent,
  payload: ContextCommitPayload,
  base: readonly ContextMessage[] | undefined,
  compactionOperationId?: string,
  behaviorOrder = event.sequence,
): TrajectoryCell[] {
  const rawOperations = payload.delta.length > 0
    ? payload.delta
    : payload.base_window_id === null
      ? payload.messages.map((message, index): ContextDelta => ({
          op: 'insert',
          message_id: message.message_id,
          index,
          message,
        }))
      : []
  const operations = contextDisplayOperations(rawOperations, base ?? [])
  const baseById = new Map((base ?? []).map(message => [message.message_id, message]))
  const prompt = contextPromptMaterialization(payload.messages)
  const previousPrompt = base === undefined
    ? undefined
    : contextPromptMaterialization(base)
  return operations.flatMap((operation, index): TrajectoryCell[] => {
    const message = operation.message ?? baseById.get(operation.message_id)
    if (message?.role === 'assistant' || message?.role === 'tool') return []
    // A compaction output checkpoint can remove most of the old window in one
    // delta. Those removals are real diff facts, but rendering their previous
    // content as fresh USER/SYSTEM rows makes every removed occurrence appear
    // a second time. The COMPACTED cell represents that structural rewrite;
    // keep newly inserted memory/recovery occurrences visible below it.
    if (compactionOperationId !== undefined && operation.op === 'remove') return []
    const text = displayContent(message) || `${operation.op} ${operation.message_id}`
    const kind = cellKind(message)
    const previousMessage = operation.display_previous_message
      ?? (operation.op === 'replace' ? baseById.get(operation.message_id) : undefined)
    const previousText = displayContent(previousMessage)
    const currentMessageIndex = payload.messages.findIndex(
      candidate => candidate.message_id === operation.message_id,
    )
    const previousMessageIndex = (base ?? []).findIndex(
      candidate => candidate.message_id === operation.message_id,
    )
    const operationPreviousPrompt = (
      promptAttachmentHistoryMode(message) === 'delta'
        && (base?.length ?? 0) === 0
        && currentMessageIndex >= 0
    )
      ? contextPromptMaterialization(payload.messages.slice(0, currentMessageIndex))
      : previousPrompt
    const promptSystemMessageIndex = prompt.slotByMessageId.get(operation.message_id)
      ?? operationPreviousPrompt?.slotByMessageId.get(operation.message_id)
      ?? (currentMessageIndex >= 0 ? currentMessageIndex : previousMessageIndex)
    return [{
      ...eventCell(event, `context:${index}:${operation.message_id}`, kind, text, {
        kind: 'trajectory_context_delta',
        operation: operation.op,
        messageId: operation.message_id,
        windowId: payload.window_id,
        baseWindowId: payload.base_window_id,
        role: message?.role,
        origin: message?.origin,
        sourceKind: message?.source_kind,
        metadata: message?.metadata,
        transitionKind: payload.transition_kind,
      }),
      physicalInferenceId: event.inferenceIds[0],
      behaviorOrder: behaviorOrder + (index + 1) / (operations.length + 1),
      ...(kind === 'context' && operation.op === 'replace' && previousText !== text
        ? { previousInputDetail: previousText }
        : {}),
      ...(kind !== 'system'
        ? {}
        : {
            promptDetail: prompt.snapshot,
            ...(operationPreviousPrompt === undefined
              ? {}
              : { previousPromptDetail: operationPreviousPrompt.snapshot }),
            ...(promptSystemMessageIndex < 0 ? {} : { promptSystemMessageIndex }),
          }),
    }]
  })
}

function compactionCells(
  event: ParsedEvent,
  behaviorOrder = event.sequence,
): TrajectoryCell[] {
  const text = typeof event.payload.summary === 'string'
    ? event.payload.summary.trim()
    : ''
  const compactSummary = typeof event.payload.compact_summary === 'string'
    ? event.payload.compact_summary
    : undefined
  if (typeof event.payload.operation_id !== 'string' || event.payload.operation_id.trim() === ''
    || text === '') return []
  if (event.inferenceIds.length === 0) {
    if (!Array.isArray(event.payload.model_requests)
      || event.payload.model_requests.length !== 0) return []
    return [{
      ...eventCell(event, 'compaction', 'compacted', text, {
        kind: 'trajectory_compaction',
        operationId: event.payload.operation_id,
        modelFree: true,
      }),
      behaviorOrder,
      requestless: true,
      ...(compactSummary === undefined || compactSummary.trim() === ''
        ? {}
        : { outputDetail: compactSummary }),
      compactionDetail: { ...event.payload },
    }]
  }
  if (compactSummary === undefined || compactSummary.trim() === '') return []
  const requestBoundaries = event.inferenceIds.slice(0, -1).map((inferenceId, index) => ({
    ...eventCell(event, `compaction-request:${index}:${inferenceId}`, 'compacted', '', {
      kind: 'trajectory_compaction_request',
      operationId: event.payload.operation_id,
      inferenceId,
    }),
    physicalInferenceId: inferenceId,
    requestOnly: true,
    behaviorOrder: behaviorOrder - (event.inferenceIds.length - index) / (
      event.inferenceIds.length + 1
    ),
  }))
  const inferenceId = event.inferenceIds.at(-1)
  if (inferenceId === undefined) return []
  return [...requestBoundaries, {
    ...eventCell(event, 'compaction', 'compacted', text, {
      kind: 'trajectory_compaction',
      operationId: event.payload.operation_id,
      inputWindowId: event.payload.input_window_id,
      outputWindowId: event.payload.output_window_id,
    }),
    physicalInferenceId: inferenceId,
    behaviorOrder,
    outputDetail: compactSummary,
    compactionDetail: { ...event.payload },
  }]
}

function isModelFreeCompaction(event: ParsedEvent): boolean {
  return Array.isArray(event.payload.model_requests)
    && event.payload.model_requests.length === 0
}

interface CompactionCorrelation {
  diagnostic?: string
  operationId?: string
}

function compactionOutputCorrelation(
  event: ParsedEvent,
  payload: ContextCommitPayload,
  compactionsByOperationId: ReadonlyMap<string, ParsedEvent | null>,
): CompactionCorrelation {
  const explicitCorrelation = payload.transition_kind === 'compaction'
    || payload.correlation_kind === 'compaction'
    || payload.caused_by_operation_id !== undefined
    || payload.input_window_id !== undefined
    || payload.output_window_id !== undefined
  if (!explicitCorrelation) return {}
  const operationId = payload.caused_by_operation_id?.trim()
  const inputWindowId = payload.input_window_id === null
    ? null
    : payload.input_window_id?.trim()
  const outputWindowId = payload.output_window_id?.trim()
  const compactionKind = payload.transition_kind === 'compaction'
    || payload.correlation_kind === 'compaction'
  if (!compactionKind
    || !operationId || (inputWindowId !== null && !inputWindowId) || !outputWindowId) {
    return {
      diagnostic: 'Explicit compaction correlation is incomplete; the output window was not associated.',
    }
  }
  if (inputWindowId !== payload.base_window_id
    || outputWindowId !== payload.window_id) {
    return {
      diagnostic: 'Explicit compaction correlation conflicts with the context window transition.',
    }
  }
  if (!compactionsByOperationId.has(operationId)) {
    return {
      diagnostic: `Compaction operation ${operationId} is not available for this output window.`,
    }
  }
  const compaction = compactionsByOperationId.get(operationId)
  if (compaction === null || compaction === undefined) {
    return {
      diagnostic: `Compaction operation ${operationId} is ambiguous and was not associated.`,
    }
  }
  if (compaction.sequence >= event.sequence) {
    return {
      diagnostic: `Compaction operation ${operationId} does not precede its output window.`,
    }
  }
  const compactionInput = compaction.payload.input_window_id
  const compactionOutput = compaction.payload.output_window_id
  if ((inputWindowId !== null
      && typeof compactionInput === 'string'
      && compactionInput !== inputWindowId)
    || (typeof compactionOutput === 'string' && compactionOutput !== outputWindowId)) {
    return {
      diagnostic: `Compaction operation ${operationId} conflicts with its declared input/output windows.`,
    }
  }
  return { operationId }
}

function eventFingerprint(event: ParsedEvent): string {
  return JSON.stringify({
    eventKind: event.eventKind,
    payload: event.payload,
    recordedAt: String(event.recordedAt),
    sequence: event.sequence,
    sequenceEpoch: event.sequenceEpoch,
    subjectId: event.subjectId,
  })
}

function orderedEpochEvents(events: readonly ParsedEvent[]): ParsedEvent[] {
  const byEpoch = new Map<string, ParsedEvent[]>()
  for (const event of events) {
    const epoch = byEpoch.get(event.sequenceEpoch) ?? []
    epoch.push(event)
    byEpoch.set(event.sequenceEpoch, epoch)
  }
  return [...byEpoch.entries()]
    .map(([sequenceEpoch, epochEvents]) => {
      const ordered = [...epochEvents].sort((left, right) => (
        left.sequence - right.sequence
          || (left.recordedAt < right.recordedAt ? -1 : left.recordedAt > right.recordedAt ? 1 : 0)
          || left.eventId.localeCompare(right.eventId)
      ))
      const firstRecordedAt = ordered.reduce(
        (earliest, event) => event.recordedAt < earliest ? event.recordedAt : earliest,
        ordered[0]?.recordedAt ?? 0n,
      )
      return { sequenceEpoch, firstRecordedAt, events: ordered }
    })
    .sort((left, right) => (
      left.firstRecordedAt < right.firstRecordedAt
        ? -1
        : left.firstRecordedAt > right.firstRecordedAt
          ? 1
          : left.sequenceEpoch.localeCompare(right.sequenceEpoch)
    ))
    .flatMap(epoch => epoch.events)
}

function askUserInteractionId(event: ParsedEvent): string | undefined {
  const value = event.payload.interaction_id
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

function askUserEventProjection(
  requested: ParsedEvent,
  resolved: ParsedEvent | undefined,
): TrajectoryV2EventProjection | undefined {
  const interactionId = askUserInteractionId(requested)
  const argumentsValue = object(requested.payload.arguments)
  const schema = object(requested.payload.schema)
  if (interactionId === undefined || argumentsValue === undefined || schema === undefined) return undefined
  const input = JSON.stringify(argumentsValue, null, 2)
  const resultValue = resolved?.payload.result
  const result = resultValue === undefined
    ? undefined
    : typeof resultValue === 'string' ? resultValue : JSON.stringify(resultValue, null, 2)
  const outcome = resolved?.payload.outcome
  const failed = resolved !== undefined && outcome !== 'answered'
  const cell = {
    ...eventCell(requested, `ask-user:${interactionId}`, 'tool', `ask_user · ${input}`, {
      kind: 'trajectory_ask_user',
      interactionId,
      callId: interactionId,
    }),
    callId: interactionId,
    inputDetail: input,
    schemaDetail: JSON.stringify(schema),
    status: resolved === undefined ? 'running' as const : failed ? 'error' as const : 'complete' as const,
    timeSeconds: resolved === undefined
      ? null
      : Number(resolved.recordedAt - requested.recordedAt) / 1_000_000_000,
    ...(result === undefined ? {} : { outputDetail: result, result }),
    ...(failed ? { isError: true } : {}),
  }
  return {
    cells: [cell],
    eventId: requested.eventId,
    eventKind: requested.eventKind,
    sequence: requested.sequence,
    step: requested.step,
    traceId: requested.traceId,
    turn: requested.turn,
    ...(requested.requestId === undefined ? {} : { requestId: requested.requestId }),
    ...(requested.stepId === undefined ? {} : { stepId: requested.stepId }),
    ...(requested.turnId === undefined ? {} : { turnId: requested.turnId }),
  }
}

function rebuildSubject(subjectId: string, events: readonly ParsedEvent[]): TrajectoryV2SubjectProjection {
  const accumulator: SubjectAccumulator = {
    diagnostics: [],
    events: [],
    handledInferenceIds: new Set(),
    windows: new Map(),
  }
  const bySequenceByEpoch = new Map<string, Map<number, ParsedEvent>>()
  const expectedByEpoch = new Map<string, number>()
  const blockedEpochs = new Set<string>()
  const compactionsByOperationId = new Map<string, ParsedEvent | null>()
  const askUserRequested = new Map<string, ParsedEvent>()
  const askUserResolved = new Map<string, ParsedEvent>()
  const referencedCompactionOperationIds = new Set<string>()
  let activeEpoch: string | undefined
  let epochBaselineBase: readonly ContextMessage[] | undefined
  let lastWindow: readonly ContextMessage[] | undefined
  let subjectOrder = 0
  for (const event of orderedEpochEvents(events)) {
    subjectOrder += 1
    if (activeEpoch !== event.sequenceEpoch) {
      activeEpoch = event.sequenceEpoch
      epochBaselineBase = lastWindow
    }
    const bySequence = bySequenceByEpoch.get(event.sequenceEpoch) ?? new Map<number, ParsedEvent>()
    bySequenceByEpoch.set(event.sequenceEpoch, bySequence)
    const collision = bySequence.get(event.sequence)
    if (collision !== undefined) {
      accumulator.diagnostics.push(diagnostic(
        'v2.sequence_conflict',
        'Two different events use the same sequence within one epoch; the first valid event was retained.',
        subjectId,
        event.eventId,
        event.sequence,
      ))
      continue
    }
    bySequence.set(event.sequence, event)
    let expected = expectedByEpoch.get(event.sequenceEpoch)
    const completeCheckpoint = event.eventKind === 'context.window.commit'
      && contextCommitPayload(event.payload)?.complete === true
    if (blockedEpochs.has(event.sequenceEpoch)) {
      if (!completeCheckpoint) continue
      blockedEpochs.delete(event.sequenceEpoch)
      expected = event.sequence
      accumulator.diagnostics.push(diagnostic(
        'v2.checkpoint_recovery',
        'A complete context checkpoint resumed the blocked sequence epoch.',
        subjectId,
        event.eventId,
        event.sequence,
      ))
    }
    if (expected !== undefined && event.sequence > expected) {
      accumulator.diagnostics.push(diagnostic(
        'v2.sequence_gap',
        `Expected subject sequence ${expected} but received ${event.sequence}.`,
        subjectId,
        event.eventId,
        event.sequence,
      ))
      if (!completeCheckpoint) {
        blockedEpochs.add(event.sequenceEpoch)
        continue
      }
      accumulator.diagnostics.push(diagnostic(
        'v2.checkpoint_recovery',
        'A complete context checkpoint advanced the view across the missing sequence.',
        subjectId,
        event.eventId,
        event.sequence,
      ))
    }
    if (expected === undefined && event.sequence > 1) {
      accumulator.diagnostics.push(diagnostic(
        'v2.partial_window',
        `The bounded record window begins at subject sequence ${event.sequence}.`,
        subjectId,
        event.eventId,
        event.sequence,
      ))
    }
    expectedByEpoch.set(event.sequenceEpoch, event.sequence + 1)
    if (event.eventKind === 'context.window.commit') {
      if (event.inferenceIds.length !== 1) {
        accumulator.diagnostics.push(diagnostic(
          'v2.missing_physical_request',
          'Context commit is missing its physical inference parent.',
          subjectId,
          event.eventId,
          event.sequence,
        ))
        continue
      }
      const payload = contextCommitPayload(event.payload)
      if (payload === undefined) {
        accumulator.diagnostics.push(diagnostic(
          'v2.invalid_context_commit',
          'Invalid context.window.commit payload; the last valid context window was retained.',
          subjectId,
          event.eventId,
          event.sequence,
        ))
        continue
      }
      const base = payload.base_window_id === null
        ? []
        : accumulator.windows.get(payload.base_window_id)
      if (payload.base_window_id !== null && base === undefined) {
        accumulator.diagnostics.push(diagnostic(
          'v2.missing_base_window',
          `Base context window ${payload.base_window_id} is not available; the complete checkpoint was used.`,
          subjectId,
          event.eventId,
          event.sequence,
        ))
      }
      const epochBaseline = payload.transition_kind === 'epoch_baseline'
      if (base !== undefined && !epochBaseline) {
        const reconstructed = applyContextDelta(base, payload.delta)
        if (reconstructed === undefined || !sameCheckpoint(reconstructed, payload.messages)) {
          accumulator.diagnostics.push(diagnostic(
            'v2.delta_checkpoint_mismatch',
            'Context delta does not reconstruct its complete checkpoint; the last valid window was retained.',
            subjectId,
            event.eventId,
            event.sequence,
          ))
          continue
        }
      }
      accumulator.windows.set(payload.window_id, payload.messages)
      lastWindow = payload.messages
      accumulator.handledInferenceIds.add(event.inferenceIds[0])
      const referencedOperationId = payload.caused_by_operation_id?.trim()
      if (referencedOperationId) {
        referencedCompactionOperationIds.add(referencedOperationId)
      }
      const correlation = compactionOutputCorrelation(
        event,
        payload,
        compactionsByOperationId,
      )
      if (correlation.diagnostic !== undefined) {
        accumulator.diagnostics.push(diagnostic(
          'v2.invalid_compaction_correlation',
          correlation.diagnostic,
          subjectId,
          event.eventId,
          event.sequence,
        ))
      }
      accumulator.events.push({
        eventId: event.eventId,
        eventKind: event.eventKind,
        sequence: event.sequence,
        requestId: event.requestId,
        turn: event.turn,
        step: event.step,
        stepId: event.stepId,
        traceId: event.traceId,
        turnId: event.turnId,
        cells: epochBaseline && epochBaselineBase !== undefined
          ? epochBaselineCells(
              event,
              payload,
              epochBaselineBase,
              subjectOrder,
            )
          : contextCells(
              event,
              payload,
              base,
              correlation.operationId,
              subjectOrder,
            ),
      })
      continue
    }
    if (event.eventKind === 'compaction.completed') {
      const modelRequests = event.payload.model_requests
      if (event.inferenceIds.length === 0
        && (!Array.isArray(modelRequests) || modelRequests.length !== 0)) {
        accumulator.diagnostics.push(diagnostic(
          'v2.missing_physical_request',
          'A model-backed compaction event must identify its physical inference.',
          subjectId,
          event.eventId,
          event.sequence,
        ))
        continue
      }
      const cells = compactionCells(event, subjectOrder)
      if (cells.length === 0) {
        accumulator.diagnostics.push(diagnostic(
          'v2.invalid_compaction',
          'Invalid compaction.completed payload; the last valid view was retained.',
          subjectId,
          event.eventId,
          event.sequence,
        ))
        continue
      }
      accumulator.events.push({
        eventId: event.eventId,
        eventKind: event.eventKind,
        sequence: event.sequence,
        requestId: event.requestId,
        turn: event.turn,
        step: event.step,
        stepId: event.stepId,
        traceId: event.traceId,
        turnId: event.turnId,
        cells,
      })
      const operationId = String(event.payload.operation_id).trim()
      if (compactionsByOperationId.has(operationId)) {
        compactionsByOperationId.set(operationId, null)
        accumulator.diagnostics.push(diagnostic(
          'v2.compaction_operation_conflict',
          `Compaction operation ${operationId} is declared by multiple events.`,
          subjectId,
          event.eventId,
          event.sequence,
        ))
      } else {
        compactionsByOperationId.set(operationId, event)
      }
      continue
    }
    if (event.eventKind === 'ask_user.requested' || event.eventKind === 'ask_user.resolved') {
      const interactionId = askUserInteractionId(event)
      if (interactionId === undefined) {
        accumulator.diagnostics.push(diagnostic(
          'v2.invalid_ask_user',
          'ask_user event is missing its interaction identity.',
          subjectId,
          event.eventId,
          event.sequence,
        ))
        continue
      }
      const target = event.eventKind === 'ask_user.requested' ? askUserRequested : askUserResolved
      if (!target.has(interactionId)) target.set(interactionId, event)
      continue
    }
    accumulator.diagnostics.push(diagnostic(
      'v2.unknown_event_kind',
      `Unsupported schema-v2 event kind: ${event.eventKind}.`,
      subjectId,
      event.eventId,
      event.sequence,
    ))
  }
  for (const [interactionId, requested] of askUserRequested) {
    const projection = askUserEventProjection(requested, askUserResolved.get(interactionId))
    if (projection === undefined) {
      accumulator.diagnostics.push(diagnostic(
        'v2.invalid_ask_user',
        'ask_user requested payload is incomplete.',
        subjectId,
        requested.eventId,
        requested.sequence,
      ))
      continue
    }
    accumulator.events.push(projection)
  }
  for (const [operationId, compaction] of compactionsByOperationId) {
    if (compaction === null
      || isModelFreeCompaction(compaction)
      || referencedCompactionOperationIds.has(operationId)) continue
    accumulator.diagnostics.push(diagnostic(
      'v2.missing_compaction_output_correlation',
      `Compaction operation ${operationId} has no explicitly correlated output window.`,
      subjectId,
      compaction.eventId,
      compaction.sequence,
    ))
  }
  return {
    subjectId,
    diagnostics: accumulator.diagnostics,
    events: accumulator.events,
    handledInferenceIds: accumulator.handledInferenceIds,
  }
}

/** Create one durable UI reducer; later revisions of the same Span replace provisional payloads. */
export function createTrajectoryV2Reducer(): TrajectoryV2Reducer {
  const eventsBySubject = new Map<string, Map<string, ParsedEvent>>()
  const globalDiagnostics: TrajectoryDiagnostic[] = []
  return {
    apply(records) {
      for (const record of records) {
        for (const eventRecord of trajectoryV2EventRecords(record)) {
          const parsed = parseEvent(eventRecord)
          if ('code' in parsed) {
            appendUniqueDiagnostic(globalDiagnostics, parsed)
            continue
          }
          const subjectEvents = eventsBySubject.get(parsed.subjectId) ?? new Map<string, ParsedEvent>()
          const existing = subjectEvents.get(parsed.eventId)
          if (existing === undefined) {
            subjectEvents.set(parsed.eventId, parsed)
            eventsBySubject.set(parsed.subjectId, subjectEvents)
          } else if (eventFingerprint(existing) !== eventFingerprint(parsed)) {
            if (existing.traceId === parsed.traceId && existing.span.spanId === parsed.span.spanId) {
              subjectEvents.set(parsed.eventId, parsed)
            } else {
              appendUniqueDiagnostic(globalDiagnostics, diagnostic(
                'v2.event_id_conflict',
                'Different Spans reused one event ID; the first immutable event was retained.',
                parsed.subjectId,
                parsed.eventId,
                parsed.sequence,
              ))
            }
          }
        }
      }
      const subjects = new Map([...eventsBySubject].map(([subjectId, events]) => (
        [subjectId, rebuildSubject(subjectId, [...events.values()])] as const
      )))
      return { diagnostics: globalDiagnostics, subjects }
    },
    clear() {
      eventsBySubject.clear()
      globalDiagnostics.length = 0
    },
  }
}

/** Stateless convenience boundary used by tests and archive replay. */
export function reduceTrajectoryV2(
  records: readonly OtlpExportTraceServiceRequest[],
): TrajectoryV2Reduction {
  return createTrajectoryV2Reducer().apply(records)
}
