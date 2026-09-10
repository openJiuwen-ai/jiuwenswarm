// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Per-field semantic normalization for standard, OpenJiuwen, and legacy spans. */

import {
  exactAttributeMap,
  readBooleanAttribute,
  readInt64Attribute,
  readNumberAttribute,
  readStringArrayAttribute,
  readStringAttribute,
  structuredOtlpValue,
} from '../semconv/attributes.ts'
import {
  DSH_ATTRIBUTES,
  DSH_EVENTS,
  DSH_REQUEST_PURPOSES,
  DSH_STREAM_KINDS,
  DSH_TRAJECTORY_KINDS,
  OPENJIUWEN_ATTRIBUTES,
  OPENJIUWEN_EVENTS,
  STANDARD_ATTRIBUTES,
} from '../semconv/constants.ts'
import type { OtlpAttributeMap } from '../semconv/attributes.ts'
import type { OtlpAnyValue, OtlpKeyValue, OtlpSpanEvent } from '../shared/otlp.ts'

/** Stable facts consumed by the projector after profile-specific fallback. */
export interface NormalizedTrajectoryAttributes {
  raw: OtlpAttributeMap
  sources: Readonly<Record<string, string>>
  conversationId?: string
  traceRoot?: boolean
  traceSchemaVersion?: string
  traceComplete?: boolean
  traceForcedClose?: boolean
  spanForcedClose?: boolean
  spanForcedCloseReason?: string
  operationName?: string
  providerName?: string
  requestId?: string
  runId?: string
  turnId?: string
  agentMode?: string
  requestModel?: string
  responseModel?: string
  requestMaxTokens?: bigint
  requestTemperature?: number
  requestTopP?: number
  requestStopSequences?: readonly string[]
  requestStream?: boolean
  requestReasoningLevel?: string
  responseId?: string
  responseFinishReasons?: readonly string[]
  responseTimeToFirstChunkSeconds?: number
  usageInputTokens?: bigint
  usageOutputTokens?: bigint
  usageReasoningTokens?: bigint
  usageCacheReadTokens?: bigint
  usageCacheCreationTokens?: bigint
  inputCost?: number
  outputCost?: number
  totalCost?: number
  totalLatencyMs?: number
  timePerOutputTokenMs?: number
  promptTokenIds?: unknown
  completionTokenIds?: unknown
  logprobs?: unknown
  parserResult?: unknown
  providerMetadata?: unknown
  agentId?: string
  agentName?: string
  agentVersion?: string
  agentDescription?: string
  executionSubjectId?: string
  executionSubjectKind?: string
  executionSubjectParentId?: string
  executionSubjectSessionId?: string
  executionSubjectRequestNumber?: bigint
  requestMessages?: unknown
  requestMessagesComplete?: boolean
  systemInstructions?: unknown
  inputMessages?: unknown
  inputMessagesComplete?: boolean
  inputMessageProvenance?: unknown
  outputMessages?: unknown
  toolDefinitions?: unknown
  toolName?: string
  toolCallId?: string
  toolType?: string
  toolDescription?: string
  toolResourceId?: string
  openJiuwenToolType?: string
  toolAuthoritative?: boolean
  toolCallArguments?: unknown
  toolCallResult?: unknown
  sourceSequence?: bigint
  turnNumber?: bigint
  stepId?: string
  stepNumber?: bigint
  inferenceId?: string
  trajectoryKind?: string
  requestPurpose?: string
  requestNumber?: bigint
  requestRetryCount?: bigint
  requestMaxRetries?: bigint
  messageSourceKind?: string
  messageSourcePlugin?: string
  compactionInputTokens?: bigint
  compactionSummary?: string
  langfuseObservationType?: string
  errorType?: string
  errorMessage?: string
}

/** One replayable stream event after OpenJiuwen/DSH compatibility resolution. */
export interface NormalizedTrajectoryStreamEvent {
  sequence: number
  kind: string
  source: string
  text?: string
  toolCallId?: string
  toolName?: string
  argumentsDelta?: string
}

type MutableNormalized = Omit<NormalizedTrajectoryAttributes, 'sources'> & {
  sources: Record<string, string>
}

interface Resolved<T> {
  key: string
  value: T
  complete?: boolean
}

interface NormalizedPart {
  type: string
  content?: string
  id?: string
  name?: string
  arguments?: unknown
  response?: unknown
}

interface NormalizedMessage {
  role: string
  parts: readonly NormalizedPart[]
  openjiuwen?: {
    kind: 'prompt_attachment_history'
    mode: 'snapshot' | 'delta'
  }
}

const LEGACY = {
  openJiuwenSessionId: 'openjiuwen.session_id',
  responseFinishReason: 'gen_ai.response.finish_reason',
  responseTimeToFirstTokenMs: 'gen_ai.response.time_to_first_token_ms',
  toolCallId: 'gen_ai.tool.id',
  toolCallArguments: 'gen_ai.tool.input',
  toolCallResult: 'gen_ai.tool.output',
  toolCalls: 'gen_ai.tool_calls',
  langfuseInput: 'langfuse.observation.input',
  langfuseOutput: 'langfuse.observation.output',
  langfuseObservationType: 'langfuse.observation.type',
  agentTeamSessionId: 'agentteam.session.id',
  deepAgentName: 'deepagent.agent.name',
  deepAgentIteration: 'deepagent.task.iteration',
} as const

