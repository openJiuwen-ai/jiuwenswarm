"""Compatibility alias for cron models now owned by Agent Runtime."""

import sys

from jiuwenswarm.runtime.cron import models as _implementation

sys.modules[__name__] = _implementation
