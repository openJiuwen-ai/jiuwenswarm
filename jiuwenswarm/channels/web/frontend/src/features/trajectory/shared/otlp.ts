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

/**
 * A last-value-wins attribute map that converts a value when it is read.
 *
 * An attribute restating a model call's history assembles its text on every
 * read, and the lookups on this map name one key at a time: converting every
 * entry up front would assemble each history to answer a question about
 * another attribute.
 */
class ConvertedAttributeMap implements ReadonlyMap<string, unknown> {
  readonly #values: ReadonlyMap<string, OtlpAnyValue>

  constructor(values: ReadonlyMap<string, OtlpAnyValue>) {
    this.#values = values
  }

  get size(): number {
    return this.#values.size
  }

  get(key: string): unknown {
    const value = this.#values.get(key)
    return value === undefined ? undefined : otlpValue(value)
  }

  has(key: string): boolean {
    return this.#values.has(key)
  }

  forEach(
    callback: (value: unknown, key: string, map: ReadonlyMap<string, unknown>) => void,
    thisArg?: unknown,
  ): void {
    this.#converted().forEach((value, key) => callback.call(thisArg, value, key, this))
  }

  entries(): MapIterator<[string, unknown]> {
    return this.#converted().entries()
  }

  keys(): MapIterator<string> {
    return this.#values.keys()
  }

  values(): MapIterator<unknown> {
    return this.#converted().values()
  }

  [Symbol.iterator](): MapIterator<[string, unknown]> {
    return this.entries()
  }

  // Walking the whole map is what reads every value, so only it converts all.
  #converted(): Map<string, unknown> {
    const converted = new Map<string, unknown>()
    for (const [key, value] of this.#values) converted.set(key, otlpValue(value))
    return converted
  }
}

/** Build a last-value-wins attribute map from OTLP key/value entries. */
export function attributeMap(attributes: readonly OtlpKeyValue[] | undefined): ReadonlyMap<string, unknown> {
  const values = new Map<string, OtlpAnyValue>()
  for (const attribute of attributes ?? []) values.set(attribute.key, attribute.value)
  return new ConvertedAttributeMap(values)
}

/** The value the last entry for `key` states, read without touching the others. */
function lastAttributeValue(
  attributes: readonly OtlpKeyValue[] | undefined,
  key: string,
): unknown {
  if (attributes === undefined) return undefined
  for (let index = attributes.length - 1; index >= 0; index -= 1) {
    const attribute = attributes[index]
    if (attribute?.key === key) return otlpValue(attribute.value)
  }
  return undefined
}

/** Read a string attribute without coercing another OTLP value type. */
export function stringAttribute(
  attributes: readonly OtlpKeyValue[] | undefined,
  key: string,
): string | undefined {
  const value = lastAttributeValue(attributes, key)
  return typeof value === 'string' ? value : undefined
}

/** Read a finite numeric attribute, including the OTLP int64 decimal-string representation. */
export function numberAttribute(
  attributes: readonly OtlpKeyValue[] | undefined,
  key: string,
): number | undefined {
  const value = lastAttributeValue(attributes, key)
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || value.trim() === '') return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}
