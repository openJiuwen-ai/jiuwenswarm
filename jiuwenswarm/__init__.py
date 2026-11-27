# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwenswarm package.

Package import is the only hook that is guaranteed to run before any
jiuwenswarm module imports ``openjiuwen_runtime``, whose import-time
``Settings()`` would otherwise ``mkdir`` ``<install drive>:\\tmp`` on Windows.
See ``jiuwenswarm/runtime_work_dirs.py``. Keep this file free of other side
effects.
"""

from jiuwenswarm.runtime_work_dirs import ensure_runtime_work_dirs as _ensure_runtime_work_dirs

_ensure_runtime_work_dirs()
