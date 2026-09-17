// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

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
import type { OtlpExportTraceServiceRequest } from './shared/otlp'

const SEQUENCE_REFERENCE_PREFIX = '@oj-seq'
const SEQUENCE_REFERENCE_VERSION = '1'

/** Content held by hash, plus the chains seen so far. */
export interface SequenceCache {
  blobs: Map<string, string>
  chains: Map<string, readonly string[]>
}

export function createSequenceCache(): SequenceCache {
  return { blobs: new Map(), chains: new Map() }
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
  const elements = cache.chains.get(hash)
  if (elements === undefined) return undefined
  const parts: string[] = []
  for (const element of elements) {
    const content = cache.blobs.get(element)
    if (content === undefined) return undefined
    parts.push(content)
  }
  return `[${parts.join(',')}]`
}

function rebuildSpanAttributes(
  span: Record<string, unknown>,
  cache: SequenceCache,
  unresolved: Set<string>,
): boolean {
  const attributes = span.attributes
  if (!Array.isArray(attributes)) return false
  let changed = false
  for (const attribute of attributes) {
    if (typeof attribute !== 'object' || attribute === null) continue
    const entry = attribute as { key?: unknown; value?: unknown }
    const value = entry.value
    if (typeof value !== 'object' || value === null) continue
    const holder = value as { stringValue?: unknown }
    const reference = parseSequenceReference(holder.stringValue)
    if (reference === null) continue
    const rebuilt = rebuildSequenceValue(cache, reference.hash)
    if (rebuilt === undefined) {
      // Leave the reference in place and say which attribute could not be
      // rebuilt. A missing element must not cost the reader the whole span.
      unresolved.add(String(entry.key ?? ''))
      continue
    }
    holder.stringValue = rebuilt
    changed = true
  }
  return changed
}

/**
 * Rebuild one record's OTLP into the shape the projection expects.
 *
 * The record is returned by reference when it states nothing to rebuild, so
 * the projection cache keeps recognizing it as unchanged.
 */
export function rebuildRecord(
  record: TrajectoryDetailRecord,
  cache: SequenceCache,
): TrajectoryDetailRecord {
  if (record.otlp === null || record.sequences === undefined) return record
  const clone = JSON.parse(JSON.stringify(record.otlp)) as OtlpExportTraceServiceRequest
  const unresolved = new Set<string>()
  let changed = false
  for (const resource of (clone.resourceSpans ?? []) as Record<string, unknown>[]) {
    for (const scope of (resource.scopeSpans ?? []) as Record<string, unknown>[]) {
      for (const span of (scope.spans ?? []) as Record<string, unknown>[]) {
        if (rebuildSpanAttributes(span, cache, unresolved)) changed = true
      }
    }
  }
  if (!changed && unresolved.size === 0) return record
  return {
    ...record,
    otlp: clone,
    ...(unresolved.size === 0 ? {} : { incomplete_sequences: [...unresolved] }),
  } as TrajectoryDetailRecord
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
