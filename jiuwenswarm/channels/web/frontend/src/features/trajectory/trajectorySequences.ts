/**
 * Rebuilding the attributes a record states by reference.
 *
 * Storage keeps one copy of each distinct message, tool definition and system
 * instruction, and each record names the chain it used rather than repeating
 * its content. A page therefore arrives as references plus a dictionary of
 * what this reader was not assumed to already hold, and assembly happens here.
 *
 * The cache is what makes that assumption safe to make: content is addressed
 * by the hash of itself, so an entry can never go stale, and a reader that
 * still holds one needs nothing from the server to use it again.
 */

import type { TrajectoryDetailRecord } from './trajectoryClient'
import type { OtlpAnyValue, OtlpExportTraceServiceRequest } from './shared/otlp'
import {
  sequenceBackedValue,
  sequenceContentOf,
  sequenceText,
} from './shared/sequenceValue.ts'
import type { SequenceContent } from './shared/sequenceValue.ts'
import {
  SEQUENCE_REFERENCE_PREFIX,
  SEQUENCE_REFERENCE_VERSION,
} from './semconv/openjiuwen-semconv.generated.ts'

/** Content held by hash, plus the chains seen so far. */
export interface SequenceCache {
  blobs: Map<string, string>
  chains: Map<string, readonly string[]>
  /** Chains resolved in full, shared by every record that states them. */
  contents: Map<string, SequenceContent>
  /** Elements parsed so far, by hash; `NOT_JSON` for one that is not JSON. */
  parsed: Map<string, unknown>
}

export function createSequenceCache(): SequenceCache {
  return { blobs: new Map(), chains: new Map(), contents: new Map(), parsed: new Map() }
}

/** Read the chain a stored attribute names, or null for a plain value. */
export function parseSequenceReference(value: unknown): { hash: string; depth: number } | null {
  if (typeof value !== 'string' || !value.startsWith(SEQUENCE_REFERENCE_PREFIX)) return null
  const parts = value.split(':')
  if (parts.length !== 4 || parts[1] !== SEQUENCE_REFERENCE_VERSION) return null
  const depth = Number(parts[3])
  if (!parts[2] || !Number.isSafeInteger(depth) || depth < 0) return null
  return { hash: parts[2], depth }
}

/**
 * Take in what one page delivered.
 *
 * Both dictionaries are additive: the server sends a chain's elements every
 * time, and its content only when this reader was not assumed to hold it.
 */
export function absorbSequencePage(
  cache: SequenceCache,
  page: {
    sequences?: Record<string, readonly string[]>
    blobs?: Record<string, string>
  },
): void {
  for (const [hash, elements] of Object.entries(page.sequences ?? {})) {
    if (Array.isArray(elements)) cache.chains.set(hash, elements)
  }
  for (const [hash, content] of Object.entries(page.blobs ?? {})) {
    if (typeof content === 'string') cache.blobs.set(hash, content)
  }
}

/** Element hashes a page refers to but whose content the cache lacks. */
export function missingSequenceContent(
  cache: SequenceCache,
  heads: Iterable<string>,
): string[] {
  const missing = new Set<string>()
  for (const head of heads) {
    const elements = cache.chains.get(head)
    if (elements === undefined) {
      missing.add(head)
      continue
    }
    for (const element of elements) {
      if (!cache.blobs.has(element)) missing.add(element)
    }
  }
  return [...missing]
}

/**
 * Rebuild the value a chain states.
 *
 * Always an array: a chain is built only from one, so it rebuilds into one
 * at any depth. Nothing here inspects the count.
 */
export function rebuildSequenceValue(
  cache: SequenceCache,
  hash: string,
): string | undefined {
  const content = sequenceContent(cache, hash)
  return content === undefined ? undefined : sequenceText(content.elements)
}

/**
 * The content a chain states, once every element of it is held.
 *
 * Resolved once per chain and shared: a run restates the same tool list and
 * instructions on every call, and each call's history extends the last.
 */