const TRAJECTORY_KINDS = new Set<string>(DSH_TRAJECTORY_KINDS)
const REQUEST_PURPOSES = new Set<string>(DSH_REQUEST_PURPOSES)
const STREAM_KINDS = new Set<string>(DSH_STREAM_KINDS)

function parsedJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown
  } catch {
    return value
  }
}

function flexibleValue(value: OtlpAnyValue): unknown {
  if ('stringValue' in value) return parsedJson(value.stringValue)
  return structuredOtlpValue(value)
}

function resolveString(attributes: OtlpAttributeMap, keys: readonly string[]): Resolved<string> | undefined {
  for (const key of keys) {
    const value = readStringAttribute(attributes, key)
    if (value !== undefined) return { key, value }
  }
  return undefined
}

function resolveClosedString(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
  accepted: ReadonlySet<string>,
): Resolved<string> | undefined {
  for (const key of keys) {
    const value = readStringAttribute(attributes, key)
    if (value !== undefined && accepted.has(value)) return { key, value }
  }
  return undefined
}

function resolveBoolean(attributes: OtlpAttributeMap, keys: readonly string[]): Resolved<boolean> | undefined {
  for (const key of keys) {
    const value = readBooleanAttribute(attributes, key)
    if (value !== undefined) return { key, value }
  }
  return undefined
}

function resolveNonNegativeInt64(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
): Resolved<bigint> | undefined {
  for (const key of keys) {
    const value = readInt64Attribute(attributes, key)
    if (value !== undefined && value >= 0n) return { key, value }
  }
  return undefined
}

function resolvePositiveInt64(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
): Resolved<bigint> | undefined {
  for (const key of keys) {
    const value = readInt64Attribute(attributes, key)
    if (value !== undefined && value > 0n) return { key, value }
  }
  return undefined
}

function resolveNumber(attributes: OtlpAttributeMap, keys: readonly string[]): Resolved<number> | undefined {
  for (const key of keys) {
    const value = readNumberAttribute(attributes, key)
    if (value !== undefined && Number.isFinite(value)) return { key, value }
  }
  return undefined
}

function resolveNonNegativeNumber(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
): Resolved<number> | undefined {
  for (const key of keys) {
    const value = readNumberAttribute(attributes, key)
    if (value !== undefined && Number.isFinite(value) && value >= 0) return { key, value }
  }
  return undefined
}

function resolveStringArray(attributes: OtlpAttributeMap, keys: readonly string[]): Resolved<readonly string[]> | undefined {
  for (const key of keys) {
    const value = readStringArrayAttribute(attributes, key)
    if (value !== undefined) return { key, value }
    const scalar = readStringAttribute(attributes, key)
    if (scalar !== undefined) {
      const parsed = parsedJson(scalar)
      if (Array.isArray(parsed) && parsed.every(item => typeof item === 'string')) {
        return { key, value: parsed }
      }
      return { key, value: [scalar] }
    }
  }
  return undefined
}

function resolveFlexible(attributes: OtlpAttributeMap, keys: readonly string[]): Resolved<unknown> | undefined {
  for (const key of keys) {
    const value = attributes.get(key)
    if (value !== undefined) return { key, value: flexibleValue(value) }
  }
  return undefined
}

function assign<T>(
  target: MutableNormalized,
  field: keyof NormalizedTrajectoryAttributes,
  resolved: Resolved<T> | undefined,
): void {
  if (resolved === undefined) return
  ;(target as unknown as Record<string, unknown>)[field] = resolved.value
  target.sources[field] = resolved.key
}

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function langfuseOutput(attributes: OtlpAttributeMap): Resolved<Record<string, unknown>> | undefined {
  const resolved = resolveFlexible(attributes, [LEGACY.langfuseOutput])
  if (resolved === undefined) return undefined
  const value = object(resolved.value)
  return value === undefined ? undefined : { key: resolved.key, value }
}

function langfuseFinishReasons(
  output: Resolved<Record<string, unknown>> | undefined,
): Resolved<readonly string[]> | undefined {
  if (output === undefined || !Array.isArray(output.value.choices)) return undefined
  const reasons = output.value.choices.flatMap((candidate): string[] => {
    const choice = object(candidate)
    return typeof choice?.finish_reason === 'string' ? [choice.finish_reason] : []
  })
  return reasons.length === 0 ? undefined : { key: output.key, value: reasons }
}

function text(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2) ?? String(value)
}

