"""Compatibility alias for cron persistence now owned by Agent Runtime."""

import sys

from jiuwenswarm.gateway.runtime.cron import store as _implementation

sys.modules[__name__] = _implementation
