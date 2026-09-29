# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把配额检查挂到 SysOperation.fs().write_file（写文件工具统一入口）。"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from jiuwenswarm.common.workspace.quota import (
    WORKSPACE_QUOTA_EXCEEDED,
    WorkspaceQuotaExceeded,
    check_workspace_write,
    estimate_text_write_additional,
)

logger = logging.getLogger(__name__)

_GUARD_FLAG = "_jiuwenswarm_quota_guard_installed"


def install_write_quota_guard(sys_operation: Any) -> None:
    """包装 ``fs().write_file``；已包装则跳过。"""
    if sys_operation is None:
        return
    try:
        fs = sys_operation.fs()
    except Exception:  # noqa: BLE001
        return
    if fs is None or getattr(fs, _GUARD_FLAG, False):
        return

    original = fs.write_file

    async def guarded_write_file(
        path: str,
        content: str | bytes,
        *,
        mode: str = "text",
        prepend_newline: bool = True,
        append_newline: bool = False,
        append: bool = False,
        create_if_not_exist: bool = True,
        permissions: str = "644",
        encoding: str = "utf-8",
        options: Any = None,
    ):
        try:
            from jiuwenswarm.server.runtime.workspace.policy_reload import (
                reload_quota_policies_from_gateway_db,
            )

            await reload_quota_policies_from_gateway_db()
            # 与 FsOperation 实际写入对齐的净增估算（含可选换行）。
            payload: str | bytes = content
            if mode == "text" and not isinstance(content, (bytes, bytearray)):
                txt = str(content)
                if prepend_newline:
                    txt = "\n" + txt
                if append_newline:
                    txt = txt + "\n"
                payload = txt
            add = estimate_text_write_additional(
                path, payload, encoding=encoding, append=append
            )
            # du / walk 可能阻塞；卸到线程池，避免卡住 Agent 事件循环。
            await asyncio.to_thread(
                functools.partial(check_workspace_write, additional_bytes=add)
            )
        except WorkspaceQuotaExceeded as exc:
            logger.info(
                "[workspace.quota] write_file blocked path=%s detail=%s",
                path,
                exc.detail,
            )
            try:
                from openjiuwen.core.common.exception.codes import StatusCode
                from openjiuwen.core.sys_operation.result import (
                    WriteFileResult,
                    build_operation_error_result,
                )

                return build_operation_error_result(
                    error_type=StatusCode.SYS_OPERATION_FS_EXECUTION_ERROR,
                    msg_format_kwargs={
                        "execution": "write_file",
                        "error_msg": WORKSPACE_QUOTA_EXCEEDED,
                    },
                    result_cls=WriteFileResult,
                )
            except Exception:  # noqa: BLE001
                raise exc from None

        return await original(
            path,
            content,
            mode=mode,
            prepend_newline=prepend_newline,
            append_newline=append_newline,
            append=append,
            create_if_not_exist=create_if_not_exist,
            permissions=permissions,
            encoding=encoding,
            options=options,
        )

    fs.write_file = guarded_write_file  # type: ignore[method-assign]
    setattr(fs, _GUARD_FLAG, True)
