"""OTel GenAI Semantic Convention constants (D286).

Provides standard attribute keys for instrumenting LLM and agent operations
following the OpenTelemetry GenAI semantic conventions.

Reference:
    https://opentelemetry.io/docs/specs/semconv/gen-ai/
    https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/

"""

# --- GenAI Semantic Conventions ---
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"

# Finish reason (singular)
GEN_AI_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reason"

# Request parameters
GEN_AI_REQUEST_TOP_P = "gen_ai.request.top_p"

# Span type
GEN_AI_SPAN_TYPE = "gen_ai.span.type"

# Agent / conversation
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"

# --- JiuWenClaw custom attributes ---
JIUWENCLAW_CLAW_ID = "jiuwenclaw.claw.id"
JIUWENCLAW_CHANNEL_ID = "jiuwenclaw.channel.id"
JIUWENCLAW_SESSION_ID = "jiuwenclaw.session.id"
JIUWENCLAW_USER_ID = "jiuwenclaw.user.id"
JIUWENCLAW_REQUEST_ID = "jiuwenclaw.request.id"
JIUWENCLAW_AGENT_NAME = "jiuwenclaw.agent.name"

# Session state tracking
JIUWENCLAW_SESSION_STATE = "jiuwenclaw.session.state"
JIUWENCLAW_SESSION_STATE_REASON = "jiuwenclaw.session.state.reason"

# Token usage
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_tokens"
GEN_AI_USAGE_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation_tokens"

# Tool
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"

# --- Streaming LLM ---
GEN_AI_REQUEST_STREAMING = "gen_ai.request.streaming"
GEN_AI_USAGE_ESTIMATED = "gen_ai.usage.estimated"

# --- React loop / nesting ---
JIUWENCLAW_ITERATION = "jiuwenclaw.iteration"
JIUWENCLAW_AGENT_PARENT = "jiuwenclaw.agent.parent"

# --- Cancel / timeout ---
JIUWENCLAW_CANCELED = "jiuwenclaw.canceled"
ERROR_TYPE = "error.type"
