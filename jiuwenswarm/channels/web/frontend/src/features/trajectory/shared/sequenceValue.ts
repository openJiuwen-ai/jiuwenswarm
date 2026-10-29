/**
 * Attribute values assembled from content shared across records.
 *
 * Storage keeps one copy of each message, tool definition and system
 * instruction, and every model call names the chain it restated. Joining that
 * chain into one string per record held the whole conversation once per call,
 * which on a long Team run outgrew the page's heap. A value built here keeps
 * the chain's elements, which every record restating them shares, and
 * assembles the text only when something reads it.
 */

import type { OtlpAnyValue } from './otlp.ts'

/** The shared content behind one referenced attribute. */
export interface SequenceContent {
  /** The chain's hash: equal hashes state equal content. */
  readonly hash: string
  /** Each element of the chain, a JSON document of its own. */
  readonly elements: readonly string[]
  /** The elements parsed, each once per cache; undefined when one is not JSON. */
  parsedElements(): readonly unknown[] | undefined
}

// Registered, so a copy of this module bundled twice still recognizes it.
const SEQUENCE_CONTENT: unique symbol = Symbol.for('openjiuwen.trajectory.sequenceContent')

type SequenceBackedValue = { readonly stringValue: string, readonly [SEQUENCE_CONTENT]: SequenceContent }

/** The JSON array text a chain rebuilds into: always an array, at any depth. */
export function sequenceText(elements: readonly string[]): string {
  return `[${elements.join(',')}]`
}

/**
 * An OTLP string value whose text is assembled on each read.
 *
 * Nothing holds the assembled text, so it lives only as long as the reader
 * that asked for it. Readers that can use the elements directly go through
 * `sequenceContentOf` instead.
 */
export function sequenceBackedValue(content: SequenceContent): OtlpAnyValue {
  const value = {} as Record<PropertyKey, unknown>
  Object.defineProperty(value, 'stringValue', {
    enumerable: true,
    get: () => sequenceText(content.elements),
  })
  // Hidden from copies and comparisons, which see the value as its text.
  Object.defineProperty(value, SEQUENCE_CONTENT, { value: content })
  return value as unknown as SequenceBackedValue
}

/** The shared content behind a value, or undefined for a plain value. */
export function sequenceContentOf(value: OtlpAnyValue): SequenceContent | undefined {
  return (value as Partial<SequenceBackedValue>)[SEQUENCE_CONTENT]
}
