# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Restore process-owned checkpoint state after lifecycle fault injection."""

import pytest
from openjiuwen.core.session.checkpointer import CheckpointerFactory

from jiuwenswarm.runtime import checkpointer


@pytest.fixture(autouse=True)
def isolate_checkpoint_lifecycle_state():
    # Mock providers must not be reinstalled by a later Runtime or agent turn.
    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "_PERSISTENT_CHECKPOINTER_READY", "_shared_checkpointer",
            "_shared_engine", "_shared_engine_kind",
            "_PERSISTENT_CHECKPOINTER_LOCK", "_PERSISTENT_CHECKPOINTER_LOCK_LOOP",
        ):
            patch.setattr(checkpointer, name, getattr(checkpointer, name))
        patch.setattr(CheckpointerFactory, "_default_checkpointer", CheckpointerFactory._default_checkpointer)
        yield
