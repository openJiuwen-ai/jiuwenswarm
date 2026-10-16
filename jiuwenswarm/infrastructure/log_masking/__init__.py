# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""日志脱敏：规则编译、引擎与 logging Filter。"""

from .engine import (
    DEFAULT_REPLACEMENT,
    CompiledMaskingRule,
    LogMaskingEngine,
)
from .filter import SensitiveDataFilter
from .probes import (
    LOG_MASKING_PROBE_SAMPLES,
    emit_log_masking_probe_samples,
    log_masking_probes_enabled,
    maybe_emit_log_masking_probe_samples,
)

reload_from_rows = LogMaskingEngine.reload_from_rows
compile_masking_rows = LogMaskingEngine.compile_masking_rows
reload_log_masking_rule = LogMaskingEngine.reload_log_masking_rule
reload_log_masking_from_gateway_db = LogMaskingEngine.reload_log_masking_from_gateway_db

__all__ = (
    "DEFAULT_REPLACEMENT",
    "CompiledMaskingRule",
    "LogMaskingEngine",
    "SensitiveDataFilter",
    "compile_masking_rows",
    "reload_from_rows",
    "reload_log_masking_rule",
    "reload_log_masking_from_gateway_db",
    "emit_log_masking_probe_samples",
    "log_masking_probes_enabled",
    "maybe_emit_log_masking_probe_samples",
    "LOG_MASKING_PROBE_SAMPLES",
)
