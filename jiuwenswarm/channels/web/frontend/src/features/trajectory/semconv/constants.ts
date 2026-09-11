// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Version pins and semantic names accepted by the observability profile. */

import { GEN_AI_ATTRIBUTES } from './gen-ai-semconv.generated.ts'

export {
  GEN_AI_ATTRIBUTES,
  GEN_AI_CORE_SEMCONV_SCHEMA_URL,
  GEN_AI_OPERATIONS,
  GEN_AI_SEMCONV_ATTRIBUTE_COUNT,
  GEN_AI_SEMCONV_REVISION,
  GEN_AI_SEMCONV_SCHEMA_URL,
} from './gen-ai-semconv.generated.ts'

/** OpenTelemetry specification revision used to define the trace model. */
export const OTEL_SPEC_VERSION = '1.60.0' as const

/** OTLP protocol revision used to define the protobuf JSON mapping. */
export const OTLP_PROTO_VERSION = '1.11.0' as const

/** Stable core semantic-conventions revision used by resource schema URLs. */
export const OTEL_SEMCONV_VERSION = '1.44.0' as const

/** Current pre-release DSH extension schema accepted by the viewer. */
export const DSH_SCHEMA_VERSION = '1' as const

/** Standard OTel and GenAI attribute keys used by the profile. */
export const STANDARD_ATTRIBUTES = {
  serviceName: 'service.name',
  sessionId: 'session.id',
  errorType: 'error.type',
  exceptionType: 'exception.type',
  exceptionMessage: 'exception.message',
  exceptionStacktrace: 'exception.stacktrace',
  operationName: GEN_AI_ATTRIBUTES.GEN_AI_OPERATION_NAME,
  providerName: GEN_AI_ATTRIBUTES.GEN_AI_PROVIDER_NAME,
  conversationId: GEN_AI_ATTRIBUTES.GEN_AI_CONVERSATION_ID,
  conversationCompacted: GEN_AI_ATTRIBUTES.GEN_AI_CONVERSATION_COMPACTED,
  requestModel: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_MODEL,
  requestMaxTokens: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_MAX_TOKENS,
  requestTemperature: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_TEMPERATURE,
  requestTopP: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_TOP_P,
  requestStopSequences: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_STOP_SEQUENCES,
  requestStream: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_STREAM,
  requestReasoningLevel: GEN_AI_ATTRIBUTES.GEN_AI_REQUEST_REASONING_LEVEL,
  responseId: GEN_AI_ATTRIBUTES.GEN_AI_RESPONSE_ID,
  responseModel: GEN_AI_ATTRIBUTES.GEN_AI_RESPONSE_MODEL,
  responseFinishReasons: GEN_AI_ATTRIBUTES.GEN_AI_RESPONSE_FINISH_REASONS,
  responseTimeToFirstChunk: GEN_AI_ATTRIBUTES.GEN_AI_RESPONSE_TIME_TO_FIRST_CHUNK,
  usageInputTokens: GEN_AI_ATTRIBUTES.GEN_AI_USAGE_INPUT_TOKENS,
  usageOutputTokens: GEN_AI_ATTRIBUTES.GEN_AI_USAGE_OUTPUT_TOKENS,
  usageReasoningTokens: GEN_AI_ATTRIBUTES.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
  usageCacheReadTokens: GEN_AI_ATTRIBUTES.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
  usageCacheWriteTokens: GEN_AI_ATTRIBUTES.GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS,
  agentId: GEN_AI_ATTRIBUTES.GEN_AI_AGENT_ID,
  agentName: GEN_AI_ATTRIBUTES.GEN_AI_AGENT_NAME,
  agentVersion: GEN_AI_ATTRIBUTES.GEN_AI_AGENT_VERSION,
  agentDescription: GEN_AI_ATTRIBUTES.GEN_AI_AGENT_DESCRIPTION,
  toolName: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_NAME,
  toolCallId: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_CALL_ID,
  toolType: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_TYPE,
  toolDescription: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_DESCRIPTION,
  toolCallArguments: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_CALL_ARGUMENTS,
  toolCallResult: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_CALL_RESULT,
  systemInstructions: GEN_AI_ATTRIBUTES.GEN_AI_SYSTEM_INSTRUCTIONS,
  inputMessages: GEN_AI_ATTRIBUTES.GEN_AI_INPUT_MESSAGES,
  outputMessages: GEN_AI_ATTRIBUTES.GEN_AI_OUTPUT_MESSAGES,
  toolDefinitions: GEN_AI_ATTRIBUTES.GEN_AI_TOOL_DEFINITIONS,
} as const