function normalizedToolCall(value: unknown): NormalizedPart | undefined {
  const call = object(value)
  if (call === undefined) return undefined
  const functionValue = object(call.function)
  const name = typeof call.name === 'string'
    ? call.name
    : typeof functionValue?.name === 'string' ? functionValue.name : undefined
  const rawArguments = call.arguments ?? functionValue?.arguments
  const id = typeof call.id === 'string' ? call.id : undefined
  if (name === undefined && id === undefined && rawArguments === undefined) return undefined
  return {
    type: 'tool_call',
    ...(id === undefined ? {} : { id }),
    ...(name === undefined ? {} : { name }),
    ...(rawArguments === undefined
      ? {}
      : { arguments: typeof rawArguments === 'string' ? parsedJson(rawArguments) : rawArguments }),
  }
}

function canonicalValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalValue).join(',')}]`
  const valueObject = object(value)
  if (valueObject === undefined) return JSON.stringify(value) ?? String(value)
  return `{${Object.keys(valueObject).sort().map(key => (
    `${JSON.stringify(key)}:${canonicalValue(valueObject[key])}`
  )).join(',')}}`
}

function sameToolCall(left: NormalizedPart, right: NormalizedPart): boolean {
  if (left.type !== 'tool_call' || right.type !== 'tool_call') return false
  if (left.id !== undefined || right.id !== undefined) {
    return left.id !== undefined && left.id === right.id
  }
  return left.name !== undefined
    && left.name === right.name
    && canonicalValue(left.arguments) === canonicalValue(right.arguments)
}

function mergeToolCallRepresentations(
  primaryParts: readonly NormalizedPart[],
  aliasCalls: readonly NormalizedPart[],
): NormalizedPart[] {
  const consumedPrimaryCalls = new Set<number>()
  const merged = [...primaryParts]
  for (const call of aliasCalls) {
    const primaryIndex = primaryParts.findIndex((part, index) => (
      !consumedPrimaryCalls.has(index) && sameToolCall(part, call)
    ))
    if (primaryIndex >= 0) {
      consumedPrimaryCalls.add(primaryIndex)
      continue
    }
    merged.push(call)
  }
  return merged
}

function normalizedContentParts(value: unknown, reasoning: boolean): NormalizedPart[] {
  if (typeof value === 'string') return [{ type: reasoning ? 'reasoning' : 'text', content: value }]
  if (!Array.isArray(value)) {
    return value === undefined ? [] : [{ type: reasoning ? 'reasoning' : 'text', content: text(value) }]
  }
  const parts: NormalizedPart[] = []
  for (const candidate of value) {
    if (typeof candidate === 'string') {
      parts.push({ type: reasoning ? 'reasoning' : 'text', content: candidate })
      continue
    }
    const part = object(candidate)
    if (part === undefined) {
      parts.push({ type: reasoning ? 'reasoning' : 'text', content: text(candidate) })
      continue
    }
    const typeValue = typeof part.type === 'string' ? part.type : reasoning ? 'reasoning' : 'text'
    const content = typeof part.content === 'string'
      ? part.content
      : typeof part.text === 'string' ? part.text : undefined
    const toolCall = typeValue === 'tool_call' || typeValue === 'tool-call'
      ? normalizedToolCall(part)
      : undefined
    if (toolCall !== undefined) {
      parts.push(toolCall)
      continue
    }
    parts.push({
      type: typeValue,
      ...(content === undefined ? {} : { content }),
      ...(typeof part.id === 'string' ? { id: part.id } : {}),
      ...(typeof part.name === 'string' ? { name: part.name } : {}),
      ...(part.arguments === undefined ? {} : { arguments: part.arguments }),
      ...(part.response === undefined ? {} : { response: part.response }),
    })
  }
  return parts
}

function normalizedMessage(value: unknown, defaultRole: string): NormalizedMessage | undefined {
  const message = object(value)
  if (message === undefined) {
    if (value === undefined) return undefined
    return { role: defaultRole, parts: normalizedContentParts(value, false) }
  }
  const role = typeof message.role === 'string' ? message.role : defaultRole
  const reasoning = message.is_reasoning === true || role === 'reasoning'
  const rawParts = message.parts ?? message.content ?? message.text
  let parts = normalizedContentParts(rawParts, reasoning)
  const calls = message.tool_calls ?? message.toolCalls
  if (Array.isArray(calls)) {
    const aliasCalls = calls.flatMap((call): NormalizedPart[] => {
      const normalized = normalizedToolCall(call)
      return normalized === undefined ? [] : [normalized]
    })
    parts = mergeToolCallRepresentations(parts, aliasCalls)
  }
  const openjiuwen = object(message.openjiuwen)
  const promptAttachmentHistory = openjiuwen?.kind === 'prompt_attachment_history'
    && (openjiuwen.mode === 'snapshot' || openjiuwen.mode === 'delta')
    ? {
        kind: openjiuwen.kind,
        mode: openjiuwen.mode,
      } as const
    : undefined
  return {
    role: role === 'reasoning' ? 'assistant' : role,
    parts,
    ...(promptAttachmentHistory === undefined
      ? {}
      : { openjiuwen: promptAttachmentHistory }),
  }
}

function normalizedMessages(value: unknown, defaultRole: string): readonly NormalizedMessage[] {
  const envelope = object(value)
  if (envelope !== undefined) {
    if (Array.isArray(envelope.choices)) {
      return envelope.choices.flatMap((choice): readonly NormalizedMessage[] => {
        const choiceObject = object(choice)
        return normalizedMessages(choiceObject?.message ?? choice, defaultRole)
      })
    }
    const nested = envelope.messages ?? envelope.message ?? envelope.input ?? envelope.output
    if (nested !== undefined && nested !== value) return normalizedMessages(nested, defaultRole)
  }
  const candidates = Array.isArray(value) ? value : [value]
  return candidates.flatMap((candidate): NormalizedMessage[] => {
    const message = normalizedMessage(candidate, defaultRole)
    return message === undefined ? [] : [message]
  })
}

function hasMessageContentShape(message: Record<string, unknown>): boolean {
  return message.parts !== undefined
    || message.content !== undefined
    || message.text !== undefined
    || message.tool_calls !== undefined
    || message.toolCalls !== undefined
}

function normalizedStructuredMessages(
  value: unknown,
  defaultRole: string,
): readonly NormalizedMessage[] | undefined {
  if (Array.isArray(value)) {
    if (value.length === 0) return []
    const messages = value.flatMap((candidate): NormalizedMessage[] => {
      const candidateObject = object(candidate)
      if (candidateObject === undefined || !hasMessageContentShape(candidateObject)) return []
      const message = normalizedMessage(candidateObject, defaultRole)
      return message === undefined ? [] : [message]
    })
    return messages.length === 0 ? undefined : messages
  }
  const envelope = object(value)
  if (envelope === undefined) return undefined
  if (Array.isArray(envelope.choices)) {
    if (envelope.choices.length === 0) return []
    const messages = envelope.choices.flatMap((choice): NormalizedMessage[] => {
      const choiceObject = object(choice)
      const normalized = normalizedStructuredMessages(choiceObject?.message ?? choice, defaultRole)
      return normalized === undefined ? [] : [...normalized]
    })
    return messages.length === 0 ? undefined : messages
  }
  const nested = envelope.messages ?? envelope.message ?? envelope.input ?? envelope.output
  if (nested !== undefined && nested !== value) {
    return normalizedStructuredMessages(nested, defaultRole)
  }
  if (!hasMessageContentShape(envelope)) return undefined
  const message = normalizedMessage(envelope, defaultRole)
  return message === undefined ? undefined : [message]
}

function indexedMessages(
  attributes: OtlpAttributeMap,
  prefix: string,
  defaultRole: string,
): { messages: readonly NormalizedMessage[]; complete: boolean } | undefined {
  const indexes = new Set<number>()
  const expression = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.(\\d+)\\.`)
  for (const key of attributes.keys()) {
    const match = expression.exec(key)
    if (match?.[1] !== undefined) indexes.add(Number(match[1]))
  }
  if (indexes.size === 0) return undefined
  const messages: NormalizedMessage[] = []
  for (const index of [...indexes].sort((left, right) => left - right)) {
    const role = readStringAttribute(attributes, `${prefix}.${index}.role`) ?? defaultRole
    const contentValue = attributes.get(`${prefix}.${index}.content`)
    const content = contentValue === undefined ? undefined : flexibleValue(contentValue)
    const reasoning = readBooleanAttribute(attributes, `${prefix}.${index}.is_reasoning`) === true
      || role === 'reasoning'
    const parts = normalizedContentParts(content, reasoning)
    const toolCallsValue = attributes.get(`${prefix}.${index}.tool_calls`)
    const toolCalls = toolCallsValue === undefined ? undefined : flexibleValue(toolCallsValue)
    if (Array.isArray(toolCalls)) {
      for (const toolCall of toolCalls) {
        const normalized = normalizedToolCall(toolCall)
        if (normalized !== undefined) parts.push(normalized)
      }
    }
    messages.push({ role: role === 'reasoning' ? 'assistant' : role, parts })
  }
  const orderedIndexes = [...indexes].sort((left, right) => left - right)
  return {
    messages,
    complete: orderedIndexes[0] === 0
      && orderedIndexes.every((index, position) => index === position),
  }
}

