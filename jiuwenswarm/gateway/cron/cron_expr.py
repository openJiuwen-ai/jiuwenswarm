"""Compatibility alias for cron expressions now owned by Agent Runtime."""

import sys

from jiuwenswarm.runtime.cron import cron_expr as _implementation

sys.modules[__name__] = _implementation
