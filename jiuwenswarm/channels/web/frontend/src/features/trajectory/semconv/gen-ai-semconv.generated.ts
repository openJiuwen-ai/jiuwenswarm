// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Generated OpenTelemetry GenAI semantic-convention definitions.
 *
 * Source: open-telemetry/semantic-conventions-genai
 * Revision: fee465db333bdd6a7d2faa320edab5cf3101a4f4
 *
 * Replace this file as one unit when the pinned registry is upgraded. Product
 * extensions and compatibility aliases belong in constants.ts.
 */

export const GEN_AI_SEMCONV_REVISION = 'fee465db333bdd6a7d2faa320edab5cf3101a4f4' as const
export const GEN_AI_SEMCONV_SCHEMA_URL = 'https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev' as const
export const GEN_AI_CORE_SEMCONV_SCHEMA_URL = 'https://opentelemetry.io/schemas/1.44.0' as const
export const GEN_AI_SEMCONV_ATTRIBUTE_COUNT = 72 as const

export const GEN_AI_ATTRIBUTES = {
  GEN_AI_AGENT_DESCRIPTION: 'gen_ai.agent.description',
  GEN_AI_AGENT_ID: 'gen_ai.agent.id',
  GEN_AI_AGENT_NAME: 'gen_ai.agent.name',
  GEN_AI_AGENT_VERSION: 'gen_ai.agent.version',
  GEN_AI_CONVERSATION_COMPACTED: 'gen_ai.conversation.compacted',
  GEN_AI_CONVERSATION_ID: 'gen_ai.conversation.id',
  GEN_AI_DATA_SOURCE_ID: 'gen_ai.data_source.id',
  GEN_AI_EMBEDDINGS_DIMENSION_COUNT: 'gen_ai.embeddings.dimension.count',
  GEN_AI_EVALUATION_EXPLANATION: 'gen_ai.evaluation.explanation',
  GEN_AI_EVALUATION_NAME: 'gen_ai.evaluation.name',
  GEN_AI_EVALUATION_SCORE_LABEL: 'gen_ai.evaluation.score.label',
  GEN_AI_EVALUATION_SCORE_VALUE: 'gen_ai.evaluation.score.value',
  GEN_AI_INPUT_MESSAGES: 'gen_ai.input.messages',
  GEN_AI_MEMORY_QUERY_TEXT: 'gen_ai.memory.query.text',
  GEN_AI_MEMORY_RECORD_COUNT: 'gen_ai.memory.record.count',
  GEN_AI_MEMORY_RECORD_ID: 'gen_ai.memory.record.id',
  GEN_AI_MEMORY_RECORDS: 'gen_ai.memory.records',
  GEN_AI_MEMORY_STORE_ID: 'gen_ai.memory.store.id',
  GEN_AI_OPERATION_NAME: 'gen_ai.operation.name',
  GEN_AI_OUTPUT_MESSAGES: 'gen_ai.output.messages',
  GEN_AI_OUTPUT_TYPE: 'gen_ai.output.type',
  GEN_AI_PROMPT_NAME: 'gen_ai.prompt.name',
  GEN_AI_PROMPT_VARIABLE: 'gen_ai.prompt.variable',
  GEN_AI_PROMPT_VERSION: 'gen_ai.prompt.version',
  GEN_AI_PROVIDER_NAME: 'gen_ai.provider.name',
  GEN_AI_REQUEST_CHOICE_COUNT: 'gen_ai.request.choice.count',
  GEN_AI_REQUEST_ENCODING_FORMATS: 'gen_ai.request.encoding_formats',
  GEN_AI_REQUEST_FREQUENCY_PENALTY: 'gen_ai.request.frequency_penalty',
  GEN_AI_REQUEST_MAX_TOKENS: 'gen_ai.request.max_tokens',
  GEN_AI_REQUEST_MODEL: 'gen_ai.request.model',
  GEN_AI_REQUEST_PRESENCE_PENALTY: 'gen_ai.request.presence_penalty',
  GEN_AI_REQUEST_PREVIOUS_RESPONSE_ID: 'gen_ai.request.previous_response.id',
  GEN_AI_REQUEST_REASONING_LEVEL: 'gen_ai.request.reasoning.level',
  GEN_AI_REQUEST_SEED: 'gen_ai.request.seed',
  GEN_AI_REQUEST_STOP_SEQUENCES: 'gen_ai.request.stop_sequences',
  GEN_AI_REQUEST_STREAM: 'gen_ai.request.stream',
  GEN_AI_REQUEST_STREAM_CURSOR: 'gen_ai.request.stream_cursor',
  GEN_AI_REQUEST_TEMPERATURE: 'gen_ai.request.temperature',
  GEN_AI_REQUEST_TOP_K: 'gen_ai.request.top_k',
  GEN_AI_REQUEST_TOP_P: 'gen_ai.request.top_p',
  GEN_AI_RESPONSE_FINISH_REASONS: 'gen_ai.response.finish_reasons',
  GEN_AI_RESPONSE_ID: 'gen_ai.response.id',
  GEN_AI_RESPONSE_MODEL: 'gen_ai.response.model',
  GEN_AI_RESPONSE_STATUS: 'gen_ai.response.status',
  GEN_AI_RESPONSE_TIME_TO_FIRST_CHUNK: 'gen_ai.response.time_to_first_chunk',
  GEN_AI_RETRIEVAL_DOCUMENTS: 'gen_ai.retrieval.documents',
  GEN_AI_RETRIEVAL_QUERY_TEXT: 'gen_ai.retrieval.query.text',
  GEN_AI_RETRIEVAL_TOP_K: 'gen_ai.retrieval.top_k',
  GEN_AI_SYSTEM_INSTRUCTIONS: 'gen_ai.system_instructions',
  GEN_AI_TOKEN_TYPE: 'gen_ai.token.type',
  GEN_AI_TOOL_CALL_ARGUMENTS: 'gen_ai.tool.call.arguments',
  GEN_AI_TOOL_CALL_ID: 'gen_ai.tool.call.id',
  GEN_AI_TOOL_CALL_RESULT: 'gen_ai.tool.call.result',
  GEN_AI_TOOL_DEFINITIONS: 'gen_ai.tool.definitions',
  GEN_AI_TOOL_DESCRIPTION: 'gen_ai.tool.description',
  GEN_AI_TOOL_NAME: 'gen_ai.tool.name',
  GEN_AI_TOOL_TYPE: 'gen_ai.tool.type',
  GEN_AI_USAGE_AUDIO_CACHE_READ_INPUT_TOKENS: 'gen_ai.usage.audio.cache_read.input_tokens',
  GEN_AI_USAGE_AUDIO_INPUT_TOKENS: 'gen_ai.usage.audio.input_tokens',
  GEN_AI_USAGE_AUDIO_OUTPUT_TOKENS: 'gen_ai.usage.audio.output_tokens',
  GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: 'gen_ai.usage.cache_read.input_tokens',
  GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS: 'gen_ai.usage.cache_write.input_tokens',
  GEN_AI_USAGE_IMAGE_CACHE_READ_INPUT_TOKENS: 'gen_ai.usage.image.cache_read.input_tokens',
  GEN_AI_USAGE_IMAGE_INPUT_TOKENS: 'gen_ai.usage.image.input_tokens',
  GEN_AI_USAGE_IMAGE_OUTPUT_TOKENS: 'gen_ai.usage.image.output_tokens',
  GEN_AI_USAGE_INPUT_TOKENS: 'gen_ai.usage.input_tokens',
  GEN_AI_USAGE_OUTPUT_TOKENS: 'gen_ai.usage.output_tokens',
  GEN_AI_USAGE_REASONING_OUTPUT_TOKENS: 'gen_ai.usage.reasoning.output_tokens',
  GEN_AI_USAGE_TEXT_CACHE_READ_INPUT_TOKENS: 'gen_ai.usage.text.cache_read.input_tokens',
  GEN_AI_USAGE_TEXT_INPUT_TOKENS: 'gen_ai.usage.text.input_tokens',
  GEN_AI_USAGE_TEXT_OUTPUT_TOKENS: 'gen_ai.usage.text.output_tokens',
  GEN_AI_WORKFLOW_NAME: 'gen_ai.workflow.name',
} as const

export const GEN_AI_OPERATIONS = {
  chat: 'chat',
  generateContent: 'generate_content',
  textCompletion: 'text_completion',
  embeddings: 'embeddings',
  retrieval: 'retrieval',
  fetchResponse: 'fetch_response',
  createAgent: 'create_agent',
  invokeAgent: 'invoke_agent',
  executeTool: 'execute_tool',
  invokeWorkflow: 'invoke_workflow',
  plan: 'plan',
  searchMemory: 'search_memory',
  createMemory: 'create_memory',
  updateMemory: 'update_memory',
  upsertMemory: 'upsert_memory',
  deleteMemory: 'delete_memory',
  createMemoryStore: 'create_memory_store',
  deleteMemoryStore: 'delete_memory_store',
} as const
