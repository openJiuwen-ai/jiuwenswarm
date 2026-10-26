# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""把宿主持久化的会话创建依据交给Todo提交入口。"""

import asyncio

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.session.session_metadata import (
    check_todo_initialization,
)


def bind_todo_lifecycle(tool):
    async def guard(session_id, snapshot, *, committing=False):
        # Resolve in the request context, before moving blocking metadata I/O.
        root = get_agent_sessions_dir()
        await asyncio.to_thread(
            check_todo_initialization,
            session_id,
            file_exists=snapshot.exists,
            committing=committing,
            sessions_root=root,
        )

    tool.snapshot_guard = guard
