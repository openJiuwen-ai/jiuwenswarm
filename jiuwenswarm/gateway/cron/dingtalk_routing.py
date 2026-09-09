"""Compatibility alias for transport-neutral DingTalk cron bindings."""

import sys

from jiuwenswarm.runtime.cron import dingtalk_routing as _implementation

sys.modules[__name__] = _implementation
