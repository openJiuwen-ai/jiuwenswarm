// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** OTLP/JSON trace types and normalized accessors used on both sides of the transport. */

export type OtlpAnyValue =
  | { stringValue: string }
  | { boolValue: boolean }
  | { intValue: string }
  | { doubleValue: number }
  | { bytesValue: string }
  | { arrayValue: { values?: OtlpAnyValue[] } }
  | { kvlistValue: { values?: OtlpKeyValue[] } }

export interface OtlpKeyValue {
  key: string
  value: OtlpAnyValue
}

export interface OtlpResource {
  attributes?: OtlpKeyValue[]
  droppedAttributesCount?: number
}

export interface OtlpInstrumentationScope {
  name?: string
  version?: string
  attributes?: OtlpKeyValue[]
  droppedAttributesCount?: number
}

export interface OtlpSpanEvent {
  timeUnixNano: string
  name: string
  attributes?: OtlpKeyValue[]
  droppedAttributesCount?: number
}

export interface OtlpSpanLink {
  traceId: string
  spanId: string
  traceState?: string
  attributes?: OtlpKeyValue[]
  droppedAttributesCount?: number
  flags?: number
}

export interface OtlpStatus {
  message?: string
  code?: number
}

export interface OtlpSpan {
  traceId: string
  spanId: string
  traceState?: string
  parentSpanId?: string
  flags?: number
  name: string
  kind?: number
  startTimeUnixNano: string
  /** Absent on an additive provisional snapshot; required on authoritative OTLP final records. */
  endTimeUnixNano?: string
  attributes?: OtlpKeyValue[]
  droppedAttributesCount?: number
  events?: OtlpSpanEvent[]
  droppedEventsCount?: number
  links?: OtlpSpanLink[]
  droppedLinksCount?: number
  status?: OtlpStatus
}

export interface OtlpScopeSpans {
  scope?: OtlpInstrumentationScope
  spans?: OtlpSpan[]
  schemaUrl?: string
}

export interface OtlpResourceSpans {
  resource?: OtlpResource
  scopeSpans?: OtlpScopeSpans[]
  schemaUrl?: string
}

/** One physical observability JSONL record. */
export interface OtlpExportTraceServiceRequest {
  resourceSpans: OtlpResourceSpans[]
}

/** One-span profile extracted from an accepted JSONL line. */
export interface NormalizedTraceRecord {
  request: OtlpExportTraceServiceRequest
  resourceSpans: OtlpResourceSpans
  scopeSpans: OtlpScopeSpans
  span: OtlpSpan
  traceId: string
  spanId: string
  parentSpanId: string | null
  startTimeUnixNano: bigint
  endTimeUnixNano: bigint
}

/** Convert an OTLP AnyValue into its JSON-compatible value. */
export function otlpValue(value: OtlpAnyValue): unknown {
  if ('stringValue' in value) return value.stringValue
  if ('boolValue' in value) return value.boolValue
  if ('intValue' in value) return value.intValue
  if ('doubleValue' in value) return value.doubleValue
  if ('bytesValue' in value) return value.bytesValue
  if ('arrayValue' in value) return (value.arrayValue.values ?? []).map(otlpValue)
  if ('kvlistValue' in value) return Object.fromEntries(
    (value.kvlistValue.values ?? []).map(item => [item.key, otlpValue(item.value)]),
  )
  return undefined
}

/** Build a last-value-wins attribute map from OTLP key/value entries. */
export function attributeMap(attributes: readonly OtlpKeyValue[] | undefined): ReadonlyMap<string, unknown> {
  const result = new Map<string, unknown>()
  for (const attribute of attributes ?? []) result.set(attribute.key, otlpValue(attribute.value))
  return result
}

/** Read a string attribute without coercing another OTLP value type. */
export function stringAttribute(
  attributes: readonly OtlpKeyValue[] | undefined,
  key: string,
): string | undefined {
  const value = attributeMap(attributes).get(key)
  return typeof value === 'string' ? value : undefined
}

/** Read a finite numeric attribute, including the OTLP int64 decimal-string representation. */
export function numberAttribute(
  attributes: readonly OtlpKeyValue[] | undefined,
  key: string,
): number | undefined {
  const value = attributeMap(attributes).get(key)
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || value.trim() === '') return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}