function resolveMessages(
  attributes: OtlpAttributeMap,
  structuredKeys: readonly string[],
  indexedPrefixes: readonly string[],
  observationKey: string,
  scalarKeys: readonly string[],
  defaultRole: string,
): Resolved<unknown> | undefined {
  for (const key of structuredKeys) {
    const value = attributes.get(key)
    if (value === undefined) continue
    const messages = normalizedStructuredMessages(flexibleValue(value), defaultRole)
    if (messages !== undefined) return { key, value: messages, complete: true }
  }
  for (const prefix of indexedPrefixes) {
    const indexed = indexedMessages(attributes, prefix, defaultRole)
    if (indexed !== undefined) {
      return { key: `${prefix}.*`, value: indexed.messages, complete: indexed.complete }
    }
  }
  const observation = resolveFlexible(attributes, [observationKey])
  if (observation !== undefined) {
    const messages = normalizedMessages(observation.value, defaultRole)
    if (messages.length > 0 && messages.some(message => message.parts.length > 0)) {
      return { key: observation.key, value: messages }
    }
  }
  const scalar = resolveFlexible(attributes, scalarKeys)
  return scalar === undefined
    ? undefined
    : { key: scalar.key, value: normalizedMessages(scalar.value, defaultRole) }
}

function resolveStructuredParts(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
): Resolved<unknown> | undefined {
  for (const key of keys) {
    const value = attributes.get(key)
    if (value === undefined) continue
    const structured = flexibleValue(value)
    if (!Array.isArray(structured)) continue
    return { key, value: normalizedContentParts(structured, false) }
  }
  return undefined
}