/**
 * OpenJiuwen trajectory extensions.
 *
 * Only facts the GenAI standard does not model live here. Session, agent and
 * tool-call identity are read from `STANDARD_ATTRIBUTES`; the emitter writes
 * a single canonical key per fact.
 */
export const OPENJIUWEN_ATTRIBUTES = {
  traceRoot: 'openjiuwen.trace.root',
  traceSchemaVersion: 'openjiuwen.trace.schema_version',
  traceComplete: 'openjiuwen.trace.complete',
  traceForcedClose: 'openjiuwen.trace.forced_close',
  spanForcedClose: 'openjiuwen.span.forced_close',
  spanForcedCloseReason: 'openjiuwen.span.forced_close.reason',
  requestId: 'openjiuwen.request.id',
  runId: 'openjiuwen.run.id',
  turnId: 'openjiuwen.turn.id',
  turnNumber: 'openjiuwen.turn.number',
  stepId: 'openjiuwen.step.id',
  stepNumber: 'openjiuwen.step.number',
  inferenceId: 'openjiuwen.inference.id',
  executionSubjectId: 'openjiuwen.execution.subject.id',
  executionSubjectDisplayName: 'openjiuwen.execution.subject.display_name',
  executionSubjectKind: 'openjiuwen.execution.subject.kind',
  executionSubjectParentId: 'openjiuwen.execution.subject.parent_id',
  executionSubjectSessionId: 'openjiuwen.execution.subject.session_id',
  executionSubjectRequestNumber: 'openjiuwen.execution.subject.request.number',
  trajectoryKind: 'openjiuwen.trajectory.record.kind',
  requestPurpose: 'openjiuwen.request.purpose',
  contextOperationId: 'openjiuwen.context.operation.id',
  compactionNumber: 'openjiuwen.compaction.number',
  requestNumber: 'openjiuwen.request.number',
  requestRetryCount: 'openjiuwen.request.retry_count',
  requestMaxRetries: 'openjiuwen.request.max_retries',
  agentMode: 'openjiuwen.agent.mode',
  inputCost: 'openjiuwen.gen_ai.usage.input_cost',
  outputCost: 'openjiuwen.gen_ai.usage.output_cost',
  totalCost: 'openjiuwen.gen_ai.usage.total_cost',
  totalLatencyMs: 'openjiuwen.gen_ai.response.total_latency_ms',
  timePerOutputTokenMs: 'openjiuwen.gen_ai.response.tpot_ms',
  promptTokenIds: 'openjiuwen.gen_ai.response.prompt_token_ids',
  completionTokenIds: 'openjiuwen.gen_ai.response.completion_token_ids',
  logprobs: 'openjiuwen.gen_ai.response.logprobs',
  parserResult: 'openjiuwen.gen_ai.response.parser_result',
  providerMetadata: 'openjiuwen.gen_ai.response.provider_metadata',
  inputMessageProvenance: 'openjiuwen.gen_ai.input.message_provenance',
  toolResourceId: 'openjiuwen.tool.resource_id',
  toolProtocol: 'openjiuwen.tool.protocol',
  toolAuthoritative: 'openjiuwen.tool.authoritative',
  eventSequence: 'openjiuwen.event.sequence',
  streamKind: 'openjiuwen.stream.kind',
  streamText: 'openjiuwen.stream.text',
  streamArgumentsDelta: 'openjiuwen.stream.tool_call.arguments_delta',
  trajectorySchemaVersion: 'openjiuwen.trajectory.schema_version',
  trajectoryEventId: 'openjiuwen.trajectory.event_id',
  trajectoryEventKind: 'openjiuwen.trajectory.event_kind',
  trajectorySubjectId: 'openjiuwen.trajectory.subject_id',
  trajectorySubjectSequence: 'openjiuwen.trajectory.subject_sequence',
  trajectorySequenceEpoch: 'openjiuwen.trajectory.sequence_epoch',
  trajectoryRecordedAtUnixNano: 'openjiuwen.trajectory.recorded_at_unix_nano',
  trajectoryPayload: 'openjiuwen.trajectory.payload',
} as const

