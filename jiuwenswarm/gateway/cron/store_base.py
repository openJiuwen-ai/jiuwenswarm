"""Compatibility alias for cron store backend protocol now owned by Agent Runtime."""

import sys

from jiuwenswarm.runtime.cron import store_base as _implementation

sys.modules[__name__] = _implementation