"""Compatibility alias for the cron store factory now owned by Agent Runtime."""

import sys

from jiuwenswarm.gateway.runtime.cron import factory as _implementation

sys.modules[__name__] = _implementation