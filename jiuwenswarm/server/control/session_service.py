# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session query/list Control Service. Does not create, switch, or delete sessions."""

from __future__ import annotations

from jiuwenswarm.server.control.repositories.session_repository import (
    handle_session_request,
)

__all__ = ["handle_session_request"]
