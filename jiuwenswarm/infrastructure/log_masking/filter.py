# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""logging.Filter：对日志消息 / identity / traceback 应用 LogMaskingEngine。"""

from __future__ import annotations

import logging
import traceback as _traceback

from jiuwenswarm.edition import is_enterprise

from .engine import LogMaskingEngine

_logger = logging.getLogger(__name__)
_LOG_MASKING_DONE_ATTR = "_jiuwenswarm_log_masking_done"


class SensitiveDataFilter(logging.Filter):
    """Mask sensitive data in log messages, identity prefix, and tracebacks.

    统一走 ``LogMaskingEngine``（内置规则 + 企业版 GDB/热更规则）。
    须挂在 ``IdentityFieldFilter`` **之后**：先拼 identity，再脱敏。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _LOG_MASKING_DONE_ATTR, False):
            return True

        engine = LogMaskingEngine.get_instance()

        try:
            message = record.getMessage()
            record.msg = engine.sanitize(message)
            record.args = ()
        except Exception:
            # Never block logging because of desensitization failure.
            _logger.debug(
                "[LogMasking] sensitive data filter failed",
                exc_info=True,
            )

        # identity 由 IdentityFieldFilter 预先拼好；企业版走引擎（内置即生效，
        # GDB 冷加载/热更后替换为库规则）。非企业版不脱敏前缀，避免误伤。
        # 同时回写 user_id/domain_id/app_id，供 JSON Formatter 直接读字段时不泄露。
        try:
            identity = getattr(record, "identity", None)
            if isinstance(identity, str) and identity and is_enterprise():
                record.identity = engine.sanitize(identity)
                for field in ("user_id", "domain_id", "app_id"):
                    val = getattr(record, field, None)
                    if not isinstance(val, str) or not val:
                        continue
                    masked_piece = engine.sanitize(f"{field}={val}")
                    prefix = f"{field}="
                    if masked_piece.startswith(prefix):
                        setattr(record, field, masked_piece[len(prefix):])
        except Exception:
            # 不阻断日志；保留原始 identity。
            _logger.debug(
                "[LogMasking] identity sanitize failed",
                exc_info=True,
            )

        # Traceback 由 Formatter.formatException() 在 record.exc_text 中单独渲染，
        # 不经过 record.getMessage()，因此 message 脱敏覆盖不到。
        try:
            exc_info = record.exc_info
            if exc_info and not record.exc_text:
                # Python 3.10+ 推荐 format_exception(exc) 单参形式。
                formatted = "".join(_traceback.format_exception(exc_info[1]))
                record.exc_text = engine.sanitize(formatted)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = engine.sanitize(record.exc_text)
        except Exception:
            # 不阻断日志；保留未脱敏 traceback，避免吞异常无记录。
            _logger.debug(
                "[LogMasking] traceback sanitize failed",
                exc_info=True,
            )

        setattr(record, _LOG_MASKING_DONE_ATTR, True)
        return True
