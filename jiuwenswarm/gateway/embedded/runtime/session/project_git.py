"""Git error types used by Gateway handlers.

Only the response helpers are kept here. Repository commands stay on AgentServer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_GIT_OUTPUT_TRUNCATE = 4000


@dataclass(slots=True)
class GitError:
    """Git 操作失败时的结构化错误对象。"""

    code: str
    message: str
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    hint: str = ""
    retryable: bool = False
    repo: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:_GIT_OUTPUT_TRUNCATE],
            "stderr": self.stderr[:_GIT_OUTPUT_TRUNCATE],
            "hint": self.hint,
            "retryable": self.retryable,
            "repo": self.repo,
        }


class GitOperationError(RuntimeError):
    """Git 操作失败,携带结构化 ``GitError`` 供 handler 层映射错误码。"""

    def __init__(self, git_error: GitError) -> None:
        self.git_error = git_error
        super().__init__(git_error.message)


def send_git_error_response(
    channel: Any, ws: Any, req_id: str, error: Any,
) -> Any:
    """发送 Git 结构化错误响应。"""
    if isinstance(error, GitError):
        git_error = error
    else:
        git_error = getattr(error, "git_error", None)
    if git_error is not None:
        detail = git_error.to_dict() if hasattr(git_error, "to_dict") else dict(git_error)
        return channel.send_response(
            ws, req_id, ok=False,
            payload={"detail": detail},
            error=git_error.message,
            code=git_error.code,
        )
    logger.warning("[GitHandler] error: %s", error)
    return channel.send_response(
        ws, req_id, ok=False,
        error=f"handler error: {error}", code="INTERNAL_ERROR",
    )