/** OpenJiuwen event names added alongside existing DSH and llm.chunk events. */
export const OPENJIUWEN_EVENTS = {
  streamChunk: 'openjiuwen.stream.chunk',
  retryScheduled: 'openjiuwen.retry.scheduled',
  retryStarted: 'openjiuwen.retry.started',
  legacyStreamChunk: 'llm.chunk',
} as const

/** DSH-specific attribute keys whose meaning is defined by the data-format reference. */
export const DSH_ATTRIBUTES = {
  schemaVersion: 'dsh.schema.version',
  genAiSemconvRevision: 'dsh.semconv.gen_ai.revision',
  sessionParentId: 'dsh.session.parent_id',
  sessionSourceSequence: 'dsh.session.source_sequence',
  turnNumber: 'dsh.turn.number',
  turnEndReason: 'dsh.turn.end_reason',
  stepNumber: 'dsh.step.number',
  trajectoryKind: 'dsh.trajectory.record.kind',
  requestPurpose: 'dsh.request.purpose',
  requestNumber: 'dsh.request.number',
  requestRetryCount: 'dsh.request.retry_count',
  requestMaxRetries: 'dsh.request.max_retries',
  messageSourceKind: 'dsh.message.source.kind',
  messageSourcePlugin: 'dsh.message.source.plugin',
  eventSequence: 'dsh.event.sequence',
  retryAttempt: 'dsh.retry.attempt',
  retryMaximumAttempts: 'dsh.retry.maximum_attempts',
  retryDelayMilliseconds: 'dsh.retry.delay_ms',
  streamSequence: 'dsh.stream.sequence',
  streamKind: 'dsh.stream.kind',
  streamBlockIndex: 'dsh.stream.block.index',
  streamText: 'dsh.stream.text',
  streamArgumentsDelta: 'dsh.stream.arguments_delta',
  streamToolCallId: 'dsh.stream.tool_call.id',
  streamToolName: 'dsh.stream.tool.name',
  compactionId: 'dsh.compaction.id',
  compactionShadowedSequenceStart: 'dsh.compaction.shadowed_sequence.start',
  compactionShadowedSequenceEnd: 'dsh.compaction.shadowed_sequence.end',
  compactionInputTokens: 'dsh.compaction.input_tokens',
  compactionSourceCommand: 'dsh.compaction.source_command',
  compactionSummary: 'dsh.compaction.summary',
} as const

/** DSH-specific span-event names used for replay and lifecycle detail. */
export const DSH_EVENTS = {
  streamChunk: 'dsh.stream.chunk',
  retryScheduled: 'dsh.retry.scheduled',
  retryStarted: 'dsh.retry.started',
} as const

/** Closed DSH record-kind hints understood by the trajectory projector. */
export const DSH_TRAJECTORY_KINDS = [
  'turn',
  'step',
  'inference',
  'reasoning',
  'tool',
  'compaction',
] as const

/** Replayable DSH stream-chunk discriminants. */
export const DSH_STREAM_KINDS = [
  'block-start',
  'text-delta',
  'reasoning-delta',
  'tool-call-delta',
  'block-end',
  'usage',
] as const

/** DSH turn outcomes rendered independently from OTel status. */
export const DSH_TURN_END_REASONS = [
  'completed',
  'max-tokens',
  'error',
  'aborted',
  'blocked',
  'interrupted',
] as const

/** Request purposes that select the trajectory request inspector. */
export const DSH_REQUEST_PURPOSES = ['assistant', 'compaction'] as const
