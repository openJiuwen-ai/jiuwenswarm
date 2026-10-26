// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Version pins and semantic names accepted by the observability profile. */

/** OpenTelemetry specification revision used to define the trace model. */
export const OTEL_SPEC_VERSION = '1.60.0' as const

/** OTLP protocol revision used to define the protobuf JSON mapping. */
export const OTLP_PROTO_VERSION = '1.11.0' as const

/** Stable core semantic-conventions revision used by resource schema URLs. */
export const OTEL_SEMCONV_VERSION = '1.44.0' as const

/** Exact development revision of the standalone GenAI semantic conventions. */
export const GEN_AI_SEMCONV_REVISION = 'a685613a207a580163353b8e48a7ad88967e7b42' as const

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
  operationName: 'gen_ai.operation.name',
  providerName: 'gen_ai.provider.name',
  conversationId: 'gen_ai.conversation.id',
  conversationCompacted: 'gen_ai.conversation.compacted',
  requestModel: 'gen_ai.request.model',
  requestMaxTokens: 'gen_ai.request.max_tokens',
  requestTemperature: 'gen_ai.request.temperature',
  requestTopP: 'gen_ai.request.top_p',
  requestStopSequences: 'gen_ai.request.stop_sequences',
  requestStream: 'gen_ai.request.stream',
  requestReasoningLevel: 'gen_ai.request.reasoning.level',
  responseId: 'gen_ai.response.id',
  responseModel: 'gen_ai.response.model',
  responseFinishReasons: 'gen_ai.response.finish_reasons',
  responseTimeToFirstChunk: 'gen_ai.response.time_to_first_chunk',
  usageInputTokens: 'gen_ai.usage.input_tokens',
  usageOutputTokens: 'gen_ai.usage.output_tokens',
  usageReasoningTokens: 'gen_ai.usage.reasoning.output_tokens',
  usageCacheReadTokens: 'gen_ai.usage.cache_read.input_tokens',
  usageCacheCreationTokens: 'gen_ai.usage.cache_creation.input_tokens',
  agentId: 'gen_ai.agent.id',
  agentName: 'gen_ai.agent.name',
  agentVersion: 'gen_ai.agent.version',
  agentDescription: 'gen_ai.agent.description',
  toolName: 'gen_ai.tool.name',
  toolId: 'gen_ai.tool.id',
  toolCallId: 'gen_ai.tool.call.id',
  toolType: 'gen_ai.tool.type',
  toolDescription: 'gen_ai.tool.description',
  toolCallArguments: 'gen_ai.tool.call.arguments',
  toolCallResult: 'gen_ai.tool.call.result',
  systemInstructions: 'gen_ai.system_instructions',
  inputMessages: 'gen_ai.input.messages',
  outputMessages: 'gen_ai.output.messages',
  toolDefinitions: 'gen_ai.tool.definitions',
} as const

/** OpenJiuwen trajectory extensions and compatibility attributes. */
export const OPENJIUWEN_ATTRIBUTES = {
  traceRoot: 'openjiuwen.trace.root',
  traceSchemaVersion: 'openjiuwen.trace.schema_version',
  traceComplete: 'openjiuwen.trace.complete',
  traceForcedClose: 'openjiuwen.trace.forced_close',
  spanForcedClose: 'openjiuwen.span.forced_close',
  spanForcedCloseReason: 'openjiuwen.span.forced_close.reason',
  sessionId: 'openjiuwen.session.id',
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
  teamId: 'openjiuwen.team.id',
  teamName: 'openjiuwen.team.name',
  teamLeader: 'openjiuwen.team.leader',
  teamMemberId: 'openjiuwen.team.member.id',
  teamMemberName: 'openjiuwen.team.member.name',
  trajectoryKind: 'openjiuwen.trajectory.record.kind',
  requestPurpose: 'openjiuwen.request.purpose',
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
  toolType: 'openjiuwen.tool.type',
  toolAuthoritative: 'openjiuwen.tool.authoritative',
  eventSequence: 'openjiuwen.event.sequence',
  streamKind: 'openjiuwen.stream.kind',
  streamText: 'openjiuwen.stream.text',
  streamToolCallId: 'openjiuwen.stream.tool_call.id',
  streamToolName: 'openjiuwen.stream.tool_call.name',
  streamArgumentsDelta: 'openjiuwen.stream.tool_call.arguments_delta',
  trajectorySchemaVersion: 'openjiuwen.trajectory.schema_version',
  trajectoryEventId: 'openjiuwen.trajectory.event_id',
  trajectoryEventKind: 'openjiuwen.trajectory.event_kind',
  trajectorySubjectId: 'openjiuwen.trajectory.subject_id',
  trajectorySubjectSequence: 'openjiuwen.trajectory.subject_sequence',
  trajectorySequenceEpoch: 'openjiuwen.trajectory.sequence_epoch',
  trajectorySessionId: 'openjiuwen.trajectory.session_id',
  trajectoryTurnId: 'openjiuwen.trajectory.turn_id',
  trajectoryStepId: 'openjiuwen.trajectory.step_id',
  trajectoryRequestId: 'openjiuwen.trajectory.request_id',
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

/** Well-known GenAI operation names used by the projector. */
export const GEN_AI_OPERATIONS = {
  chat: 'chat',
  generateContent: 'generate_content',
  textCompletion: 'text_completion',
  invokeAgent: 'invoke_agent',
  executeTool: 'execute_tool',
  invokeWorkflow: 'invoke_workflow',
  plan: 'plan',
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
