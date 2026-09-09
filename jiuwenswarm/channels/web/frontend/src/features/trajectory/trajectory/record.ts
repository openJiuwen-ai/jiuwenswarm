// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Standalone trajectory record model and formatters.
 *
 * Adapted from `packages/client/ui-trajectory/src/client/trajectory-record.ts`
 * under the repository MIT license.
 */

import type { TrajectoryPromptSnapshot } from './model.ts'

/** Closed set of record kinds rendered by the trajectory UI. */
export type TrajectoryCellKind =
  | 'system'
  | 'user'
  | 'context'
  | 'compacted'
  | 'message'
  | 'tool'
  | 'subtool'

/** Recorded inputs needed to derive assistant TTFT and decode throughput. */
export interface AssistantMetricDetail {
  timingRecorded: boolean
  /** Whether the provider response exposed token-level streaming timing. */
  streaming?: boolean | null
  stepStartTime: number | null
  firstTokenTime: number | null
  completedTime: number | null
  usageProvided: boolean
  outputTokens: number | null
}

/** One source content block preserved in model order for the inspector. */
export interface TrajectorySourceBlock {
  type: string
  content: string
  imageSrc?: string
  imageAlt?: string
  callId?: string
  toolName?: string
}

/** Data for one projected trajectory record. */
export interface TrajectoryCell {
  index: number
  recordId?: string
  /** Physical model request owning this row; several requests may share one Step. */
  requestRecordId?: string
  /** Schema-v2 physical inference identity, resolved without temporal inference. */
  physicalInferenceId?: string
  kind: TrajectoryCellKind
  /** Explicit lifecycle; payload capture policy never determines completion. */
  status?: 'complete' | 'running' | 'error'
  text: string
  previewMarkdown?: string
  opensTurn?: boolean
  sourceSeq?: number
  /** Stable logical position inside one request when wall-clock timestamps overlap. */
  behaviorOrder?: number
  messageSource?: unknown
  /** Original one-Span OTLP export request for the generic OTel inspector. */
  traceDetail?: unknown
  requestOnly?: boolean
  /** Behavior event that must not create a synthetic Request boundary. */
  requestless?: boolean
  inputDetail?: string
  /** Previous content for a same-slot USER/CONTEXT replacement. */
  previousInputDetail?: string
  promptDetail?: TrajectoryPromptSnapshot
  previousPromptDetail?: TrajectoryPromptSnapshot
  /** Real request-message slot represented by this SYSTEM row. */
  promptSystemMessageIndex?: number
  outputDetail?: string
  /** Structured schema-v2 compaction payload shown independently from Markdown output. */
  compactionDetail?: Readonly<Record<string, unknown>>
  thinkingDetail?: string
  sourceBlocks?: readonly TrajectorySourceBlock[]
  outputBlocks?: readonly TrajectorySourceBlock[]
  schemaDetail?: string
  assistantMetrics?: AssistantMetricDetail
  result?: string
  resultPreviewMarkdown?: string
  callId?: string
  isError?: boolean
  timeSeconds: number | null
  startedAt?: number | null
  input?: number
  cacheRead?: number
  cacheWrite?: number
  output?: number
  think?: number
  total?: number
}

/** Compatibility name retained by the mechanically adapted DSH components. */
export type TrajectoryCellProps = TrajectoryCell

/** Resolve the identity that survives prepending older projected records. */
export function trajectoryRecordId(cell: TrajectoryCell): string {
  if (cell.recordId !== undefined) return cell.recordId
  if (cell.callId !== undefined) return `${cell.kind}\u0000call\u0000${cell.callId}`
  if (cell.sourceSeq !== undefined) return `${cell.kind}\u0000seq\u0000${cell.sourceSeq}`
  return `${cell.kind}\u0000index\u0000${cell.index}`
}

/** Format a duration in milliseconds, or an em dash when unknown. */
export function formatDurationMillis(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds)) return '—'
  const integer = String(Math.round(milliseconds))
  return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')} ms`
}

/** Format a token count with thousands separators, or an em dash when unknown. */
export function formatTokenCount(tokens: number | null | undefined): string {
  if (tokens === null || tokens === undefined || !Number.isFinite(tokens)) return '—'
  const integer = String(Math.round(tokens))
  return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')} tok`
}

/** Format elapsed seconds as an integer-millisecond label. */
export function formatElapsedSeconds(seconds: number | null): string {
  return formatDurationMillis(seconds === null ? null : seconds * 1_000)
}

/** Resolve a running record's open interval without mutating its recorded facts. */
export function liveElapsedSeconds(
  cell: TrajectoryCell,
  nowMilliseconds: number,
): number | null {
  if (cell.timeSeconds !== null) return cell.timeSeconds
  if (cell.status !== 'running'
    || cell.startedAt === null
    || cell.startedAt === undefined
    || !Number.isFinite(cell.startedAt)
    || !Number.isFinite(nowMilliseconds)) return null
  return Math.max(0, nowMilliseconds - cell.startedAt) / 1_000
}
