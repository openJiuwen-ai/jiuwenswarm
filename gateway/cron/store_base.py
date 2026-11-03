"""Compatibility alias for cron store backend protocol now owned by Agent Runtime."""

import sys

from gateway.runtime.cron import store_base as _implementation

sys.modules[__name__] = _implementation