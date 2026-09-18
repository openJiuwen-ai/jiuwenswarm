// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Standalone trajectory read model.
 *
 * Adapted from `packages/client/ui-trajectory/src/client/layout.ts` under the
 * repository MIT license. This file deliberately contains no DSH runtime or
 * React types; OTel projectors publish this closed model to the UI.
 */

import type { TrajectoryCell } from './record.ts'

/** Provider request settings displayed by the request inspector. */
export interface TrajectoryRequestConfig {
  provider: string
  model: string
  purpose?: string
  thinking?: string
  reasoningEffort?: string
  temperature?: number
  topP?: number
  maxTokens?: number
  stop?: readonly string[]
  stream?: boolean
}

/** Correlation, response, cost, and trace facts shown outside request options. */
export interface TrajectoryRecordedFacts {
  correlation?: {
    sessionId?: string
    requestId?: string
    runId?: string
    turnId?: string
  }
  agent?: {
    id?: string
    name?: string
    version?: string
    description?: string
    mode?: string
  }
  response?: {
    id?: string
    model?: string
    finishReasons?: readonly string[]
    totalLatencyMs?: number
    timePerOutputTokenMs?: number
    promptTokenIds?: unknown
    completionTokenIds?: unknown
    logprobs?: unknown
    parserResult?: unknown
    providerMetadata?: unknown
  }
  cost?: {
    input?: number
    output?: number
    total?: number
  }
  trace?: {
    root?: boolean
    schemaVersion?: string
    complete?: boolean
    forcedClose?: boolean
  }
}

/** One tool definition in the effective model-visible tool catalog. */
export interface TrajectoryToolSchema {
  name: string
  description: string
  parameters: object | unknown[]
}

/** One real system message slot in the model request, preserving boundaries. */
export interface TrajectorySystemMessage {
  index: number
  content: string
}

/** Effective prompt state displayed by SYSTEM records. */
export interface TrajectoryPromptSnapshot {
  config: TrajectoryRequestConfig
  system: string
  systemMessages: readonly TrajectorySystemMessage[]
  tools: readonly TrajectoryToolSchema[]
}

/** One Message or Step group inside a turn. */
export interface TrajectoryGroupModel {
  title: string
  description?: string
  cells: readonly TrajectoryCell[]
}

/** One sticky turn, or a standalone compaction section between turns. */
export interface TrajectoryTurnModel {
  turn: number | null
  groups: readonly TrajectoryGroupModel[]
}

/** Token buckets displayed for one request or a loaded-window prefix. */
export interface TrajectoryUsage {
  input?: number
  cacheRead?: number
  cacheWrite?: number
  output?: number
  reasoning?: number
  total?: number
}

/** Request-inspector fields shared by generation and compaction. */
interface TrajectoryRequestBase {
  /** Stable identity of one physical model request, independent from its Step group. */
  recordId?: string
  seq?: number
  group: string
  number: number
  status?: 'complete' | 'running' | 'error'
  startedAt?: number
  completedAt?: number | null
  error?: string
  retry?: number
  maxRetries?: number
  retryDelayMs?: number
  resultSeq?: number
  provider?: string
  model?: string
  requestConfig?: TrajectoryRequestConfig
  recordedFacts?: TrajectoryRecordedFacts
  usage?: TrajectoryUsage
  cumulativeUsage?: TrajectoryUsage
}

/** One purpose-discriminated request paired with its display number. */
export type TrajectoryRequest = TrajectoryRequestBase & (
  | { purpose?: 'assistant'; turn: number; step: number }
  | { purpose: 'compaction'; turn: number; step: number }
)

/** Snapshot consumed atomically by `TrajectoryExplorer`. */
export interface TrajectorySnapshot {
  turns: readonly TrajectoryTurnModel[]
  requests?: readonly TrajectoryRequest[]
  streamingCells?: readonly TrajectoryCell[]
  diagnostics?: readonly TrajectoryDiagnostic[]
}

/** Non-fatal projection issue that preserves the last valid trajectory view. */
export interface TrajectoryDiagnostic {
  code: string
  message: string
  subjectId?: string
  eventId?: string
  sequence?: number
}