function sequenceContent(
  cache: SequenceCache,
  hash: string,
): SequenceContent | undefined {
  const known = cache.contents.get(hash)
  if (known !== undefined) return known
  const hashes = cache.chains.get(hash)
  if (hashes === undefined) return undefined
  const elements: string[] = []
  for (const element of hashes) {
    const content = cache.blobs.get(element)
    if (content === undefined) return undefined
    elements.push(content)
  }
  let parsedElements: readonly unknown[] | null | undefined
  const content: SequenceContent = {
    hash,
    elements,
    parsedElements: () => {
      parsedElements ??= parseElements(cache, hashes, elements)
      return parsedElements ?? undefined
    },
  }
  cache.contents.set(hash, content)
  return content
}

// What an element that is not JSON parses to. Not null: `null` is a JSON
// document an element may well state.
const NOT_JSON: unique symbol = Symbol('trajectory.notJson')

function parseElements(
  cache: SequenceCache,
  hashes: readonly string[],
  elements: readonly string[],
): readonly unknown[] | null {
  const parsed: unknown[] = []
  for (const [index, hash] of hashes.entries()) {
    if (!cache.parsed.has(hash)) {
      let value: unknown
      try {
        value = JSON.parse(elements[index] as string) as unknown
      } catch {
        value = NOT_JSON
      }
      cache.parsed.set(hash, value)
    }
    const value = cache.parsed.get(hash)
    if (value === NOT_JSON) return null
    parsed.push(value)
  }
  return parsed
}

type UnknownRecord = Record<string, unknown>

const NO_ITEMS: readonly UnknownRecord[] = []

function rebuildSpanAttributes(
  span: UnknownRecord,
  cache: SequenceCache,
  unresolved: Set<string>,
): UnknownRecord {
  const attributes = span.attributes
  if (!Array.isArray(attributes)) return span
  let rebuiltAttributes: unknown[] | null = null
  attributes.forEach((attribute: unknown, index) => {
    if (typeof attribute !== 'object' || attribute === null) return
    const entry = attribute as { key?: unknown; value?: unknown }
    const value = entry.value
    if (typeof value !== 'object' || value === null) return
    const reference = parseSequenceReference((value as { stringValue?: unknown }).stringValue)
    if (reference === null) return
    const content = sequenceContent(cache, reference.hash)
    if (content === undefined) {
      // Leave the reference in place and say which attribute could not be
      // rebuilt. A missing element must not cost the reader the whole span.
      unresolved.add(String(entry.key ?? ''))
      return
    }
    rebuiltAttributes ??= [...attributes]
    rebuiltAttributes[index] = { ...entry, value: sequenceBackedValue(content) }
  })
  return rebuiltAttributes === null ? span : { ...span, attributes: rebuiltAttributes }
}

/** Map a list copy-on-write: the input itself when no item changed. */
function mapChanged<T>(items: readonly T[], map: (item: T) => T): readonly T[] {
  let changed: T[] | null = null
  items.forEach((item, index) => {
    const next = map(item)
    if (next === item) return
    changed ??= [...items]
    changed[index] = next
  })
  return changed ?? items
}

/**
 * Rebuild one record's OTLP into the shape the projection expects.
 *
 * Only the spans that state a reference are copied; everything else is shared
 * with the received record, which is never modified. The record is returned by
 * reference when it states nothing to rebuild, so the projection cache keeps
 * recognizing it as unchanged.
 */
export function rebuildRecord(
  record: TrajectoryDetailRecord,
  cache: SequenceCache,
): TrajectoryDetailRecord {
  if (record.otlp === null || record.sequences === undefined) return record
  const unresolved = new Set<string>()
  const otlp = record.otlp as unknown as UnknownRecord
  const listOf = (value: unknown): readonly UnknownRecord[] => (
    Array.isArray(value) ? value as UnknownRecord[] : NO_ITEMS
  )
  const sourceResources = listOf(otlp.resourceSpans)
  const resourceSpans = mapChanged(sourceResources, (resource) => {
    const sourceScopes = listOf(resource.scopeSpans)
    const scopeSpans = mapChanged(sourceScopes, (scope) => {
      const sourceSpans = listOf(scope.spans)
      const spans = mapChanged(
        sourceSpans,
        span => rebuildSpanAttributes(span, cache, unresolved),
      )
      return spans === sourceSpans ? scope : { ...scope, spans }
    })
    return scopeSpans === sourceScopes ? resource : { ...resource, scopeSpans }
  })
  const changed = resourceSpans !== sourceResources
  if (!changed && unresolved.size === 0) return record
  return {
    ...record,
    otlp: (changed ? { ...otlp, resourceSpans } : otlp) as unknown as OtlpExportTraceServiceRequest,
    ...(unresolved.size === 0 ? {} : { incomplete_sequences: [...unresolved] }),
  } as TrajectoryDetailRecord
}