function resolveStructuredArray(
  attributes: OtlpAttributeMap,
  keys: readonly string[],
): Resolved<unknown> | undefined {
  for (const key of keys) {
    const value = attributes.get(key)
    if (value === undefined) continue
    const structured = flexibleValue(value)
    if (Array.isArray(structured)) return { key, value: structured }
  }
  return undefined
}

function systemFromMessages(value: unknown): readonly NormalizedPart[] | undefined {
  if (!Array.isArray(value)) return undefined
  const systemParts = value.flatMap((candidate): NormalizedPart[] => {
    const message = object(candidate)
    if (message?.role !== 'system' || !Array.isArray(message.parts)) return []
    return message.parts.flatMap((part): NormalizedPart[] => {
      const valuePart = object(part)
      return valuePart === undefined || typeof valuePart.type !== 'string'
        ? []
        : [valuePart as unknown as NormalizedPart]
    })
  })
  return systemParts.length === 0 ? undefined : systemParts
}

/**
 * Rebuild the full request as one ordered message list.
 *
 * The standard attributes split the request in two: the instructions given
 * outside the chat history, and the history itself. Views that reason about
 * where a message sat -- which system turn is the prompt-attachment snapshot,
 * above all -- need them back in one sequence, instructions first.
 */
function composedRequestMessages(
  attributes: OtlpAttributeMap,
): Resolved<unknown> | undefined {
  const rawInput = attributes.get(STANDARD_ATTRIBUTES.inputMessages)
  if (rawInput === undefined) return undefined
  const history = normalizedStructuredMessages(flexibleValue(rawInput), 'user')
  if (history === undefined) return undefined

  const instructions = resolveStructuredParts(attributes, [
    STANDARD_ATTRIBUTES.systemInstructions,
  ])
  const leading = instructions === undefined
    ? []
    : [{ role: 'system', parts: instructions.value }]
  return {
    key: STANDARD_ATTRIBUTES.inputMessages,
    value: [...leading, ...history],
    complete: true,
  }
}


function withoutSystemMessages(value: unknown): unknown {
  if (!Array.isArray(value)) return value
  return value.filter((candidate) => object(candidate)?.role !== 'system')
}

