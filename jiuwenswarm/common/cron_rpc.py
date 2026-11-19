"""Cron agent→gateway Reverse RPC 共享契约（method 名与超时常量）。

run_now 回执关联（2026-11-19「立即执行 run_id 为空」修复）：原 fire-and-forget
转发信封（``cron_tools._send_split``）不含 run_id——真实 run_id 由 gateway
调度器收到触发后才生成，信封无法预置。改为 Reverse RPC 请求-响应：gateway
同步触发成功后把 run_id 随响应回填给发起聊天的工具调用。

payload/result 均为平凡 dict，不建模型类：
- request payload: ``{"job_id": "<job id>"}``
- success result:  ``{"run_id": "<job_id>:<epoch_ms>"}``
"""

CRON_RUN_NOW_REVERSE_RPC_METHOD = "cron.run_now"

# 调用方超时：trigger_run_now 在 gateway 内同步起跑（毫秒级），管道 RTT 毫秒级，
# 8s 是数量级余量。超时后触发是否已送达是 ambiguous 的，调用方不得重发（防双触发）。
CRON_RUN_NOW_TIMEOUT_SECONDS = 8.0

# gateway 侧 CapabilitySpec.max_timeout_seconds 上限（须 >= 调用方超时）。
CRON_RUN_NOW_MAX_TIMEOUT_SECONDS = 30.0