/**
 * The record with each value stated by reference turned into its text.
 *
 * Such a value assembles its text on every read. A view that walks the whole
 * record on each render -- the raw inspector -- reads one copy made here, so
 * a history is assembled once per record shown rather than once per render.
 */
export function withSequenceText(
  otlp: OtlpExportTraceServiceRequest,
): OtlpExportTraceServiceRequest {
  const listOf = (value: unknown): readonly UnknownRecord[] => (
    Array.isArray(value) ? value as UnknownRecord[] : NO_ITEMS
  )
  const source = otlp as unknown as UnknownRecord
  const sourceResources = listOf(source.resourceSpans)
  const resourceSpans = mapChanged(sourceResources, (resource) => {
    const sourceScopes = listOf(resource.scopeSpans)
    const scopeSpans = mapChanged(sourceScopes, (scope) => {
      const sourceSpans = listOf(scope.spans)
      const spans = mapChanged(sourceSpans, (span) => {
        const sourceAttributes = listOf(span.attributes)
        const attributes = mapChanged(sourceAttributes, (entry) => {
          const value = entry.value as OtlpAnyValue | undefined
          const content = value === undefined ? undefined : sequenceContentOf(value)
          return content === undefined
            ? entry
            : { ...entry, value: { stringValue: sequenceText(content.elements) } }
        })
        return attributes === sourceAttributes ? span : { ...span, attributes }
      })
      return spans === sourceSpans ? scope : { ...scope, spans }
    })
    return scopeSpans === sourceScopes ? resource : { ...resource, scopeSpans }
  })
  return resourceSpans === sourceResources
    ? otlp
    : { ...source, resourceSpans } as unknown as OtlpExportTraceServiceRequest
}

/**
 * Attribute keys each record could not rebuild, by `traceId:spanId`.
 *
 * Content asked for by hash and still not delivered is content the store no
 * longer holds. The projection needs to know which span lost what, so it can
 * say that rather than render the span as having said nothing.
 */
export function unresolvedAttributesByRecordId(
  records: readonly TrajectoryDetailRecord[],
): Map<string, readonly string[]> {
  const byRecord = new Map<string, readonly string[]>()
  for (const record of records) {
    const keys = record.incomplete_sequences
    if (keys === undefined || keys.length === 0) continue
    if (record.trace_id === undefined || record.span_id === undefined) continue
    byRecord.set(`${record.trace_id}:${record.span_id}`, keys)
  }
  return byRecord
}

/**
 * Chain heads a rebuild could not resolve.
 *
 * A page delivers content only when this reader was not assumed to hold it,
 * and that assumption can be wrong -- a reload, a second device, an entry
 * dropped from the browser's cache. These are the chains to ask for by hash.
 */
export function unresolvedHeadsOf(
  records: readonly TrajectoryDetailRecord[],
): string[] {
  const heads = new Set<string>()
  for (const record of records) {
    for (const key of record.incomplete_sequences ?? []) {
      const reference = record.sequences?.[key]
      if (reference?.hash) heads.add(reference.hash)
    }
  }
  return [...heads]
}

/** Chain heads one page of records refers to. */
export function sequenceHeadsOf(
  records: readonly TrajectoryDetailRecord[],
): string[] {
  const heads = new Set<string>()
  for (const record of records) {
    for (const reference of Object.values(record.sequences ?? {})) {
      if (reference?.hash) heads.add(reference.hash)
    }
  }
  return [...heads]
}