/** Resolve each fact independently, preserving the winning physical key. */
export function normalizeTrajectoryAttributes(
  entries: readonly OtlpKeyValue[] | undefined,
): NormalizedTrajectoryAttributes {
  const raw = exactAttributeMap(entries)
  const target: MutableNormalized = { raw, sources: {} }
  const observationOutput = langfuseOutput(raw)

  assign(target, 'conversationId', resolveString(raw, [
    STANDARD_ATTRIBUTES.conversationId,
    OPENJIUWEN_ATTRIBUTES.sessionId,
    STANDARD_ATTRIBUTES.sessionId,
    LEGACY.openJiuwenSessionId,
    LEGACY.agentTeamSessionId,
  ]))
  assign(target, 'traceRoot', resolveBoolean(raw, [
    OPENJIUWEN_ATTRIBUTES.traceRoot,
  ]))
  assign(target, 'traceSchemaVersion', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.traceSchemaVersion,
    DSH_ATTRIBUTES.schemaVersion,
  ]))
  assign(target, 'traceComplete', resolveBoolean(raw, [
    OPENJIUWEN_ATTRIBUTES.traceComplete,
  ]))
  assign(target, 'traceForcedClose', resolveBoolean(raw, [
    OPENJIUWEN_ATTRIBUTES.traceForcedClose,
  ]))
  assign(target, 'spanForcedClose', resolveBoolean(raw, [
    OPENJIUWEN_ATTRIBUTES.spanForcedClose,
  ]))
  assign(target, 'spanForcedCloseReason', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.spanForcedCloseReason,
  ]))
  assign(target, 'operationName', resolveString(raw, [
    STANDARD_ATTRIBUTES.operationName,
  ]))
  assign(target, 'providerName', resolveString(raw, [
    STANDARD_ATTRIBUTES.providerName,
  ]))
  assign(target, 'requestId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.requestId,
  ]))
  assign(target, 'runId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.runId,
  ]))
  assign(target, 'turnId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.turnId,
  ]))
  assign(target, 'stepId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.stepId,
  ]))
  assign(target, 'inferenceId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.inferenceId,
  ]))
  assign(target, 'agentMode', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.agentMode,
  ]))
  assign(target, 'requestModel', resolveString(raw, [
    STANDARD_ATTRIBUTES.requestModel,
  ]))
  assign(target, 'responseModel', resolveString(raw, [
    STANDARD_ATTRIBUTES.responseModel,
  ]))
  assign(target, 'requestMaxTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.requestMaxTokens,
  ]))
  assign(target, 'requestTemperature', resolveNumber(raw, [
    STANDARD_ATTRIBUTES.requestTemperature,
  ]))
  assign(target, 'requestTopP', resolveNumber(raw, [
    STANDARD_ATTRIBUTES.requestTopP,
  ]))
  assign(target, 'requestStopSequences', resolveStringArray(raw, [
    STANDARD_ATTRIBUTES.requestStopSequences,
  ]))
  assign(target, 'requestStream', resolveBoolean(raw, [
    STANDARD_ATTRIBUTES.requestStream,
  ]))
  assign(target, 'requestReasoningLevel', resolveString(raw, [
    STANDARD_ATTRIBUTES.requestReasoningLevel,
  ]))
  assign(target, 'responseId', resolveString(raw, [
    STANDARD_ATTRIBUTES.responseId,
  ]))
  assign(target, 'responseFinishReasons', resolveStringArray(raw, [
    STANDARD_ATTRIBUTES.responseFinishReasons,
    LEGACY.responseFinishReason,
  ]) ?? langfuseFinishReasons(observationOutput))

  const firstChunk = resolveNonNegativeNumber(raw, [
    STANDARD_ATTRIBUTES.responseTimeToFirstChunk,
  ])
  if (firstChunk !== undefined) {
    assign(target, 'responseTimeToFirstChunkSeconds', firstChunk)
  } else {
    const legacyFirstToken = resolveNonNegativeNumber(raw, [LEGACY.responseTimeToFirstTokenMs])
    if (legacyFirstToken !== undefined) {
      assign(target, 'responseTimeToFirstChunkSeconds', {
        key: legacyFirstToken.key,
        value: legacyFirstToken.value / 1_000,
      })
    }
  }

  // Token usage is read only in the standard shape, where every cache and
  // reasoning count is a breakdown of the input respectively output total.
  // Legacy and Langfuse spellings carve the cache out of the input instead, so
  // mixing them would silently double-subtract cached tokens.
  assign(target, 'usageInputTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.usageInputTokens,
  ]))
  assign(target, 'usageOutputTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.usageOutputTokens,
  ]))
  assign(target, 'usageReasoningTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.usageReasoningTokens,
  ]))
  assign(target, 'usageCacheReadTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.usageCacheReadTokens,
  ]))
  assign(target, 'usageCacheCreationTokens', resolveNonNegativeInt64(raw, [
    STANDARD_ATTRIBUTES.usageCacheCreationTokens,
  ]))
  assign(target, 'inputCost', resolveNonNegativeNumber(raw, [
    OPENJIUWEN_ATTRIBUTES.inputCost,
  ]))
  assign(target, 'outputCost', resolveNonNegativeNumber(raw, [
    OPENJIUWEN_ATTRIBUTES.outputCost,
  ]))
  assign(target, 'totalCost', resolveNonNegativeNumber(raw, [
    OPENJIUWEN_ATTRIBUTES.totalCost,
  ]))
  assign(target, 'totalLatencyMs', resolveNonNegativeNumber(raw, [
    OPENJIUWEN_ATTRIBUTES.totalLatencyMs,
  ]))
  assign(target, 'timePerOutputTokenMs', resolveNonNegativeNumber(raw, [
    OPENJIUWEN_ATTRIBUTES.timePerOutputTokenMs,
  ]))
  assign(target, 'promptTokenIds', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.promptTokenIds,
  ]))
  assign(target, 'completionTokenIds', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.completionTokenIds,
  ]))
  assign(target, 'logprobs', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.logprobs,
  ]))
  assign(target, 'parserResult', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.parserResult,
  ]))
  assign(target, 'providerMetadata', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.providerMetadata,
  ]))

  assign(target, 'agentId', resolveString(raw, [
    STANDARD_ATTRIBUTES.agentId,
  ]))
  assign(target, 'agentName', resolveString(raw, [
    STANDARD_ATTRIBUTES.agentName,
    LEGACY.deepAgentName,
  ]))
  assign(target, 'agentVersion', resolveString(raw, [
    STANDARD_ATTRIBUTES.agentVersion,
  ]))
  assign(target, 'agentDescription', resolveString(raw, [
    STANDARD_ATTRIBUTES.agentDescription,
  ]))
  assign(target, 'executionSubjectId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.executionSubjectId,
  ]))
  assign(target, 'executionSubjectKind', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.executionSubjectKind,
  ]))
  assign(target, 'executionSubjectParentId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.executionSubjectParentId,
  ]))
  assign(target, 'executionSubjectSessionId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.executionSubjectSessionId,
  ]))
  assign(target, 'executionSubjectRequestNumber', resolvePositiveInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.executionSubjectRequestNumber,
  ]))

  const requestMessages = composedRequestMessages(raw)
  assign(target, 'requestMessages', requestMessages)
  if (requestMessages !== undefined) {
    target.requestMessagesComplete = requestMessages.complete ?? true
  }

  const inputMessages = resolveMessages(
    raw,
    [
      STANDARD_ATTRIBUTES.inputMessages,
    ],
    [],
    LEGACY.langfuseInput,
    [],
    'user',
  )
  if (inputMessages !== undefined) {
    const explicitSystem = resolveStructuredParts(raw, [
      STANDARD_ATTRIBUTES.systemInstructions,
    ])
    const inferredSystem = systemFromMessages(inputMessages.value)
    const system = explicitSystem ?? (
      inferredSystem === undefined
        ? undefined
        : { key: inputMessages.key, value: inferredSystem }
    )
    assign(target, 'systemInstructions', system)
    assign(target, 'inputMessages', {
      key: inputMessages.key,
      value: withoutSystemMessages(inputMessages.value),
    })
    target.inputMessagesComplete = inputMessages.complete ?? true
  } else {
    assign(target, 'systemInstructions', resolveStructuredParts(raw, [
      STANDARD_ATTRIBUTES.systemInstructions,
    ]))
  }
  assign(target, 'inputMessageProvenance', resolveFlexible(raw, [
    OPENJIUWEN_ATTRIBUTES.inputMessageProvenance,
  ]))

  const outputMessages = resolveMessages(
    raw,
    [
      STANDARD_ATTRIBUTES.outputMessages,
    ],
    [],
    LEGACY.langfuseOutput,
    [],
    'assistant',
  )
  if (outputMessages !== undefined) {
    const calls = resolveFlexible(raw, [LEGACY.toolCalls])
    const aliasCalls = calls === undefined || !Array.isArray(calls.value)
      ? []
      : calls.value.flatMap((call): NormalizedPart[] => {
        const normalized = normalizedToolCall(call)
        return normalized === undefined ? [] : [normalized]
      })
    const messages = normalizedMessages(outputMessages.value, 'assistant').map((message, index) => {
      if (index !== 0 || aliasCalls.length === 0) return message
      return { ...message, parts: mergeToolCallRepresentations(message.parts, aliasCalls) }
    })
    assign(target, 'outputMessages', { key: outputMessages.key, value: messages })
  }

  assign(target, 'toolDefinitions', resolveStructuredArray(raw, [
    STANDARD_ATTRIBUTES.toolDefinitions,
  ]))
  assign(target, 'toolName', resolveString(raw, [
    STANDARD_ATTRIBUTES.toolName,
  ]))
  assign(target, 'toolCallId', resolveString(raw, [
    STANDARD_ATTRIBUTES.toolCallId,
    LEGACY.toolCallId,
  ]))
  assign(target, 'toolType', resolveString(raw, [
    STANDARD_ATTRIBUTES.toolType,
  ]))
  assign(target, 'toolDescription', resolveString(raw, [
    STANDARD_ATTRIBUTES.toolDescription,
  ]))
  assign(target, 'toolResourceId', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.toolResourceId,
    STANDARD_ATTRIBUTES.toolId,
  ]))
  assign(target, 'openJiuwenToolType', resolveString(raw, [
    OPENJIUWEN_ATTRIBUTES.toolType,
  ]))
  assign(target, 'toolAuthoritative', resolveBoolean(raw, [
    OPENJIUWEN_ATTRIBUTES.toolAuthoritative,
  ]))
  assign(target, 'toolCallArguments', resolveFlexible(raw, [
    STANDARD_ATTRIBUTES.toolCallArguments,
    LEGACY.toolCallArguments,
    LEGACY.langfuseInput,
  ]))
  assign(target, 'toolCallResult', resolveFlexible(raw, [
    STANDARD_ATTRIBUTES.toolCallResult,
    LEGACY.toolCallResult,
    LEGACY.langfuseOutput,
  ]))

  assign(target, 'sourceSequence', resolveNonNegativeInt64(raw, [
    DSH_ATTRIBUTES.sessionSourceSequence,
  ]))
  assign(target, 'turnNumber', resolvePositiveInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.turnNumber,
    DSH_ATTRIBUTES.turnNumber,
  ]))
  assign(target, 'stepNumber', resolvePositiveInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.stepNumber,
    DSH_ATTRIBUTES.stepNumber,
    LEGACY.deepAgentIteration,
  ]))
  assign(target, 'trajectoryKind', resolveClosedString(raw, [
    OPENJIUWEN_ATTRIBUTES.trajectoryKind,
    DSH_ATTRIBUTES.trajectoryKind,
  ], TRAJECTORY_KINDS))
  assign(target, 'requestPurpose', resolveClosedString(raw, [
    OPENJIUWEN_ATTRIBUTES.requestPurpose,
    DSH_ATTRIBUTES.requestPurpose,
  ], REQUEST_PURPOSES))
  assign(target, 'requestNumber', resolvePositiveInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.requestNumber,
    DSH_ATTRIBUTES.requestNumber,
  ]))
  assign(target, 'requestRetryCount', resolveNonNegativeInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.requestRetryCount,
    DSH_ATTRIBUTES.requestRetryCount,
  ]))
  assign(target, 'requestMaxRetries', resolveNonNegativeInt64(raw, [
    OPENJIUWEN_ATTRIBUTES.requestMaxRetries,
    DSH_ATTRIBUTES.requestMaxRetries,
  ]))
  assign(target, 'messageSourceKind', resolveString(raw, [
    DSH_ATTRIBUTES.messageSourceKind,
  ]))
  assign(target, 'messageSourcePlugin', resolveString(raw, [
    DSH_ATTRIBUTES.messageSourcePlugin,
  ]))
  assign(target, 'compactionInputTokens', resolveNonNegativeInt64(raw, [
    DSH_ATTRIBUTES.compactionInputTokens,
  ]))
  assign(target, 'compactionSummary', resolveString(raw, [
    DSH_ATTRIBUTES.compactionSummary,
  ]))
  assign(target, 'langfuseObservationType', resolveString(raw, [
    LEGACY.langfuseObservationType,
  ]))
  assign(target, 'errorType', resolveString(raw, [
    STANDARD_ATTRIBUTES.errorType,
  ]))

  return target
}

