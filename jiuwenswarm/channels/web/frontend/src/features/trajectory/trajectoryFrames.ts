import type {
  OtlpExportTraceServiceRequest,
  OtlpSpan,
  OtlpSpanEvent,
} from './shared/otlp'
import type { TrajectoryStreamFrame } from './trajectoryClient'

/**
 * What one still-running span has produced so far, rebuilt from its frames.
 *
 * A span's own record states its output only once it ends. Until then this is
 * the whole of what a reader can show, so it is assembled from the increments
 * as they land rather than waiting for the answer to finish.
 */
export interface StreamingSpanText {
  text: string
  reasoning: string
  toolArguments: ReadonlyMap<string, string>
  lastSequence: number
  frameCount: number
}

/** Frames accumulated per span, plus how far along the session the reader is. */
export interface StreamFrameState {
  bySpan: ReadonlyMap<string, StreamingSpanText>
  frameSeq: number
}

export const emptyStreamFrameState: StreamFrameState = {
  bySpan: new Map(),
  frameSeq: 0,
}

function spanKey(frame: TrajectoryStreamFrame): string {
  return `${frame.trace_id}:${frame.span_id}`
}

function blank(): StreamingSpanText {
  return {
    text: '',
    reasoning: '',
    toolArguments: new Map(),
    lastSequence: -1,
    frameCount: 0,
  }
}

function withFrame(
  current: StreamingSpanText,
  frame: TrajectoryStreamFrame,
): StreamingSpanText {
  // Frames of one span arrive in emission order, so an out-of-order or
  // repeated sequence is a duplicate delivery rather than new content.
  // Appending it would double a word in the middle of the answer.
  if (frame.sequence <= current.lastSequence) return current
  const next: StreamingSpanText = {
    text: current.text,
    reasoning: current.reasoning,
    toolArguments: current.toolArguments,
    lastSequence: frame.sequence,
    frameCount: current.frameCount + 1,
  }
  if (frame.kind === 'text-delta' && frame.text !== undefined) {
    next.text = current.text + frame.text
  } else if (frame.kind === 'reasoning-delta' && frame.text !== undefined) {
    next.reasoning = current.reasoning + frame.text
  } else if (frame.kind === 'tool-call-delta' && frame.arguments_delta !== undefined) {
    const callId = frame.tool_call_id ?? ''
    const merged = new Map(current.toolArguments)
    merged.set(callId, (merged.get(callId) ?? '') + frame.arguments_delta)
    next.toolArguments = merged
  }
  return next
}

/**
 * Fold one page of frames onto what the reader already holds.
 *
 * `reset` means the store no longer contains the frames this reader resumed
 * from -- the database was rebuilt, or retention removed them -- so what it
 * holds describes an answer that is no longer there and has to be dropped
 * rather than extended.
 */
export function applyStreamFrames(
  state: StreamFrameState,
  page: {
    frames: readonly TrajectoryStreamFrame[]
    frame_seq: number
    next_since_frame_seq: number
    reset: boolean
  },
): StreamFrameState {
  const bySpan = page.reset
    ? new Map<string, StreamingSpanText>()
    : new Map(state.bySpan)
  for (const frame of page.frames) {
    const key = spanKey(frame)
    bySpan.set(key, withFrame(bySpan.get(key) ?? blank(), frame))
  }
  const advanced = Math.max(
    page.reset ? 0 : state.frameSeq,
    page.next_since_frame_seq,
  )
  return { bySpan, frameSeq: advanced }
}

/**
 * Forget one span's frames once its record carries the complete output.
 *
 * The frames were only ever a stand-in for an answer still being written.
 * Keeping them after the span ends would let a partial copy compete with the
 * authoritative one.
 */
export function forgetStreamFrames(
  state: StreamFrameState,
  finishedSpanKeys: Iterable<string>,
): StreamFrameState {
  const bySpan = new Map(state.bySpan)
  let removed = false
  for (const key of finishedSpanKeys) {
    removed = bySpan.delete(key) || removed
  }
  return removed ? { bySpan, frameSeq: state.frameSeq } : state
}

