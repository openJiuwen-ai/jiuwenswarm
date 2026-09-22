// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Version pins and semantic names accepted by the observability profile. */

import { GEN_AI_ATTRIBUTES } from './gen-ai-semconv.generated.ts'
import { OPENJIUWEN_SEMCONV } from './openjiuwen-semconv.generated.ts'

export {
  GEN_AI_ATTRIBUTES,
  GEN_AI_CORE_SEMCONV_SCHEMA_URL,
  GEN_AI_OPERATIONS,
  GEN_AI_SEMCONV_ATTRIBUTE_COUNT,
  GEN_AI_SEMCONV_REVISION,
  GEN_AI_SEMCONV_SCHEMA_URL,
} from './gen-ai-semconv.generated.ts'

export {
  OPENJIUWEN_SEMCONV,
  SEQUENCE_REFERENCE_PREFIX,
  SEQUENCE_REFERENCE_VERSION,
  TRAJECTORY_EVENT_KINDS,
  TRAJECTORY_RECORD_KINDS,
  TRAJECTORY_SPAN_SCHEMA_VERSION,
} from './openjiuwen-semconv.generated.ts'

/** OpenTelemetry specification revision used to define the trace model. */
export const OTEL_SPEC_VERSION = '1.60.0' as const

/** OTLP protocol revision used to define the protobuf JSON mapping. */
export const OTLP_PROTO_VERSION = '1.11.0' as const

/** Stable core semantic-conventions revision used by resource schema URLs. */
export const OTEL_SEMCONV_VERSION = '1.44.0' as const

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
 * a single canonical key per fact. Every value is taken from the constants
 * generated out of Agent Core, so a key the emitter drops or renames fails to
 * compile here instead of silently reading nothing.
 */
export const OPENJIUWEN_ATTRIBUTES = {
  traceRoot: OPENJIUWEN_SEMCONV.OJ_TRACE_ROOT,
  traceComplete: OPENJIUWEN_SEMCONV.OJ_TRACE_COMPLETE,
  traceForcedClose: OPENJIUWEN_SEMCONV.OJ_TRACE_FORCED_CLOSE,
  spanForcedClose: OPENJIUWEN_SEMCONV.OJ_SPAN_FORCED_CLOSE,
  spanForcedCloseReason: OPENJIUWEN_SEMCONV.OJ_SPAN_FORCED_CLOSE_REASON,
  spanInput: OPENJIUWEN_SEMCONV.OJ_SPAN_INPUT,
  requestId: OPENJIUWEN_SEMCONV.OJ_REQUEST_ID,
  runId: OPENJIUWEN_SEMCONV.OJ_RUN_ID,
  turnId: OPENJIUWEN_SEMCONV.OJ_TURN_ID,
  turnNumber: OPENJIUWEN_SEMCONV.OJ_TURN_NUMBER,
  stepId: OPENJIUWEN_SEMCONV.OJ_STEP_ID,
  stepNumber: OPENJIUWEN_SEMCONV.OJ_STEP_NUMBER,
  inferenceId: OPENJIUWEN_SEMCONV.OJ_INFERENCE_ID,
  executionSubjectId: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_ID,
  executionSubjectDisplayName: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_DISPLAY_NAME,
  executionSubjectKind: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_KIND,
  executionSubjectParentId: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_PARENT_ID,
  executionSubjectSessionId: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_SESSION_ID,
  executionSubjectRequestNumber: OPENJIUWEN_SEMCONV.OJ_EXECUTION_SUBJECT_REQUEST_NUMBER,
  trajectoryKind: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_RECORD_KIND,
  requestPurpose: OPENJIUWEN_SEMCONV.OJ_REQUEST_PURPOSE,
  contextOperationId: OPENJIUWEN_SEMCONV.OJ_CONTEXT_OPERATION_ID,
  compactionNumber: OPENJIUWEN_SEMCONV.OJ_COMPACTION_NUMBER,
  requestNumber: OPENJIUWEN_SEMCONV.OJ_REQUEST_NUMBER,
  agentMode: OPENJIUWEN_SEMCONV.OJ_AGENT_MODE,
  inputCost: OPENJIUWEN_SEMCONV.OJ_GEN_AI_USAGE_INPUT_COST,
  outputCost: OPENJIUWEN_SEMCONV.OJ_GEN_AI_USAGE_OUTPUT_COST,
  totalCost: OPENJIUWEN_SEMCONV.OJ_GEN_AI_USAGE_TOTAL_COST,
  totalLatencyMs: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_TOTAL_LATENCY_MS,
  timePerOutputTokenMs: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_TPOT_MS,
  promptTokenIds: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_PROMPT_TOKEN_IDS,
  completionTokenIds: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_COMPLETION_TOKEN_IDS,
  logprobs: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_LOGPROBS,
  parserResult: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_PARSER_RESULT,
  providerMetadata: OPENJIUWEN_SEMCONV.OJ_GEN_AI_RESPONSE_PROVIDER_METADATA,
  inputMessageProvenance: OPENJIUWEN_SEMCONV.OJ_GEN_AI_INPUT_MESSAGE_PROVENANCE,
  toolResourceId: OPENJIUWEN_SEMCONV.OJ_TOOL_RESOURCE_ID,
  toolProtocol: OPENJIUWEN_SEMCONV.OJ_TOOL_PROTOCOL,
  toolAuthoritative: OPENJIUWEN_SEMCONV.OJ_TOOL_AUTHORITATIVE,
  eventSequence: OPENJIUWEN_SEMCONV.OJ_EVENT_SEQUENCE,
  streamKind: OPENJIUWEN_SEMCONV.OJ_STREAM_KIND,
  streamText: OPENJIUWEN_SEMCONV.OJ_STREAM_TEXT,
  streamArgumentsDelta: OPENJIUWEN_SEMCONV.OJ_STREAM_TOOL_CALL_ARGUMENTS_DELTA,
  trajectorySchemaVersion: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_SCHEMA_VERSION,
  trajectoryEventId: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_EVENT_ID,
  trajectoryEventKind: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_EVENT_KIND,
  trajectorySubjectId: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_SUBJECT_ID,
  trajectorySubjectSequence: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_SUBJECT_SEQUENCE,
  trajectorySequenceEpoch: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_SEQUENCE_EPOCH,
  trajectoryRecordedAtUnixNano: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_RECORDED_AT_UNIX_NANO,
  trajectoryPayload: OPENJIUWEN_SEMCONV.OJ_TRAJECTORY_PAYLOAD,
} as const

/** OpenJiuwen span-event names read by the viewer. */
export const OPENJIUWEN_EVENTS = {
  streamChunk: OPENJIUWEN_SEMCONV.OJ_STREAM_FRAME_EVENT,
} as const

/**
 * Stream-frame kinds Agent Core emits (`openjiuwen.stream.kind`). Not an
 * attribute key, so it is stated here rather than generated.
 */
export const STREAM_FRAME_KINDS = [
  'text-delta',
  'reasoning-delta',
  'tool-call-delta',
  'usage',
] as const

/** Request purposes that select the trajectory request inspector. */
export const REQUEST_PURPOSES = ['assistant', 'compaction'] as const