function safeEventSequence(value: bigint | undefined, fallback: number): number {
  if (value === undefined || value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) return fallback
  return Number(value)
}

/** Normalize replayable chunk events without treating ended spans as live revisions. */
export function normalizeTrajectoryStreamEvents(
  events: readonly OtlpSpanEvent[] | undefined,
): readonly NormalizedTrajectoryStreamEvent[] {
  const normalized: Array<NormalizedTrajectoryStreamEvent & { order: number }> = []
  for (const [order, event] of (events ?? []).entries()) {
    if (event.name === OPENJIUWEN_EVENTS.legacyStreamChunk) {
      normalized.push({
        sequence: order,
        kind: 'lifecycle',
        source: event.name,
        order,
      })
      continue
    }
    if (event.name !== OPENJIUWEN_EVENTS.streamChunk && event.name !== DSH_EVENTS.streamChunk) continue
    const attributes = exactAttributeMap(event.attributes)
    const sequence = safeEventSequence(
      resolveNonNegativeInt64(attributes, [
        OPENJIUWEN_ATTRIBUTES.eventSequence,
        DSH_ATTRIBUTES.eventSequence,
        DSH_ATTRIBUTES.streamSequence,
      ])?.value,
      order,
    )
    const kind = resolveClosedString(attributes, [
      OPENJIUWEN_ATTRIBUTES.streamKind,
      DSH_ATTRIBUTES.streamKind,
    ], STREAM_KINDS)?.value ?? 'lifecycle'
    const textValue = resolveString(attributes, [
      OPENJIUWEN_ATTRIBUTES.streamText,
      DSH_ATTRIBUTES.streamText,
    ])?.value
    const toolCallId = resolveString(attributes, [
      OPENJIUWEN_ATTRIBUTES.streamToolCallId,
      DSH_ATTRIBUTES.streamToolCallId,
    ])?.value
    const toolName = resolveString(attributes, [
      OPENJIUWEN_ATTRIBUTES.streamToolName,
      DSH_ATTRIBUTES.streamToolName,
    ])?.value
    const argumentsDelta = resolveString(attributes, [
      OPENJIUWEN_ATTRIBUTES.streamArgumentsDelta,
      DSH_ATTRIBUTES.streamArgumentsDelta,
    ])?.value
    normalized.push({
      sequence,
      kind,
      source: event.name,
      ...(textValue === undefined ? {} : { text: textValue }),
      ...(toolCallId === undefined ? {} : { toolCallId }),
      ...(toolName === undefined ? {} : { toolName }),
      ...(argumentsDelta === undefined ? {} : { argumentsDelta }),
      order,
    })
  }
  const ordered = normalized
    .sort((left, right) => left.sequence - right.sequence || left.order - right.order)
  const hasReplayableEvent = ordered.some(event => event.kind !== 'lifecycle')
  const selected = new Map<string, typeof ordered[number]>()
  const priority = (source: string): number => source === OPENJIUWEN_EVENTS.streamChunk
    ? 0
    : source === DSH_EVENTS.streamChunk ? 1 : 2
  for (const event of ordered) {
    if (hasReplayableEvent && event.kind === 'lifecycle') continue
    const identity = [
      event.sequence,
      event.kind,
      event.toolCallId ?? '',
      event.toolName ?? '',
    ].join('\u0000')
    const existing = selected.get(identity)
    if (existing === undefined || priority(event.source) < priority(existing.source)) {
      selected.set(identity, event)
    }
  }
  return [...selected.values()]
    .sort((left, right) => left.sequence - right.sequence || left.order - right.order)
    .map(event => ({
      sequence: event.sequence,
      kind: event.kind,
      source: event.source,
      ...(event.text === undefined ? {} : { text: event.text }),
      ...(event.toolCallId === undefined ? {} : { toolCallId: event.toolCallId }),
      ...(event.toolName === undefined ? {} : { toolName: event.toolName }),
      ...(event.argumentsDelta === undefined ? {} : { argumentsDelta: event.argumentsDelta }),
    }))
}
