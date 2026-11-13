# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility import for Skill authorization APIs now owned by agent-core.

New code should import :mod:`openjiuwen.harness.security.skill_authorization`
directly. This package remains temporarily so extensions using the old
JiuwenSwarm internal path do not fail immediately.
"""

from openjiuwen.harness.security.skill_authorization import *  # noqa: F401,F403
from openjiuwen.harness.security.skill_authorization import __all__
