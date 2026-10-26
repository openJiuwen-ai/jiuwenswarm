# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Shared WebSocket payload limits for Gateway and AgentServer links."""

# Gateway ↔ AgentServer internal link (streaming events, no large file base64).
AGENT_WS_MAX_MESSAGE_BYTES = 8 * 2**20
AGENT_WS_SEND_BUDGET_BYTES = 6 * 2**20

# Browser ↔ WebChannel: document.persist may carry large base64 payloads.
# Keep headroom above the document size limit for base64 + JSON overhead.
WEB_WS_MAX_MESSAGE_BYTES = 100 * 2**20