/** Read what one span has streamed so far, if anything. */
export function streamingTextFor(
  state: StreamFrameState,
  traceId: string,
  spanId: string,
): StreamingSpanText | undefined {
  return state.bySpan.get(`${traceId}:${spanId}`)
}

const STREAM_CHUNK_EVENT = 'openjiuwen.stream.chunk'
const EVENT_SEQUENCE_KEY = 'openjiuwen.event.sequence'
const STREAM_KIND_KEY = 'openjiuwen.stream.kind'
const STREAM_TEXT_KEY = 'openjiuwen.stream.text'
const TOOL_ARGUMENTS_KEY = 'openjiuwen.stream.tool_call.arguments_delta'
const TOOL_CALL_ID_KEY = 'gen_ai.tool.call.id'

function chunkEvent(
  sequence: number,
  kind: string,
  textKey: string,
  text: string,
  toolCallId?: string,
): OtlpSpanEvent {
  const attributes = [
    { key: EVENT_SEQUENCE_KEY, value: { intValue: String(sequence) } },
    { key: STREAM_KIND_KEY, value: { stringValue: kind } },
    { key: textKey, value: { stringValue: text } },
  ]
  if (toolCallId !== undefined && toolCallId !== '') {
    attributes.push({ key: TOOL_CALL_ID_KEY, value: { stringValue: toolCallId } })
  }
  return { timeUnixNano: '0', name: STREAM_CHUNK_EVENT, attributes }
}

/**
 * Express what a span has streamed so far as the chunk events it once carried.
 *
 * Frames were moved off the span because its event list is a bounded ring that
 * drops the oldest entries. Nothing bounds them here, so one synthetic event
 * per kind restates the accumulated content and the existing projection reads
 * a running answer exactly as it always has -- and stops using it the moment
 * the span's own record states its complete output.
 */
function streamEventsFor(streamed: StreamingSpanText): OtlpSpanEvent[] {
  const events: OtlpSpanEvent[] = []
  if (streamed.reasoning !== '') {
    events.push(chunkEvent(0, 'reasoning-delta', STREAM_TEXT_KEY, streamed.reasoning))
  }
  if (streamed.text !== '') {
    events.push(chunkEvent(1, 'text-delta', STREAM_TEXT_KEY, streamed.text))
  }
  let sequence = 2
  for (const [callId, args] of streamed.toolArguments) {
    if (args === '') continue
    events.push(
      chunkEvent(sequence, 'tool-call-delta', TOOL_ARGUMENTS_KEY, args, callId),
    )
    sequence += 1
  }
  return events
}

function spanWithFrames(span: OtlpSpan, state: StreamFrameState): OtlpSpan {
  // A span that already ended states its own output; its frames are spent.
  if (span.endTimeUnixNano !== undefined && span.endTimeUnixNano !== '0') return span
  const streamed = state.bySpan.get(`${span.traceId}:${span.spanId}`)
  if (streamed === undefined) return span
  const events = streamEventsFor(streamed)
  if (events.length === 0) return span
  return { ...span, events: [...(span.events ?? []), ...events] }
}

/**
 * Attach accumulated frames to the still-running spans of a record set.
 *
 * Records without frames are returned by reference so the projection cache
 * keeps recognizing them as unchanged.
 */
export function withStreamFrames(
  records: readonly OtlpExportTraceServiceRequest[],
  state: StreamFrameState,
): readonly OtlpExportTraceServiceRequest[] {
  if (state.bySpan.size === 0) return records
  let changed = false
  const next = records.map((record) => {
    let recordChanged = false
    const resourceSpans = (record.resourceSpans ?? []).map((resource) => {
      const scopeSpans = (resource.scopeSpans ?? []).map((scope) => {
        const spans = (scope.spans ?? []).map((span) => {
          const updated = spanWithFrames(span, state)
          if (updated !== span) recordChanged = true
          return updated
        })
        return recordChanged ? { ...scope, spans } : scope
      })
      return recordChanged ? { ...resource, scopeSpans } : resource
    })
    if (!recordChanged) return record
    changed = true
    return { ...record, resourceSpans }
  })
  return changed ? next : records
}
