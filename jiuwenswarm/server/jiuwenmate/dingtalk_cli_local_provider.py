"""Run scoped DingTalk app APIs and employee OAuth commands through DWS."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import time
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.configuration import (
    allow_from_valid,
    dingtalk_apps,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir

_CLI_VERSION = "1.0.65"
_CLI_PACKAGE = f"dingtalk-workspace-cli@{_CLI_VERSION}"
_MAX_OUTPUT_BYTES = 240 * 1024
_CLI_REQUEST_TIMEOUT_SECONDS = 30
_MAX_GROUP_FILE_SIZE = 100 * 1024 * 1024
_NPM_CACHE_LOCK_TIMEOUT_SECONDS = 5
_GROUP_MEMBER_PAGE_SIZE = 200
_MAX_GROUP_CALENDAR_ATTENDEES = 5_000
_MAX_CALENDAR_ATTENDEES_PER_REQUEST = 150
_DINGTALK_AUTH_TIMEOUT_SECONDS = 600
_DINGTALK_AUTH_OPERATION = re.compile(r"^[a-f0-9]{20}\.[a-f0-9]{32}$")
_SENSITIVE_FIELD = re.compile(
    r"(?:secret|password|authorization|(?:access|refresh|app)[_-]?token)", re.I
)
_PERMISSION_PURPOSES = {
    "qyapi_get_member": "通讯录用户详情读取权限",
    "Contact.User.Read": "企业用户读取权限",
    "ConvFile.Space.Read": "群文件空间读权限",
    "ConvFile.File.Send": "群文件发送权限",
    "Storage.File.Read": "企业存储文件读权限",
    "Storage.File.Write": "企业存储文件写权限",
    "Storage.UploadInfo.Read": "企业存储文件上传信息读权限",
    "Storage.File.UploadInfo.Read": "企业存储文件上传信息读权限",
    "Document.Workspace.Read": "知识库空间读取权限",
    "Document.Workspace.Write": "知识库空间管理权限",
    "Document.WorkspaceDocument.Read": "知识库文档读取权限",
    "Document.WorkspaceDocument.Write": "知识库文档写入权限",
    "Wiki.Node.Read": "知识库目录读取权限",
    "Calendar.Event.Write": "日历应用中日程写权限",
}


def _safe_api_path(value: str) -> str:
    """Keep permission diagnostics useful without echoing arbitrary query values."""
    try:
        return urlsplit(value).path or "/"
    except ValueError:
        return value.split("?", 1)[0]


def _find_npx() -> Path | None:
    configured = os.environ.get("JIUWENMATE_DINGTALK_CLI_NPX")
    candidates: list[Path] = []
    if configured:
        located = shutil.which(configured)
        candidates.append(Path(located) if located else Path(configured).expanduser())
    located = shutil.which("npx")
    if located:
        candidates.append(Path(located))
    home = Path.home()
    versioned_node_dir = home / ".nvm" / "versions" / "node"
    try:
        candidates.extend(
            sorted(versioned_node_dir.glob("*/bin/npx"), key=lambda path: path.stat().st_mtime, reverse=True)
        )
        candidates.extend(
            sorted(versioned_node_dir.glob("*/bin/npx.cmd"), key=lambda path: path.stat().st_mtime, reverse=True)
        )
    except OSError:
        pass
    candidates.extend([
        home / ".nvm" / "versions" / "node" / "current" / "bin" / "npx",
        home / ".volta" / "bin" / ("npx.cmd" if os.name == "nt" else "npx"),
        home / ".asdf" / "shims" / ("npx.cmd" if os.name == "nt" else "npx"),
        home / ".npm-global" / "bin" / ("npx.cmd" if os.name == "nt" else "npx"),
        Path("/opt/homebrew/bin/npx"),
        Path("/usr/local/bin/npx"),
        Path("/usr/bin/npx"),
    ])
    for candidate in candidates:
        if candidate.is_dir():
            candidate = candidate / ("npx.cmd" if os.name == "nt" else "npx")
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate.absolute()
    return None


def _default_runner(
    args: list[str], *, env: dict[str, str], input_text: str | None, timeout: float | None, cwd: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )


@contextmanager
def _npm_cache_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Serialize npx installs that share npm's cache across processes."""
    private_dir(path.parent)
    path.touch(exist_ok=True)
    with path.open("r+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise PersistentAgentError(
                            "DingTalk CLI package cache is busy; stop retrying DingTalk API calls for this request",
                            code="DINGTALK_CLI_CACHE_BUSY",
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise PersistentAgentError(
                        "DingTalk CLI package cache is busy; stop retrying DingTalk API calls for this request",
                        code="DINGTALK_CLI_CACHE_BUSY",
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class LocalDingTalkCliRuntime:
    """Use the shared default app, or a sole configured app, for every employee."""

    def __init__(
        self,
        *,
        root: Path,
        workspace_root: Path | None = None,
        config_loader: Callable[[], Mapping[str, Any]] | None = None,
        dws_path: Path | None = None,
        npx_path: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = _default_runner,
        signed_upload: Callable[[str, Mapping[str, str], bytes], Awaitable[None]] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        if workspace_root is None:
            from jiuwenswarm.server.jiuwenmate.service import default_data_dir

            workspace_root = default_data_dir() / "workspaces"
        self._workspace_root = Path(workspace_root).resolve()
        self._config = config_loader or self._load_config
        self._dws_path = Path(dws_path).expanduser().absolute() if dws_path is not None else None
        self._npx_path = Path(npx_path).expanduser().absolute() if npx_path is not None else _find_npx()
        self._runner = runner
        self._signed_upload = signed_upload or self._upload_signed_url
        self._oauth_locks: dict[str, threading.RLock] = {}

    def _read_group_output_file(self, scope: DingTalkCliScope, file_path: str) -> tuple[str, bytes]:
        if (
            not isinstance(file_path, str)
            or len(file_path) > 512
            or "\\" in file_path
            or "\x00" in file_path
        ):
            raise PersistentAgentError(
                "Only shared output files can be uploaded", code="DINGTALK_CLI_UPLOAD_FILE_INVALID"
            )
        parts = file_path.split("/")
        if (
            len(parts) != 2
            or parts[0] != "output"
            or parts[1] in ("", ".", "..")
            or len(parts[1]) > 255
            or any(ord(char) < 32 or char in '/\\:*?"<>|' for char in parts[1])
        ):
            raise PersistentAgentError(
                "Only output/<file name> can be uploaded", code="DINGTALK_CLI_UPLOAD_FILE_INVALID"
            )
        name = parts[1]
        office = self._workspace_root / scope.namespace_id / scope.agent_id
        output_dir = office / "output"
        try:
            workspace_root = self._workspace_root.resolve(strict=True)
            office_root = office.resolve(strict=True)
            office_root.relative_to(workspace_root)
            if office_root != office:
                raise ValueError("employee workspace is redirected")
            output_metadata = output_dir.lstat()
            if not stat.S_ISDIR(output_metadata.st_mode) or stat.S_ISLNK(output_metadata.st_mode):
                raise ValueError("output directory is not a regular directory")
            output_root = output_dir.resolve(strict=True)
            output_root.relative_to(workspace_root)
            if output_root != output_dir:
                raise ValueError("output directory is redirected")
            path = output_dir / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise ValueError("upload source is not a regular file")
            if before.st_size > _MAX_GROUP_FILE_SIZE:
                raise ValueError("upload source exceeds the file size limit")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("upload source changed while opening")
                chunks: list[bytes] = []
                remaining = _MAX_GROUP_FILE_SIZE + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
            finally:
                os.close(descriptor)
        except PersistentAgentError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise PersistentAgentError(
                "Shared output file is unavailable or unsafe", code="DINGTALK_CLI_UPLOAD_FILE_INVALID"
            ) from exc
        if len(content) != before.st_size or len(content) > _MAX_GROUP_FILE_SIZE:
            raise PersistentAgentError(
                "Shared output file changed while reading", code="DINGTALK_CLI_UPLOAD_FILE_INVALID"
            )
        return name, content

    @staticmethod
    def _docx_bytes(content: str) -> bytes:
        paragraphs = "".join(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            f"{html.escape(line, quote=False)}"
            "</w:t></w:r></w:p>"
            for line in (content.splitlines() or [""])
        )
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}<w:sectPr/></w:body></w:document>"
        )
        files = {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>'
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>'
            ),
            "word/document.xml": document,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, value in files.items():
                archive.writestr(path, value.encode("utf-8"))
        return buffer.getvalue()

    @staticmethod
    def _api_body(response: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        status = response.get("statusCode")
        if isinstance(status, int) and not 200 <= status < 300:
            raise PersistentAgentError(
                f"钉钉 API {endpoint} 返回 HTTP {status}", code="DINGTALK_CLI_API_ERROR"
            )
        body: Mapping[str, Any] = response
        if isinstance(response.get("body"), dict):
            body = response["body"]
        elif isinstance(response.get("data"), dict):
            body = response["data"]
        if body.get("success") is False:
            error_code = body.get("errcode") or body.get("code")
            code_detail = f"（errcode={error_code}）" if error_code is not None else ""
            raise PersistentAgentError(
                f"钉钉 API {endpoint} 返回失败结果{code_detail}", code="DINGTALK_CLI_API_ERROR"
            )
        errcode = body.get("errcode")
        if errcode not in (None, 0, "0"):
            raise PersistentAgentError(
                f"钉钉 API {endpoint} 返回 errcode={errcode}", code="DINGTALK_CLI_API_ERROR"
            )
        if isinstance(body.get("result"), dict):
            return body["result"]
        return dict(body)

    @staticmethod
    def _calendar_datetime(value: str, time_zone: str, field: str) -> str:
        try:
            zone = ZoneInfo(time_zone)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
            raise PersistentAgentError(
                f"{field} must be a valid ISO-8601 datetime and time_zone must be a valid IANA timezone",
                code="BAD_REQUEST",
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.isoformat()

    @staticmethod
    def _group_member_page(response: Mapping[str, Any]) -> tuple[list[Any], bool, int | None]:
        pending: list[Any] = [response]
        visited: set[int] = set()
        containers: list[Mapping[str, Any]] = []
        while pending:
            item = pending.pop(0)
            if not isinstance(item, Mapping) or id(item) in visited:
                continue
            visited.add(id(item))
            containers.append(item)
            pending.extend(value for value in item.values() if isinstance(value, Mapping))

        members: list[Any] | None = None
        for container in containers:
            for key in ("members", "list", "memberList", "memberUserIds", "member_user_ids"):
                value = container.get(key)
                if isinstance(value, list):
                    members = value
                    break
            if members is not None:
                break
        if members is None:
            raise PersistentAgentError(
                "钉钉群成员接口未返回成员列表，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )

        has_more: bool | None = None
        next_cursor: int | None = None
        for container in containers:
            for key in ("hasMore", "has_more"):
                if isinstance(container.get(key), bool):
                    has_more = container[key]
                    break
            for key in ("nextCursor", "next_cursor", "nextToken", "next_token"):
                value = container.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    next_cursor = value
                    break
            if has_more is not None and next_cursor is not None:
                break
        if has_more is None:
            raise PersistentAgentError(
                "钉钉群成员接口未返回有效的 hasMore 分页标记，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        if has_more and next_cursor is None:
            raise PersistentAgentError(
                "钉钉群成员接口表示仍有下一页，但没有返回整数 nextToken，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        return members, has_more, next_cursor

    async def _query_group_staff_ids(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
    ) -> list[str]:
        binding = self._binding(scope)
        if binding is None:
            raise PersistentAgentError(
                "No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED"
            )
        endpoint = "/v1.0/im/innerGroups/members/query"
        staff_ids: list[str] = []
        seen_ids: set[str] = set()
        seen_cursors: set[int] = set()
        cursor = 0
        for _ in range((_MAX_GROUP_CALENDAR_ATTENDEES + _GROUP_MEMBER_PAGE_SIZE - 1) // _GROUP_MEMBER_PAGE_SIZE):
            if cursor in seen_cursors:
                raise PersistentAgentError(
                    "钉钉群成员接口返回了重复 nextToken，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            seen_cursors.add(cursor)
            try:
                response = await self._execute_internal_api(scope, [
                    "api", "POST", endpoint,
                    "--data", json.dumps({
                        "openConversationId": conversation_id,
                        "userId": sender_staff_id,
                        "maxResults": _GROUP_MEMBER_PAGE_SIZE,
                        "nextToken": cursor,
                    }, ensure_ascii=False),
                ])
            except PersistentAgentError as exc:
                if exc.code == "DINGTALK_CLI_API_FORBIDDEN":
                    raise PersistentAgentError(
                        "应用无法读取完整群名单；请先在该员工渠道设置完成用户授权，未创建日程",
                        code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                    ) from exc
                raise PersistentAgentError(
                    f"读取当前钉钉群成员失败：{exc}。未创建日程。",
                    code=exc.code,
                ) from exc
            except Exception as exc:
                raise PersistentAgentError(
                    f"读取当前钉钉群成员失败：应用身份调用失败（{type(exc).__name__}）；未创建日程。",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                ) from exc

            page_members, has_more, next_cursor = self._group_member_page(self._api_body(response, endpoint))
            for member in page_members:
                if isinstance(member, str):
                    staff_id = member
                elif isinstance(member, Mapping):
                    staff_id = member.get("userId")
                else:
                    staff_id = None
                if not isinstance(staff_id, str) or not staff_id:
                    raise PersistentAgentError(
                        "钉钉群成员接口返回了缺少 userId 的成员记录，未创建日程",
                        code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                    )
                if staff_id not in seen_ids:
                    staff_ids.append(staff_id)
                    seen_ids.add(staff_id)
            if len(staff_ids) > _MAX_GROUP_CALENDAR_ATTENDEES:
                raise PersistentAgentError(
                    f"当前群成员超过钉钉日程参与人上限 {_MAX_GROUP_CALENDAR_ATTENDEES}，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            if not has_more:
                break
            if next_cursor is None or next_cursor in seen_cursors:
                raise PersistentAgentError(
                    "钉钉群成员接口返回了无效 nextToken，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            cursor = next_cursor
        else:
            raise PersistentAgentError(
                "钉钉群成员接口超过分页上限，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )

        if sender_staff_id not in seen_ids:
            raise PersistentAgentError(
                "钉钉返回的群成员名单不包含当前消息发送者，未创建日程以避免邀请错误群组",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        return staff_ids

    @staticmethod
    def _dws_user_group_member_page(response: Mapping[str, Any]) -> tuple[list[Any], bool, str | None]:
        pending: list[Any] = [response]
        visited: set[int] = set()
        containers: list[Mapping[str, Any]] = []
        while pending:
            item = pending.pop(0)
            if not isinstance(item, Mapping) or id(item) in visited:
                continue
            visited.add(id(item))
            containers.append(item)
            pending.extend(value for value in item.values() if isinstance(value, Mapping))

        members: list[Any] | None = None
        for container in containers:
            if container.get("success") is False or isinstance(container.get("error"), Mapping):
                error = container.get("error")
                message = error.get("message") if isinstance(error, Mapping) else None
                detail = message if isinstance(message, str) else "DWS CLI 未返回可用的成员结果"
                raise PersistentAgentError(
                    f"员工钉钉授权账号读取群成员失败：{detail}；未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            for key in ("members", "list", "memberList", "memberUserIds", "member_user_ids", "userIds", "user_ids"):
                value = container.get(key)
                if isinstance(value, list):
                    members = value
                    break
            if members is not None:
                break
        if members is None:
            raise PersistentAgentError(
                "员工钉钉授权账号的群成员命令未返回成员列表，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )

        has_more: bool | None = None
        next_cursor: str | None = None
        for container in containers:
            for key in ("hasMore", "has_more"):
                if isinstance(container.get(key), bool):
                    has_more = container[key]
                    break
            for key in ("nextCursor", "next_cursor", "nextToken", "next_token"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    next_cursor = value
                    break
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    next_cursor = str(value)
                    break
            if has_more is not None and (not has_more or next_cursor is not None):
                break
        if has_more is None or (has_more and next_cursor is None):
            raise PersistentAgentError(
                "员工钉钉授权账号的群成员结果缺少有效分页信息，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        return members, has_more, next_cursor

    async def _query_group_invitees_as_user(
        self, scope: DingTalkCliScope, conversation_id: str, profile: str | None,
    ) -> tuple[list[str], list[str]]:
        """Keep userId and openDingTalkId distinct, including cross-organization members."""
        staff_ids: list[str] = []
        open_ids: list[str] = []
        seen: set[tuple[str, str]] = set()
        seen_cursors: set[str] = set()
        cursor = "0"
        for _ in range(25):
            seen_cursors.add(cursor)
            response = await self._execute_oauth_command(scope, [
                "chat", "group", "members", "--id", conversation_id, "--cursor", cursor,
            ], profile)
            members, has_more, next_cursor = self._dws_user_group_member_page(response)
            for member in members:
                if isinstance(member, str):
                    kind, value = "staff", member
                elif isinstance(member, Mapping):
                    value = member.get("openDingtalkId") or member.get("openDingTalkId")
                    kind = "open"
                    if not value:
                        kind = "staff"
                        value = member.get("userId") or member.get("userid") or member.get("user_id")
                else:
                    kind, value = "staff", None
                if not isinstance(value, str) or not value.strip() or "," in value or any(char.isspace() for char in value):
                    raise PersistentAgentError("群成员缺少有效身份 ID，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")
                if (kind, value) not in seen:
                    seen.add((kind, value))
                    (open_ids if kind == "open" else staff_ids).append(value)
                if len(seen) > 500:
                    raise PersistentAgentError("当前群人数超过用户日历单次 500 人上限，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")
            if not has_more:
                if not seen:
                    raise PersistentAgentError("当前群成员列表为空，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")
                return staff_ids, open_ids
            if not next_cursor or next_cursor in seen_cursors:
                raise PersistentAgentError("群成员分页游标无效，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")
            cursor = next_cursor
        raise PersistentAgentError("群成员名单超过读取上限，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")

    async def _create_group_calendar_as_user(
        self, scope: DingTalkCliScope, *, auth: dict[str, Any], conversation_id: str,
        summary: str, start: str, end: str, time_zone: str, description: str,
        reminder_minutes: int | None, create_video_meeting: bool, extra_staff_ids: list[str],
    ) -> dict[str, Any]:
        profile = auth.get("_profile")
        staff_ids, open_ids = await self._query_group_invitees_as_user(scope, conversation_id, profile)
        group_count = len(staff_ids) + len(open_ids)
        staff_ids = list(dict.fromkeys([*staff_ids, *[value.strip() for value in extra_staff_ids]]))
        if len(staff_ids) + len(open_ids) > 500:
            raise PersistentAgentError("参会人数超过用户日历单次 500 人上限，未创建日程", code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE")
        argv = [
            "calendar", "event", "create", "--title", summary.strip(), "--start", start,
            "--end", end, "--timezone", time_zone,
            f"--add-online-meeting={'true' if create_video_meeting else 'false'}",
        ]
        if staff_ids:
            argv.extend(["--attendees", ",".join(staff_ids)])
        if open_ids:
            argv.extend(["--open-dingtalk-ids", ",".join(open_ids)])
        if description.strip():
            argv.extend(["--desc", description.strip()])
        if reminder_minutes is not None:
            argv.extend(["--remind-minutes", str(reminder_minutes)])
        response = await self._execute_oauth_command(scope, argv, profile)
        body = response.get("result") if isinstance(response.get("result"), dict) else response
        event_id = body.get("eventId") or body.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise PersistentAgentError("钉钉未返回日程 ID；请先检查日历，不要重复创建", code="DINGTALK_CLI_CREATE_RESULT_INVALID")
        return {
            "created": True, "eventId": event_id, "summary": summary.strip(),
            "groupMemberCount": group_count, "requestedAttendeeCount": len(staff_ids) + len(open_ids),
            "organizerAccount": auth.get("account"),
        }

    async def _staff_union_id(self, scope: DingTalkCliScope, staff_id: str) -> str:
        endpoint = "/topapi/v2/user/get"
        try:
            response = await self._execute_internal_api(scope, [
                "api", "POST", endpoint, "--base-url", "https://oapi.dingtalk.com",
                "--data", json.dumps({"userid": staff_id, "language": "zh_CN"}, ensure_ascii=False),
            ])
            body = self._api_body(response, endpoint)
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError(
                f"无法将群成员映射为日程参与人：POST {endpoint} 失败",
                code="DINGTALK_CLI_IDENTITY_UNAVAILABLE",
            ) from exc
        union_id = body.get("unionid")
        if not isinstance(union_id, str) or not union_id:
            raise PersistentAgentError(
                f"无法将群成员映射为日程参与人：POST {endpoint} 未返回 unionid；"
                "请检查通讯录成员详情读取权限，未创建日程",
                code="DINGTALK_CLI_IDENTITY_UNAVAILABLE",
            )
        return union_id

    async def create_group_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        summary: str,
        start_datetime: str,
        end_datetime: str,
        time_zone: str = "Asia/Shanghai",
        description: str = "",
        reminder_minutes: int | None = None,
        create_video_meeting: bool = False,
        extra_attendee_staff_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create one primary-calendar event and invite every member in the trusted current group."""
        if not self.is_available(scope):
            raise PersistentAgentError(
                "No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED"
            )
        if (
            not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 512
            or not isinstance(conversation_id, str) or not conversation_id.strip()
            or not isinstance(sender_staff_id, str) or not sender_staff_id.strip()
            or not isinstance(time_zone, str) or not time_zone.strip()
            or not isinstance(start_datetime, str) or not isinstance(end_datetime, str)
            or not isinstance(description, str) or len(description) > 4000
            or isinstance(reminder_minutes, bool)
            or (reminder_minutes is not None and (not isinstance(reminder_minutes, int) or not 0 <= reminder_minutes <= 10080))
            or not isinstance(create_video_meeting, bool)
            or (extra_attendee_staff_ids is not None and (
                not isinstance(extra_attendee_staff_ids, list)
                or any(not isinstance(item, str) or not item.strip() for item in extra_attendee_staff_ids)
            ))
        ):
            raise PersistentAgentError("Invalid DingTalk group calendar event request", code="BAD_REQUEST")
        start = self._calendar_datetime(start_datetime, time_zone, "start_datetime")
        end = self._calendar_datetime(end_datetime, time_zone, "end_datetime")
        if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise PersistentAgentError("end_datetime must be later than start_datetime", code="BAD_REQUEST")

        auth = await self.dingtalk_auth_status(scope)
        if auth.get("status") == "connected":
            return await self._create_group_calendar_as_user(
                scope, auth=auth, conversation_id=conversation_id, summary=summary,
                start=start, end=end, time_zone=time_zone, description=description,
                reminder_minutes=reminder_minutes, create_video_meeting=create_video_meeting,
                extra_staff_ids=extra_attendee_staff_ids or [],
            )
        staff_ids = await self._query_group_staff_ids(
            scope, conversation_id=conversation_id, sender_staff_id=sender_staff_id
        )
        attendee_staff_ids = [staff_id for staff_id in staff_ids if staff_id != sender_staff_id]
        attendee_staff_ids.extend(
            staff_id for staff_id in (extra_attendee_staff_ids or [])
            if staff_id not in staff_ids and staff_id != sender_staff_id
        )
        organizer_union_id = await self._staff_union_id(scope, sender_staff_id)
        attendee_union_ids = [await self._staff_union_id(scope, staff_id) for staff_id in attendee_staff_ids]

        event_endpoint = f"/v1.0/calendar/users/{quote(organizer_union_id, safe='')}/calendars/primary/events"
        create_payload: dict[str, Any] = {
            "summary": summary.strip(),
            "start": {"dateTime": start, "timeZone": time_zone},
            "end": {"dateTime": end, "timeZone": time_zone},
            "attendees": [
                {"id": union_id, "isOptional": False}
                for union_id in attendee_union_ids[:_MAX_CALENDAR_ATTENDEES_PER_REQUEST]
            ],
        }
        if description.strip():
            create_payload["description"] = description.strip()
        if reminder_minutes is not None:
            create_payload["reminders"] = [{"method": "dingtalk", "minutes": reminder_minutes}]
        if create_video_meeting:
            create_payload["onlineMeetingInfo"] = {"type": "dingtalk"}
        try:
            response = await self._execute_internal_api(scope, [
                "api", "POST", event_endpoint,
                "--data", json.dumps(create_payload, ensure_ascii=False),
            ])
            body = self._api_body(response, event_endpoint)
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError(
                f"创建钉钉群日程失败：POST {event_endpoint}；底层错误：{type(exc).__name__}",
                code="DINGTALK_CLI_CREATE_GROUP_CALENDAR_FAILED",
            ) from exc
        event_id = body.get("id") or body.get("eventId")
        if not isinstance(event_id, str) or not event_id:
            raise PersistentAgentError(
                f"钉钉未返回已创建日程的 eventId（POST {event_endpoint}）；请勿重复创建",
                code="DINGTALK_CLI_CREATE_RESULT_INVALID",
            )

        added_count = min(len(attendee_union_ids), _MAX_CALENDAR_ATTENDEES_PER_REQUEST)
        attendee_endpoint = f"{event_endpoint}/{quote(event_id, safe='')}/attendees"
        for offset in range(_MAX_CALENDAR_ATTENDEES_PER_REQUEST, len(attendee_union_ids),
                            _MAX_CALENDAR_ATTENDEES_PER_REQUEST):
            batch = attendee_union_ids[offset:offset + _MAX_CALENDAR_ATTENDEES_PER_REQUEST]
            try:
                response = await self._execute_internal_api(scope, [
                    "api", "POST", attendee_endpoint,
                    "--data", json.dumps({
                        "attendeesToAdd": [{"id": union_id, "isOptional": False} for union_id in batch],
                    }, ensure_ascii=False),
                ])
                self._api_body(response, attendee_endpoint)
                added_count += len(batch)
            except Exception as exc:
                raise PersistentAgentError(
                    f"日程已创建（eventId {event_id}），但只确认添加 {added_count}/{len(attendee_union_ids)} 位群成员；"
                    f"添加剩余成员时 POST {attendee_endpoint} 失败。请勿重建日程；底层错误：{exc}",
                    code="DINGTALK_CLI_CALENDAR_PARTICIPANTS_PARTIAL",
                ) from exc

        return {
            "created": True,
            "eventId": event_id,
            "summary": summary.strip(),
            "organizerUnionId": organizer_union_id,
            "attendeeCount": added_count,
            "groupMemberCount": len(staff_ids),
        }

    @staticmethod
    async def _upload_signed_url(url: str, headers: Mapping[str, str], data: bytes) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host.endswith((".aliyuncs.com", ".alibabausercontent.com", ".dingtalk.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise RuntimeError("DingTalk returned an unsupported upload URL")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
            raise RuntimeError("DingTalk returned invalid upload headers")
        import httpx

        async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
            response = await client.put(url, content=data, headers=dict(headers))
        if not response.is_success:
            raise RuntimeError(f"DingTalk signed upload failed with HTTP {response.status_code}")

    async def _execute_oauth_command(
        self, scope: DingTalkCliScope, argv: list[str], profile: str | None = None,
    ) -> dict[str, Any]:
        """Run only host-owned user commands through one bounded CLI response path."""
        calendar_update = argv[:3] == ["calendar", "event", "update"]
        calendar_read = argv[:3] == ["calendar", "event", "get"]
        calendar_write = argv[:3] == ["calendar", "event", "create"] or calendar_update
        roster_read = argv[:3] == ["chat", "group", "members"]
        bots_read = argv[:3] == ["chat", "group", "bots"]
        history_read = argv[:3] == ["chat", "message", "list"]
        document = tuple(argv[:2]) in {("doc", "create"), ("drive", "info")}
        if not (calendar_write or calendar_read or roster_read or bots_read or history_read or document):
            raise ValueError("unsupported employee OAuth command")
        if history_read or bots_read:
            code = "DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE"
            message = "当前员工的钉钉授权账号无法读取群消息；读取进度未前移"
        elif roster_read:
            code = "DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE"
            message = "当前员工的钉钉授权账号无法读取完整群名单，未创建日程"
        elif calendar_read:
            code = "DINGTALK_CLI_CALENDAR_UNAVAILABLE"
            message = "当前员工授权账号无法读取指定日程，请核对 eventId 和组织账号"
        elif calendar_update:
            code = "DINGTALK_CLI_CALENDAR_UPDATE_FAILED"
            message = "钉钉未确认日程修改结果；请先检查原日程，不要重建或重复修改"
        elif calendar_write:
            code = "DINGTALK_CLI_CALENDAR_CREATE_FAILED"
            message = "钉钉未确认会议创建成功；请先检查日历，不要重复创建"
        else:
            code = "DINGTALK_CLI_CREATE_GROUP_DOC_FAILED"
            message = "钉钉用户文档命令未确认成功；请先检查已创建文档，不要重复创建"
        command = list(argv)
        if "--format" not in command:
            command.extend(["--format", "json"])
        if profile:
            command.extend(["--profile", profile])
        try:
            result = await asyncio.to_thread(
                self._run_cli, command, env=self._oauth_environment(scope), cwd=self._oauth_root(scope),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PersistentAgentError(message, code=code) from exc
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            raise PersistentAgentError(message, code=code)
        payload = self._json_response(stdout)
        valid_response = isinstance(payload, dict) and payload.get("success") is True
        if history_read and isinstance(payload, dict) and isinstance(payload.get('contractVersion'), str):
            valid_response = (
                bool(payload['contractVersion']) and payload.get('success') is not False
                and not payload.get('error') and not payload.get('failures')
                and not payload.get('failedCount') and not payload.get('decryptFailedCount')
            )
        if result.returncode != 0 or not valid_response:
            raise PersistentAgentError(message, code=code)
        return payload

    async def group_history_identity(self, scope: DingTalkCliScope) -> str | None:
        """Identify the isolated authorized reader without exposing its profile."""
        if not self.is_available(scope):
            return None
        auth = await self.dingtalk_auth_status(scope)
        if auth.get("status") == "error":
            raise PersistentAgentError("DingTalk authorization check failed", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE")
        profile = auth.get("_profile")
        if (
            auth.get("status") != "connected" or not isinstance(profile, str) or not profile.strip()
            or len(profile) > 512 or any(ord(char) < 32 for char in profile)
        ):
            return None
        return hashlib.sha256(profile.encode("utf-8")).hexdigest()

    async def read_group_history(
        self, scope: DingTalkCliScope, *, conversation_id: str, since: str,
    ) -> dict[str, Any]:
        """Read bounded history for a host-verified group, without exposing a model tool."""
        if (
            not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 512
            or any(char.isspace() or char == '\x00' for char in conversation_id)
        ):
            raise PersistentAgentError("Invalid group history target", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        try:
            boundary = datetime.fromisoformat(since.replace('Z', '+00:00'))
            if boundary.utcoffset() is None or boundary.year < 2000:
                raise ValueError("A time zone is required")
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise PersistentAgentError("Invalid group history boundary", code="DINGTALK_CLI_GROUP_HISTORY_INVALID") from exc
        if not self.is_available(scope):
            raise PersistentAgentError("DingTalk app is unavailable", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        auth = await self.dingtalk_auth_status(scope)
        if auth.get('status') == 'error':
            raise PersistentAgentError("DingTalk authorization check failed", code="DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE")
        profile = auth.get('_profile')
        if auth.get('status') != 'connected' or not isinstance(profile, str) or not profile:
            raise PersistentAgentError("Employee DingTalk authorization is required", code="DINGTALK_CLI_USER_AUTH_REQUIRED")
        config = self._config()
        channels = config.get('channels') if isinstance(config, Mapping) else None
        raw = channels.get('dingtalk') if isinstance(channels, Mapping) else None
        scoped_apps = [
            app for app in dingtalk_apps(raw)
            if app['enabled'] and app['persistent_agent_id'] == scope.agent_id
        ]
        if len(scoped_apps) != 1:
            raise PersistentAgentError("Employee DingTalk bot binding is unavailable", code="DINGTALK_CLI_GROUP_BOT_NOT_PRESENT")
        robot_code = scoped_apps[0]['robot_code'] or scoped_apps[0]['client_id']
        bot_response = await self._execute_oauth_command(
            scope, ['chat', 'group', 'bots', '--group', conversation_id], profile,
        )
        bot_result = bot_response.get('result')
        bots = bot_result.get('bots') if isinstance(bot_result, dict) else None
        if not isinstance(bots, list):
            raise PersistentAgentError("Invalid DingTalk group bot list", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        bot_codes = []
        sender_candidates: dict[str, list[str]] = {}
        for bot in bots:
            code = bot.get('robotCode') if isinstance(bot, dict) else None
            if (
                not isinstance(code, str) or not code or len(code) > 512
                or any(char.isspace() or ord(char) < 32 for char in code)
            ):
                raise PersistentAgentError("Invalid DingTalk group bot identity", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
            bot_codes.append(code)
            sender = bot.get('botOpenDingTalkId')
            if (isinstance(sender, str) and sender and len(sender) <= 512
                    and not any(char.isspace() or ord(char) < 32 for char in sender)):
                sender_candidates.setdefault(code, []).append(sender)
        bot_senders = {code: senders[0] for code, senders in sender_candidates.items()
                       if len(senders) == 1 and bot_codes.count(code) == 1
                       and sum(senders[0] in other for other in sender_candidates.values()) == 1}
        if not robot_code or robot_code not in bot_codes:
            raise PersistentAgentError("Employee DingTalk bot is not in this group", code="DINGTALK_CLI_GROUP_BOT_NOT_PRESENT")
        local_boundary = boundary.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(
            sep=' ', timespec='milliseconds',
        )[:-6]
        payload = await self._execute_oauth_command(scope, [
            'chat', 'message', 'list', '--conversation-id', conversation_id,
            '--time', local_boundary, '--direction', 'newer', '--limit', '50',
            '--page-all', '--page-limit', '3', '--max-items', '150',
        ], profile)
        messages = payload.get('messages')
        complete, has_more = payload.get('complete'), payload.get('hasMore')
        if (
            not isinstance(messages, list) or len(messages) > 150
            or not isinstance(complete, bool) or not isinstance(has_more, bool)
        ):
            raise PersistentAgentError("Invalid group history response", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        safe_messages = []
        fields = (
            'messageId', 'openMessageId', 'openConversationId', 'conversationId',
            'senderOpenDingTalkId', 'sender', 'sendType', 'text', 'createTime',
        )
        for message in messages:
            if not isinstance(message, dict):
                raise PersistentAgentError("Invalid group history message", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
            message_id = message.get('messageId') or message.get('openMessageId')
            group_id = message.get('openConversationId') or message.get('conversationId')
            if (
                group_id != conversation_id or not isinstance(message_id, str) or not message_id
                or len(message_id) > 512
                or message.get('openMessageId', message_id) != message_id
            ):
                raise PersistentAgentError("Group history identity mismatch", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
            safe_messages.append({key: message[key] for key in fields if key in message})
        result = payload.get('result')
        cursor = result.get('nextCursor') if isinstance(result, dict) else None
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0):
            raise PersistentAgentError("Invalid group history cursor", code="DINGTALK_CLI_GROUP_HISTORY_INVALID")
        return {
            'readerKey': hashlib.sha256(profile.encode('utf-8')).hexdigest(),
            'messages': safe_messages, 'complete': complete and not has_more,
            'hasMore': has_more, 'nextCursor': cursor, 'botCodes': bot_codes,
            'botSenders': bot_senders,
        }

    @staticmethod
    def _online_node_id(value: str) -> str:
        if isinstance(value, str) and value.startswith('https://'):
            try:
                parsed = urlsplit(value)
            except ValueError as exc:
                raise PersistentAgentError(
                    'Invalid online document URL', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID',
                ) from exc
            if (
                parsed.hostname != 'alidocs.dingtalk.com' or parsed.username or parsed.password
                or parsed.netloc not in {'alidocs.dingtalk.com', 'alidocs.dingtalk.com:443'}
                or not parsed.path.startswith('/i/nodes/')
            ):
                raise PersistentAgentError('Invalid online document URL', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
            value = parsed.path.removeprefix('/i/nodes/').rstrip('/')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
            raise PersistentAgentError('Invalid online document ID', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
        return value

    async def _online_document_profile(self, scope: DingTalkCliScope) -> str:
        if not self.is_available(scope):
            raise PersistentAgentError('DingTalk app is unavailable', code='DINGTALK_CLI_ONLINE_DOC_UNAVAILABLE')
        auth = await self.dingtalk_auth_status(scope)
        if auth.get('status') == 'error':
            raise PersistentAgentError('Unable to check employee authorization', code='DINGTALK_CLI_ONLINE_DOC_UNAVAILABLE')
        if auth.get('status') != 'connected' or not isinstance(auth.get('_profile'), str) or not auth['_profile']:
            raise PersistentAgentError(
                '请先在该 AI 员工的钉钉渠道设置中完成用户授权', code='DINGTALK_CLI_USER_AUTH_REQUIRED',
            )
        return auth['_profile']

    async def _execute_online_document(
        self, scope: DingTalkCliScope, command: list[str], *, profile: str, error_code: str,
        input_text: str | None = None,
    ) -> dict[str, Any]:
        """Consume the shortcut contract without confusing it with atomic CLI output."""
        if tuple(command[:2]) not in {('doc', '+search'), ('doc', '+fetch'), ('doc', '+update')}:
            raise ValueError('unsupported online document command')
        try:
            arguments = [*command, '--profile', profile, '--format', 'json']
            options = {'env': self._oauth_environment(scope), 'cwd': self._oauth_root(scope)}
            if input_text is not None:
                options['input_text'] = input_text
            result = await asyncio.to_thread(self._run_cli, arguments, **options)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PersistentAgentError('钉钉在线文档操作未确认成功，请勿重复写入', code=error_code) from exc
        stdout = result.stdout if isinstance(result.stdout, str) else ''
        if len(stdout.encode('utf-8', errors='replace')) > _MAX_OUTPUT_BYTES:
            raise PersistentAgentError('钉钉在线文档结果超出大小限制', code=error_code)
        payload = self._json_response(stdout)
        if (
            result.returncode != 0 or not isinstance(payload, dict)
            or payload.get('status') != 'success' or payload.get('success') is False
        ):
            raise PersistentAgentError('钉钉在线文档操作未确认成功，请勿重复写入', code=error_code)
        return payload

    async def search_online_documents(
        self, scope: DingTalkCliScope, *, query: str, cursor: str | None = None, limit: int = 10,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str) or not query.strip() or len(query) > 512 or '\x00' in query
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30
            or (cursor is not None and (
                not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or '\x00' in cursor
            ))
        ):
            raise PersistentAgentError('Invalid document search', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
        profile = await self._online_document_profile(scope)
        command = ['doc', '+search', '--query', query, '--extensions', 'adoc', '--limit', str(limit)]
        if cursor is not None:
            command.extend(['--cursor', cursor])
        payload = await self._execute_online_document(
            scope, command, profile=profile, error_code='DINGTALK_CLI_ONLINE_DOC_SEARCH_FAILED',
        )
        documents = payload.get('documents')
        has_more, complete = payload.get('hasMore'), payload.get('complete')
        next_cursor, failures = payload.get('nextCursor'), payload.get('failures', [])
        if (
            not isinstance(documents, list) or len(documents) > limit
            or not isinstance(has_more, bool) or not isinstance(complete, bool)
            or not isinstance(failures, list)
            or (has_more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor))
        ):
            raise PersistentAgentError('Invalid document search result', code='DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID')
        candidates = []
        for item in documents:
            if not isinstance(item, dict) or item.get('docType') != 'adoc' or not isinstance(item.get('name'), str):
                raise PersistentAgentError('Invalid online document candidate', code='DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID')
            try:
                node_id = self._online_node_id(item.get('nodeId'))
            except PersistentAgentError as exc:
                raise PersistentAgentError('Invalid document identity', code='DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID') from exc
            candidates.append({
                'nodeId': node_id, 'name': item['name'], 'docType': 'adoc',
                'url': f'https://alidocs.dingtalk.com/i/nodes/{node_id}',
            })
        return {
            'documents': candidates, 'complete': complete and not has_more and not failures,
            'hasMore': has_more, 'nextCursor': next_cursor,
            'partial': bool(failures), 'failureCount': len(failures),
        }

    async def read_online_document(self, scope: DingTalkCliScope, *, node_id: str) -> dict[str, Any]:
        node_id = self._online_node_id(node_id)
        profile = await self._online_document_profile(scope)
        payload = await self._execute_online_document(
            scope, ['doc', '+fetch', '--node', node_id], profile=profile,
            error_code='DINGTALK_CLI_ONLINE_DOC_READ_FAILED',
        )
        content, target = payload.get('content'), payload.get('target')
        if (
            payload.get('complete') is not True or not isinstance(content, dict) or not isinstance(target, dict)
            or target.get('canonicalId') != node_id or target.get('product') != 'doc'
            or content.get('nodeId') != node_id or content.get('success') is not True
            or not isinstance(content.get('markdown'), str) or not isinstance(content.get('title'), str)
        ):
            raise PersistentAgentError('Invalid document read result', code='DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID')
        return {
            'nodeId': node_id, 'title': content['title'], 'content': content['markdown'],
            'documentType': 'online_doc', 'url': f'https://alidocs.dingtalk.com/i/nodes/{node_id}',
        }

    async def edit_online_document(
        self, scope: DingTalkCliScope, *, node_id: str, edit_mode: str, content: str | None = None,
        old_text: str | None = None, new_text: str | None = None,
    ) -> dict[str, Any]:
        node_id = self._online_node_id(node_id)
        if not isinstance(edit_mode, str):
            raise PersistentAgentError('Invalid document edit mode', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
        command = ['doc', '+update', '--node', node_id]
        input_text = None
        if edit_mode in {'append', 'overwrite'}:
            if (
                not isinstance(content, str) or not content.strip() or len(content) > 100_000
                or old_text is not None or new_text is not None
            ):
                raise PersistentAgentError('Invalid document content', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
            command.extend(['--command', edit_mode, '--content', '-'])
            input_text = content
        elif edit_mode == 'replace_text':
            if (
                content is not None or not isinstance(old_text, str) or not old_text or len(old_text) > 4_000
                or not isinstance(new_text, str) or len(new_text) > 4_000
            ):
                raise PersistentAgentError('Invalid text replacement', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
            command.extend(['--command', 'str_replace', '--old', old_text, '--new', new_text])
        else:
            raise PersistentAgentError('Invalid document edit mode', code='DINGTALK_CLI_ONLINE_DOC_REQUEST_INVALID')
        profile = await self._online_document_profile(scope)
        payload = await self._execute_online_document(
            scope, [*command, '--yes'], profile=profile,
            error_code='DINGTALK_CLI_ONLINE_DOC_EDIT_FAILED', input_text=input_text,
        )
        data = payload.get('data')
        verification = data.get('verification') if isinstance(data, dict) else None
        if (
            payload.get('ok') is not True or payload.get('complete') is not True
            or not isinstance(data, dict) or data.get('nodeId') != node_id
            or data.get('verified') is not True or not isinstance(verification, dict)
            or verification.get('verified') is not True
        ):
            raise PersistentAgentError(
                '文档可能已修改，但回读未完整确认；请先读取现状，不要重复写入',
                code='DINGTALK_CLI_ONLINE_DOC_RESULT_INVALID',
            )
        return {'updated': True, 'nodeId': node_id, 'editMode': edit_mode, 'verified': True}

    async def create_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        title: str,
        content: str = "",
    ) -> dict[str, Any]:
        """Create a real online document and verify its entry in the current group."""
        if not self.is_available(scope):
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        clean_title = title.strip()
        if (
            not clean_title or len(clean_title) > 120
            or any(char in clean_title for char in "\\/:*?\"<>|\r\n\t")
            or clean_title.endswith(".") or not isinstance(content, str) or len(content) > 100_000
        ):
            raise PersistentAgentError("Invalid DingTalk group document title or content", code="BAD_REQUEST")
        auth = await self.dingtalk_auth_status(scope)
        account_id = auth.get("_user_id")
        if auth.get("status") != "connected" or not isinstance(account_id, str) or not account_id:
            raise PersistentAgentError(
                "请先在该 AI 员工的钉钉渠道设置中完成用户授权，再创建在线群文档",
                code="DINGTALK_CLI_USER_AUTH_REQUIRED",
            )
        # The authorized account owns the document; the requester may be another group member.
        user = await self._execute_internal_api(scope, [
            "api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com",
            "--data", json.dumps({"userid": account_id, "language": "zh_CN"}, ensure_ascii=False),
        ])
        user_result = user.get("result") if isinstance(user.get("result"), dict) else user
        union_id = user_result.get("unionid") if isinstance(user_result, dict) else None
        if not isinstance(union_id, str) or not union_id:
            raise PersistentAgentError("无法解析已授权钉钉账号的 unionId", code="DINGTALK_CLI_IDENTITY_UNAVAILABLE")
        group_space = await self._execute_internal_api(scope, [
            "api", "POST", "/v1.0/convFile/conversations/spaces/query",
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps({"openConversationId": conversation_id}),
        ])
        space = group_space.get("space") if isinstance(group_space.get("space"), dict) else {}
        space_id = space.get("spaceId")
        if not isinstance(space_id, str) or not space_id:
            raise PersistentAgentError("钉钉未返回当前群的文件空间", code="DINGTALK_CLI_GROUP_SPACE_UNAVAILABLE")
        # Creating in the group workspace avoids posting a file message as the OAuth user.
        command = ["doc", "create", "--name", clean_title, "--workspace", space_id, "--format", "json"]
        if content:
            command.extend(["--content", content])
        profile = auth.get("_profile")
        if isinstance(profile, str) and profile:
            command.extend(["--profile", profile])
        created = await self._execute_oauth_command(scope, command)
        node_id = created.get("nodeId")
        if not isinstance(node_id, str) or not node_id or len(node_id) > 512:
            raise PersistentAgentError(
                "钉钉创建响应缺少文档 ID；请先检查当前群文件列表，不要重复创建",
                code="DINGTALK_CLI_CREATE_RESULT_INVALID",
            )
        doc_url = f"https://alidocs.dingtalk.com/i/nodes/{quote(node_id, safe='')}"
        try:
            info_command = ["drive", "info", "--node", node_id, "--format", "json"]
            if isinstance(profile, str) and profile:
                info_command.extend(["--profile", profile])
            info = await self._execute_oauth_command(scope, info_command)
            detail = info.get("result") if isinstance(info.get("result"), dict) else {}
            if (
                detail.get("nodeId") != node_id or detail.get("name") != clean_title
                or detail.get("workspaceId") != space_id
                or str(detail.get("type") or detail.get("contentType")).upper() != "ALIDOC"
            ):
                raise PersistentAgentError("钉钉未确认在线文档的标题、类型或目标群空间", code="DINGTALK_CLI_CREATE_RESULT_INVALID")
            source = await self._execute_internal_api(scope, [
                "api", "GET", f"/v2.0/doc/dentries/{quote(node_id, safe='')}/queryDentryId",
                "--params", json.dumps({"operatorId": union_id}),
            ])
            if (
                source.get("dentryUuid") != node_id or source.get("spaceId") != space_id
                or not isinstance(source.get("dentryId"), str) or not source["dentryId"]
            ):
                raise PersistentAgentError("钉钉未确认在线文档存储于当前群", code="DINGTALK_CLI_CREATE_RESULT_INVALID")
            file_id = source["dentryId"]
            visible = False
            next_token = None
            seen_tokens: set[str] = set()
            for _ in range(20):
                params = {"unionId": union_id, "parentId": "0", "maxResults": 50}
                if next_token:
                    params["nextToken"] = next_token
                listing = await self._execute_internal_api(scope, [
                    "api", "GET", f"/v1.0/storage/spaces/{quote(space_id, safe='')}/dentries",
                    "--params", json.dumps(params),
                ])
                entries = listing.get("dentries")
                if not isinstance(entries, list):
                    break
                if any(isinstance(item, dict) and item.get("id") == file_id for item in entries):
                    visible = True
                    break
                next_token = listing.get("nextToken")
                if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                    break
                seen_tokens.add(next_token)
            if not visible:
                raise PersistentAgentError("未能在当前群文件列表核验文档入口", code="DINGTALK_CLI_CREATE_RESULT_INVALID")
        except PersistentAgentError as exc:
            raise PersistentAgentError(
                f"在线文档已创建：{doc_url}；尚未确认放入当前群文件列表。{exc}。不要重新创建同名文档。",
                code=exc.code,
            ) from exc
        except Exception as exc:
            raise PersistentAgentError(
                f"在线文档已创建：{doc_url}；群文件核验中断，请先检查该文档，不要重新创建同名文档。",
                code="DINGTALK_CLI_CREATE_GROUP_DOC_FAILED",
            ) from exc
        return {
            "created": True, "title": clean_title, "documentType": "online_doc",
            "dentryId": file_id, "dentryUuid": node_id,
            "documentNodeId": node_id, "url": doc_url,
        }

    async def update_calendar_event(
        self, scope: DingTalkCliScope, *, event_id: str, title: str,
    ) -> dict[str, Any]:
        """Rename one existing user-calendar event and verify the preserved details."""
        if (
            not isinstance(event_id, str) or not event_id or len(event_id) > 512
            or event_id.startswith("-") or any(char.isspace() or ord(char) < 32 for char in event_id)
            or not isinstance(title, str) or not title.strip() or len(title.strip()) > 120
        ):
            raise PersistentAgentError("日程 ID 或新标题无效", code="BAD_REQUEST")
        auth = await self.dingtalk_auth_status(scope)
        if auth.get("status") != "connected":
            raise PersistentAgentError(
                "请先在该员工的钉钉渠道设置中完成用户授权，再修改日程",
                code="DINGTALK_CLI_USER_AUTH_REQUIRED",
            )
        profile = auth.get("_profile")
        query = ["calendar", "event", "get", "--id", event_id]
        response = await self._execute_oauth_command(scope, query, profile)
        before = response.get("result") if isinstance(response.get("result"), dict) else response
        if before.get("id") != event_id or str(before.get("status", "")).lower() in {"cancelled", "canceled"}:
            raise PersistentAgentError("未找到匹配的有效日程，未执行修改", code="DINGTALK_CLI_CALENDAR_UPDATE_RESULT_INVALID")
        clean_title = title.strip()
        if before.get("summary") == clean_title:
            return {"updated": True, "changed": False, "eventId": event_id, "summary": clean_title}
        await self._execute_oauth_command(scope, [
            "calendar", "event", "update", "--id", event_id, "--title", clean_title,
        ], profile)
        try:
            response = await self._execute_oauth_command(scope, query, profile)
            after = response.get("result") if isinstance(response.get("result"), dict) else response
            protected = (
                "start", "end", "attendees", "reminders", "organizer", "meetingRooms",
                "isAllDay", "recurrence", "freeBusy", "description", "onlineMeetingInfo",
            )
            if after.get("id") != event_id or after.get("summary") != clean_title:
                raise PersistentAgentError("原日程的新标题未通过读回核验", code="DINGTALK_CLI_CALENDAR_UPDATE_RESULT_INVALID")
            if any(before.get(key) != after.get(key) for key in protected):
                raise PersistentAgentError("日程其他信息发生变化，请核对原日程", code="DINGTALK_CLI_CALENDAR_UPDATE_RESULT_INVALID")
        except PersistentAgentError as exc:
            raise PersistentAgentError(
                f"日程标题修改已提交（eventId {event_id}），但核验未完成：{exc}；不要重建日程或重复提交修改。",
                code="DINGTALK_CLI_CALENDAR_UPDATE_RESULT_INVALID",
            ) from exc
        return {
            "updated": True, "changed": True, "eventId": event_id, "summary": clean_title,
            "start": after.get("start"), "end": after.get("end"),
        }

    async def create_calendar_event(
        self,
        scope: DingTalkCliScope,
        *,
        sender_staff_id: str,
        title: str,
        start_at: str,
        end_at: str,
        time_zone: str,
        description: str = "",
        reminder_minutes: int | None = None,
        attendee_staff_ids: list[str] | None = None,
        create_video_meeting: bool = False,
        include_group_members: bool = False,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an event on the trusted sender's primary calendar using the shared app token."""
        if not self.is_available(scope):
            raise PersistentAgentError(
                "No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED"
            )
        if attendee_staff_ids is None:
            attendee_staff_ids = []
        try:
            zone = ZoneInfo(time_zone)
            start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("event times must have a UTC offset")
            if end <= start:
                raise ValueError("event end must be after its start")
            # The explicit offset must describe the same local wall time as timeZone.
            if start.astimezone(zone).replace(tzinfo=None) != start.replace(tzinfo=None):
                raise ValueError("event start does not match timeZone")
            if end.astimezone(zone).replace(tzinfo=None) != end.replace(tzinfo=None):
                raise ValueError("event end does not match timeZone")
        except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError) as exc:
            raise PersistentAgentError(
                "日程时间无效：请提供带时区偏移的开始和结束时间，并使用有效的 IANA 时区。",
                code="DINGTALK_CLI_CALENDAR_REQUEST_INVALID",
            ) from exc
        if (
            not isinstance(sender_staff_id, str) or not sender_staff_id.strip()
            or not isinstance(title, str) or not title.strip() or len(title.strip()) > 200
            or not isinstance(description, str) or len(description) > 4000
            or isinstance(reminder_minutes, bool)
            or (reminder_minutes is not None and (not isinstance(reminder_minutes, int) or not 0 <= reminder_minutes <= 10080))
            or not isinstance(create_video_meeting, bool)
            or not isinstance(include_group_members, bool)
            or not isinstance(attendee_staff_ids, list) or len(attendee_staff_ids) > 100
            or any(not isinstance(item, str) or not item.strip() for item in attendee_staff_ids)
        ):
            raise PersistentAgentError("日程信息无效", code="DINGTALK_CLI_CALENDAR_REQUEST_INVALID")

        if include_group_members:
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise PersistentAgentError(
                    "添加群成员需要来自可信群消息的当前群 ID，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            return await self.create_group_calendar_event(
                scope,
                conversation_id=conversation_id,
                sender_staff_id=sender_staff_id.strip(),
                summary=title,
                start_datetime=start_at,
                end_datetime=end_at,
                time_zone=time_zone,
                description=description,
                reminder_minutes=reminder_minutes,
                create_video_meeting=create_video_meeting,
                extra_attendee_staff_ids=attendee_staff_ids,
            )

        async def resolve_union_id(staff_id: str) -> str:
            user = await self._execute_internal_api(scope, [
                "api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com",
                "--data", json.dumps({"userid": staff_id, "language": "zh_CN"}, ensure_ascii=False),
            ])
            user_result = user.get("result") if isinstance(user.get("result"), dict) else user
            union_id = user_result.get("unionid") if isinstance(user_result, dict) else None
            if not isinstance(union_id, str) or not union_id:
                raise PersistentAgentError(
                    "无法将日程创建者或参与人映射为钉钉 unionId；请检查通讯录用户详情读取权限。",
                    code="DINGTALK_CLI_IDENTITY_UNAVAILABLE",
                )
            return union_id

        # Calendar userId must be a unionId. For group requests this is the trusted
        # sender of the accepted callback, never an ID inferred from the prompt.
        try:
            organizer_union_id = await resolve_union_id(sender_staff_id.strip())
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError(
                "调用 POST /topapi/v2/user/get 解析日程创建者失败；日程创建请求尚未提交。",
                code="DINGTALK_CLI_IDENTITY_LOOKUP_FAILED",
            ) from exc
        attendees = []
        try:
            for staff_id in attendee_staff_ids:
                attendees.append({"id": await resolve_union_id(staff_id.strip())})
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError(
                "调用 POST /topapi/v2/user/get 解析日程参与人失败；日程创建请求尚未提交。",
                code="DINGTALK_CLI_IDENTITY_LOOKUP_FAILED",
            ) from exc

        event: dict[str, Any] = {
            "summary": title.strip(),
            "start": {"dateTime": start_at, "timeZone": time_zone},
            "end": {"dateTime": end_at, "timeZone": time_zone},
            "isAllDay": False,
        }
        if description.strip():
            event["description"] = description.strip()
        if reminder_minutes is not None:
            event["reminders"] = [{"method": "dingtalk", "minutes": reminder_minutes}]
        if attendees:
            event["attendees"] = attendees
        if create_video_meeting:
            event["onlineMeetingInfo"] = {"type": "dingtalk"}

        endpoint = f"/v1.0/calendar/users/{quote(organizer_union_id, safe='')}/calendars/primary/events"
        try:
            response = await self._execute_internal_api(scope, [
                "api", "POST", endpoint, "--data", json.dumps(event, ensure_ascii=False),
            ])
        except PersistentAgentError:
            raise
        except Exception as exc:
            raise PersistentAgentError(
                f"调用 POST {endpoint} 失败，是否已创建无法确认；请先检查日历，不要自动重试。",
                code="DINGTALK_CLI_CALENDAR_CREATE_FAILED",
            ) from exc
        created = response.get("result") if isinstance(response.get("result"), dict) else response
        event_id = created.get("id") or created.get("eventId") if isinstance(created, dict) else None
        if isinstance(event_id, (int, float)) and not isinstance(event_id, bool):
            event_id = str(event_id)
        if not isinstance(event_id, str) or not event_id.strip():
            raise PersistentAgentError(
                "钉钉未返回日程 ID，无法确认创建结果；请先检查日历，不要自动重试。",
                code="DINGTALK_CLI_CALENDAR_RESULT_INVALID",
            )
        result: dict[str, Any] = {
            "created": True,
            "eventId": event_id,
            "title": title.strip(),
            "startAt": start_at,
            "endAt": end_at,
            "timeZone": time_zone,
        }
        meeting_info = created.get("onlineMeetingInfo") if isinstance(created, dict) else None
        meeting_url = meeting_info.get("url") if isinstance(meeting_info, dict) else None
        if isinstance(meeting_url, str) and meeting_url.startswith("https://"):
            result["meetingUrl"] = meeting_url
        return result

    async def upload_group_file(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        file_path: str,
    ) -> dict[str, Any]:
        """Upload one shared output file to the current group's DingTalk file space."""
        if not self.is_available(scope):
            raise PersistentAgentError(
                "No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED"
            )
        if not all(isinstance(value, str) and value.strip() for value in (conversation_id, sender_staff_id)):
            raise PersistentAgentError("Invalid DingTalk group file upload route", code="BAD_REQUEST")
        file_name, content = self._read_group_output_file(scope, file_path)
        user = await self._execute_internal_api(scope, [
            "api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com",
            "--data", json.dumps({"userid": sender_staff_id, "language": "zh_CN"}, ensure_ascii=False),
        ])
        user_result = user.get("result") if isinstance(user.get("result"), dict) else user
        union_id = user_result.get("unionid") if isinstance(user_result, dict) else None
        if not isinstance(union_id, str) or not union_id:
            raise PersistentAgentError(
                "无法将当前群消息发送者映射为钉钉 unionId", code="DINGTALK_CLI_IDENTITY_UNAVAILABLE"
            )

        group_space = await self._execute_internal_api(scope, [
            "api", "POST", "/v1.0/convFile/conversations/spaces/query",
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps({"openConversationId": conversation_id}),
        ])
        space = group_space.get("space") if isinstance(group_space.get("space"), dict) else {}
        space_id = space.get("spaceId")
        if isinstance(space_id, int) and not isinstance(space_id, bool):
            space_id = str(space_id)
        if not isinstance(space_id, str) or not space_id:
            raise PersistentAgentError("钉钉未返回当前群的文件空间", code="DINGTALK_CLI_GROUP_SPACE_UNAVAILABLE")

        root_uuid = space.get("rootDentryUuid") or space.get("rootFolderId")
        if isinstance(root_uuid, str):
            root_uuid = root_uuid.strip()
        if not isinstance(root_uuid, str) or not root_uuid:
            root_uuid = None
        if root_uuid:
            encoded_root_uuid = quote(root_uuid, safe="")
            upload_endpoint = f"/v2.0/storage/spaces/files/{encoded_root_uuid}/uploadInfos/query"
            upload_data = {
                "protocol": "HEADER_SIGNATURE",
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {"size": len(content), "name": file_name},
                },
            }
            commit_endpoint = f"/v2.0/storage/spaces/files/{encoded_root_uuid}/commit"
            commit_data = {
                "name": file_name,
                "option": {"size": len(content), "conflictStrategy": "AUTO_RENAME"},
            }
        else:
            upload_endpoint = f"/v1.0/storage/spaces/{quote(space_id, safe='')}/files/uploadInfos/query"
            upload_data = {
                "protocol": "HEADER_SIGNATURE",
                "multipart": False,
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {
                        "md5": hashlib.md5(content).hexdigest(),
                        "size": len(content),
                        "parentId": "0",
                        "name": file_name,
                    },
                },
            }
            commit_endpoint = f"/v1.0/storage/spaces/{quote(space_id, safe='')}/files/commit"
            commit_data = {
                "name": file_name,
                "parentId": "0",
                "option": {"size": len(content), "conflictStrategy": "AUTO_RENAME"},
            }
        upload_info = await self._execute_internal_api(scope, [
            "api", "POST", upload_endpoint,
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps(upload_data, ensure_ascii=False),
        ])
        upload_key = upload_info.get("uploadKey")
        signature = upload_info.get("headerSignatureInfo")
        urls = signature.get("resourceUrls") if isinstance(signature, dict) else None
        headers = signature.get("headers") if isinstance(signature, dict) else None
        if (
            not isinstance(upload_key, str) or not upload_key
            or not isinstance(urls, list) or not urls or not isinstance(urls[0], str)
            or not isinstance(headers, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items())
        ):
            raise PersistentAgentError(
                "钉钉未返回可用的群文件上传凭证", code="DINGTALK_CLI_UPLOAD_INFO_INVALID"
            )
        try:
            await self._signed_upload(urls[0], headers, content)
        except Exception as exc:
            raise PersistentAgentError(
                "上传文件内容到钉钉群文件空间失败；请勿重试本次操作", code="DINGTALK_CLI_UPLOAD_FAILED"
            ) from exc

        commit_data["uploadKey"] = upload_key
        committed = await self._execute_internal_api(scope, [
            "api", "POST", commit_endpoint,
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps(commit_data, ensure_ascii=False),
        ])
        dentry = committed.get("dentry") if isinstance(committed.get("dentry"), dict) else {}
        dentry_id = dentry.get("id")
        dentry_uuid = dentry.get("uuid")
        if (
            isinstance(dentry_id, bool) or dentry_id is None or not str(dentry_id).strip()
            or not isinstance(dentry_uuid, str) or not dentry_uuid
        ):
            raise PersistentAgentError(
                "钉钉未确认群文件上传结果；请勿自动重试", code="DINGTALK_CLI_UPLOAD_RESULT_INVALID"
            )
        return {
            "uploaded": True,
            "fileName": file_name,
            "size": len(content),
            "dentryId": str(dentry_id),
            "dentryUuid": dentry_uuid,
            "spaceId": space_id,
        }

    async def edit_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        dentry_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Replace the contents of a DOCX file after verifying it belongs to this group."""
        if not self.is_available(scope):
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        if (
            not isinstance(dentry_id, str) or not dentry_id.strip() or len(dentry_id) > 256
            or not isinstance(content, str) or len(content) > 100_000
        ):
            raise PersistentAgentError("Invalid DingTalk group document edit request", code="BAD_REQUEST")

        user = await self._execute_internal_api(scope, [
            "api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com",
            "--data", json.dumps({"userid": sender_staff_id, "language": "zh_CN"}, ensure_ascii=False),
        ])
        user_result = user.get("result") if isinstance(user.get("result"), dict) else user
        union_id = user_result.get("unionid") if isinstance(user_result, dict) else None
        if not isinstance(union_id, str) or not union_id:
            raise PersistentAgentError(
                "无法将当前群消息发送者映射为钉钉 unionId", code="DINGTALK_CLI_IDENTITY_UNAVAILABLE"
            )

        group_space = await self._execute_internal_api(scope, [
            "api", "POST", "/v1.0/convFile/conversations/spaces/query",
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps({"openConversationId": conversation_id}),
        ])
        space = group_space.get("space") if isinstance(group_space.get("space"), dict) else {}
        space_id = space.get("spaceId")
        if not isinstance(space_id, str) or not space_id:
            raise PersistentAgentError("钉钉未返回当前群的文件空间", code="DINGTALK_CLI_GROUP_SPACE_UNAVAILABLE")

        safe_space_id = quote(space_id, safe="")
        safe_dentry_id = quote(dentry_id.strip(), safe="")
        dentry_result = await self._execute_internal_api(scope, [
            "api", "POST",
            f"/v1.0/storage/spaces/{safe_space_id}/dentries/{safe_dentry_id}/query",
            "--params", json.dumps({"unionId": union_id}), "--data", "{}",
        ])
        dentry = dentry_result.get("dentry") if isinstance(dentry_result.get("dentry"), dict) else {}
        if (
            dentry.get("id") != dentry_id
            or dentry.get("spaceId") != space_id
            or str(dentry.get("type", "")).upper() != "FILE"
        ):
            raise PersistentAgentError(
                "找不到当前群中的目标文件，未执行写入", code="DINGTALK_CLI_GROUP_DOC_NOT_FOUND"
            )
        dentry_uuid = dentry.get("uuid")
        parent_id = dentry.get("parentId")
        file_name = dentry.get("name")
        extension = dentry.get("extension")
        storage_driver = dentry.get("storageDriver")
        if (
            not isinstance(dentry_uuid, str) or not dentry_uuid
            or parent_id is None or not isinstance(file_name, str) or not file_name
            or str(extension or "").lower() != "docx"
            or str(storage_driver or "").upper() != "DINGTALK"
            or dentry.get("version") is None
        ):
            raise PersistentAgentError(
                "当前群文件不是可安全覆写的 DOCX 文件，未执行写入",
                code="DINGTALK_CLI_DOCUMENT_TYPE_UNSUPPORTED",
            )

        document = self._docx_bytes(content)
        upload_data = {
            "protocol": "HEADER_SIGNATURE",
            "multipart": False,
            "option": {
                "storageDriver": "DINGTALK",
                "preCheckParam": {
                    "md5": hashlib.md5(document).hexdigest(),
                    "size": len(document),
                    "parentId": str(parent_id),
                    "name": file_name,
                },
            },
        }
        upload_info = await self._execute_internal_api(scope, [
            "api", "POST", f"/v1.0/storage/spaces/{safe_space_id}/files/uploadInfos/query",
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps(upload_data, ensure_ascii=False),
        ])
        upload_key = upload_info.get("uploadKey")
        signature = upload_info.get("headerSignatureInfo")
        urls = signature.get("resourceUrls") if isinstance(signature, dict) else None
        headers = signature.get("headers") if isinstance(signature, dict) else None
        if (
            not isinstance(upload_key, str) or not upload_key or not isinstance(urls, list) or not urls
            or not isinstance(urls[0], str) or not isinstance(headers, dict)
        ):
            raise PersistentAgentError("钉钉未返回可用的文档上传凭证", code="DINGTALK_CLI_UPLOAD_INFO_INVALID")
        try:
            await self._signed_upload(urls[0], headers, document)
        except Exception as exc:
            raise PersistentAgentError(
                "上传文档内容到钉钉存储失败；原文档尚未变更", code="DINGTALK_CLI_UPLOAD_FAILED"
            ) from exc

        latest_result = await self._execute_internal_api(scope, [
            "api", "POST",
            f"/v1.0/storage/spaces/{safe_space_id}/dentries/{safe_dentry_id}/query",
            "--params", json.dumps({"unionId": union_id}), "--data", "{}",
        ])
        latest = latest_result.get("dentry") if isinstance(latest_result.get("dentry"), dict) else {}
        if (
            latest.get("id") != dentry_id or latest.get("spaceId") != space_id
            or latest.get("uuid") != dentry_uuid or latest.get("parentId") != parent_id
            or latest.get("name") != file_name or latest.get("version") != dentry.get("version")
        ):
            raise PersistentAgentError(
                "目标文档在上传期间已变化，未提交覆盖；请重新确认文档版本",
                code="DINGTALK_CLI_DOCUMENT_CHANGED",
            )

        committed = await self._execute_internal_api(scope, [
            "api", "POST", f"/v1.0/storage/spaces/{safe_space_id}/files/commit",
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps({
                "uploadKey": upload_key,
                "name": file_name,
                "parentId": str(parent_id),
                "option": {"size": len(document), "conflictStrategy": "OVERWRITE"},
            }, ensure_ascii=False),
        ])
        updated = committed.get("dentry") if isinstance(committed.get("dentry"), dict) else {}
        if updated.get("id") != dentry_id or updated.get("uuid") != dentry_uuid:
            raise PersistentAgentError(
                "钉钉未确认更新的是原群文档；请勿自动重试", code="DINGTALK_CLI_UPDATE_RESULT_INVALID"
            )
        result: dict[str, Any] = {
            "updated": True,
            "dentryId": dentry_id,
            "dentryUuid": dentry_uuid,
            "documentType": "docx_file",
        }
        if updated.get("version") is not None:
            result["version"] = updated["version"]
        return result

    async def _execute_internal_api(self, scope: DingTalkCliScope, argv: list[str]) -> dict[str, Any]:
        """Keep signed upload URLs intact inside a composite request; never return them to the model."""
        checked = self._validate_argv(argv)
        if self._binding(scope) is None:
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        return await asyncio.to_thread(self._execute, scope, checked, redact_sensitive=False)

    @staticmethod
    def _load_config() -> Mapping[str, Any]:
        from jiuwenswarm.common.config import get_config
        return get_config()

    @staticmethod
    def _scope_digest(scope: DingTalkCliScope) -> str:
        return hashlib.sha256(f"{scope.namespace_id}\0{scope.agent_id}".encode()).hexdigest()[:20]

    def _scope_root(self, scope: DingTalkCliScope) -> Path:
        path = self._root / "employees" / self._scope_digest(scope)
        if not path.resolve().is_relative_to(self._root):
            raise ValueError("DingTalk CLI profile escapes its runtime directory")
        return path

    def _npm_cache_dir(self) -> Path:
        return self._root / "npm-cache" / _CLI_VERSION

    def _repair_npx_package_cache(self) -> None:
        cache_root = self._npm_cache_dir().resolve()
        npx_root = cache_root / "_npx"
        if npx_root.is_symlink() or not npx_root.is_dir():
            return
        for modules_dir in npx_root.glob("*/node_modules"):
            if modules_dir.is_symlink() or not modules_dir.is_dir():
                continue
            for package_dir in modules_dir.iterdir():
                if package_dir.name != "dingtalk-workspace-cli" and not package_dir.name.startswith(
                    ".dingtalk-workspace-cli-"
                ):
                    continue
                if package_dir.is_symlink():
                    package_dir.unlink()
                elif package_dir.is_dir() and package_dir.resolve().is_relative_to(cache_root):
                    shutil.rmtree(package_dir)

    def _cached_dws_binary(self) -> Path | None:
        binary_name = "dws.exe" if os.name == "nt" else "dws"
        matches: list[Path] = []
        for manifest in self._root.rglob("package.json"):
            if manifest.parent.name != "dingtalk-workspace-cli":
                continue
            try:
                package = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(package, dict) or package.get("version") != _CLI_VERSION:
                continue
            binary = manifest.parent / "vendor" / binary_name
            if not binary.is_file() or not binary.resolve().is_relative_to(self._root):
                continue
            if os.name != "nt" and not os.access(binary, os.X_OK):
                continue
            matches.append(binary)
        return max(matches, key=lambda item: item.stat().st_mtime) if matches else None

    def _profile_root(self, scope: DingTalkCliScope, binding: tuple[str, str]) -> Path:
        revision = hashlib.sha256(f"{binding[0]}\0{binding[1]}".encode()).hexdigest()[:16]
        return self._scope_root(scope) / revision

    def _oauth_root(self, scope: DingTalkCliScope) -> Path:
        binding = self._binding(scope)
        if binding is None:
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        revision = hashlib.sha256(f"{binding[0]}\0{binding[1]}".encode()).hexdigest()[:16]
        root = self._scope_root(scope) / "user-oauth" / revision
        private_dir(root)
        return root

    def _oauth_environment(self, scope: DingTalkCliScope) -> dict[str, str]:
        binding = self._binding(scope)
        if binding is None:
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        # The organization must register/allow this application as a CLI Agent.
        # User OAuth and App Token share the app identity, never their token stores.
        return self._environment_for_root(self._oauth_root(scope), binding)

    def _oauth_paths(self, scope: DingTalkCliScope, operation_id: str) -> tuple[Path, Path, Path]:
        root = self._oauth_root(scope) / "auth"
        return root / f"{operation_id}.log", root / f"{operation_id}.pid", root / f"{operation_id}.exit"

    def _oauth_current(self, scope: DingTalkCliScope) -> Path:
        return self._oauth_root(scope) / "auth" / "current"

    def _oauth_command(self, *args: str) -> list[str]:
        cached_binary = self._cached_dws_binary() if self._dws_path is None else None
        if cached_binary is not None:
            return [str(cached_binary), *args]
        return self._command(*args)

    @staticmethod
    def _oauth_device_details(output: str) -> tuple[str | None, str | None]:
        url_match = re.search(r"https://(?:login|open|auth)[^\s\x1b<>]+", output, re.I)
        url = url_match.group(0).rstrip(".,，。") if url_match else None
        code = None
        if url:
            try:
                code_values = parse_qs(urlsplit(url).query).get("user_code") or []
                if code_values and re.fullmatch(r"[A-Z0-9-]{4,16}", code_values[0], re.I):
                    code = code_values[0]
            except ValueError:
                pass
        if code is None:
            code_match = re.search(
                r"(?:user[_ ]?code|用户码|授权码|code)\s*[:：=]?\s*([A-Z0-9-]{4,16})",
                output,
                re.I,
            )
            code = code_match.group(1) if code_match else None
        return url, code

    async def dingtalk_auth_status(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._dingtalk_auth_status, scope)

    @staticmethod
    def _oauth_process_running(pid_path: Path) -> bool:
        import psutil

        try:
            pid = int(pid_path.read_text(encoding='utf-8'))
            if pid <= 0:
                return False
            process = psutil.Process(pid)
            return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                    and abs(process.create_time() - pid_path.stat().st_mtime) <= 5)
        except (OSError, ValueError, psutil.Error):
            return False

    def _dingtalk_auth_status(self, scope: DingTalkCliScope, *, recheck_finished: bool = True) -> dict[str, Any]:
        root = self._oauth_root(scope)
        try:
            result = self._run_cli(
                ["auth", "status", "--format", "json"], env=self._oauth_environment(scope), cwd=root,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "error", "cli_version": _CLI_VERSION}
        payload = self._json_response(result.stdout if isinstance(result.stdout, str) else "")
        if result.returncode != 0 or not isinstance(payload, dict) or payload.get("success") is False:
            return {"status": "error", "cli_version": _CLI_VERSION}
        data = payload.get("data")
        connected = result.returncode == 0 and isinstance(payload, dict) and (
            payload.get("authenticated") is True
            or (isinstance(data, dict) and data.get("logged_in") is True)
        )
        if connected:
            try:
                profile_result = self._run_cli(
                    ["profile", "list", "--format", "json"], env=self._oauth_environment(scope), cwd=root,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {"status": "error", "cli_version": _CLI_VERSION}
            if profile_result.returncode != 0:
                return {"status": "error", "cli_version": _CLI_VERSION}
            profile_payload = self._json_response(profile_result.stdout if isinstance(profile_result.stdout, str) else "")
            profile_data = profile_payload.get("data") if isinstance(profile_payload, dict) else None
            profiles = profile_payload.get("profiles") if isinstance(profile_payload, dict) else None
            if not isinstance(profiles, list):
                profiles = profile_data if isinstance(profile_data, list) else (
                    profile_data.get("profiles") if isinstance(profile_data, dict) else None
                )
            candidates = [item for item in profiles if isinstance(item, dict)] if isinstance(profiles, list) else []
            current = [item for item in candidates if item.get("isCurrent") or item.get("current")]
            if len(current) == 1:
                selected = current[0]
            elif not current and len(candidates) == 1:
                selected = candidates[0]
            else:
                return {"status": "error", "cli_version": _CLI_VERSION}
            status = {"status": "connected", "cli_version": _CLI_VERSION}
            if selected:
                user_id = selected.get("userId")
                if isinstance(user_id, str) and user_id and len(user_id) <= 200:
                    status["_user_id"] = user_id
                profile = selected.get("profile")
                if isinstance(profile, str) and profile and len(profile) <= 512:
                    status["_profile"] = profile
                account = selected.get("userName") or selected.get("username") or selected.get("userId")
                organization = selected.get("corpName") or selected.get("corp_name")
                if isinstance(account, str) and len(account) <= 200:
                    status["account"] = account
                if isinstance(organization, str) and len(organization) <= 200:
                    status["organization"] = organization
            if status.get('_profile') and (
                not status.get('account') or status.get('account') == status.get('_user_id')
                or not status.get('organization') or status.get('organization') == selected.get('corpId')
            ):
                try:
                    identity_result = self._run_cli(
                        ['--profile', status['_profile'], 'contact', '+me', '--format', 'json',
                         '--fields', 'name,userId,org', '--timeout', '5'],
                        env=self._oauth_environment(scope), cwd=root,
                    )
                    identity_payload = self._json_response(identity_result.stdout)
                    identity = identity_payload.get('data') if isinstance(identity_payload, dict) else None
                    if (identity_result.returncode == 0 and isinstance(identity_payload, dict)
                            and identity_payload.get('ok') is True
                            and identity_payload.get('outcome') == 'success' and isinstance(identity, dict)
                            and isinstance(identity.get('userId'), str) and identity['userId'].strip()
                            and (not selected.get('userId') or selected['userId'] == identity['userId'])):
                        # These names are display metadata, not group-reading authorization.
                        for source, target in [('name', 'account'), ('org', 'organization')]:
                            value = identity.get(source)
                            if isinstance(value, str) and value.strip() and len(value) <= 200:
                                status[target] = value.strip()
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                operation_id = self._oauth_current(scope).read_text(encoding="utf-8").strip()
            except OSError:
                operation_id = ""
            if _DINGTALK_AUTH_OPERATION.fullmatch(operation_id) and operation_id.startswith(f"{self._scope_digest(scope)}."):
                self._clear_dingtalk_auth(scope, operation_id)
            return status
        current_path = self._oauth_current(scope)
        try:
            operation_id = current_path.read_text(encoding="utf-8").strip()
        except OSError:
            operation_id = ""
        digest = self._scope_digest(scope)
        if _DINGTALK_AUTH_OPERATION.fullmatch(operation_id) and operation_id.startswith(f"{digest}."):
            log_path, pid_path, exit_path = self._oauth_paths(scope, operation_id)
            try:
                output = log_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                output = ''
            # The CLI can consume the code and fail its later organization check.
            # Keep the safe failure observable across page reloads, never the old code.
            failure = self._json_response(output)
            error = failure.get('error') if isinstance(failure, dict) else None
            reason = error.get('message', '') if isinstance(error, dict) else ''
            details = output + (reason if isinstance(reason, str) else '')
            if '暂无 CLI 数据访问权限' in details or '未授权您通过 CLI' in details:
                return {'status': 'error', 'cli_version': _CLI_VERSION, 'error_code': 'org_access_denied'}
            try:
                expired = time.time() - pid_path.stat().st_mtime >= _DINGTALK_AUTH_TIMEOUT_SECONDS
            except OSError:
                expired = False
            if expired:
                if not exit_path.exists() and self._oauth_process_running(pid_path):
                    try:
                        os.kill(int(pid_path.read_text(encoding='utf-8')), signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
                    exit_path.write_text('expired', encoding='utf-8')
                return {'status': 'error', 'cli_version': _CLI_VERSION, 'error_code': 'authorization_expired'}
            if error or exit_path.exists() or not self._oauth_process_running(pid_path):
                # Login may finish after the earlier CLI status snapshot was read.
                # Verify persisted credentials once before making failure terminal.
                if recheck_finished:
                    completed = self._dingtalk_auth_status(scope, recheck_finished=False)
                    if completed.get('status') == 'connected':
                        return completed
                return {'status': 'error', 'cli_version': _CLI_VERSION, 'error_code': 'authorization_failed'}
            details = self._oauth_device_details(output)
            return {"status": "authorizing", "cli_version": _CLI_VERSION, "operation_id": operation_id,
                    "auth_url": details[0], "user_code": details[1]}
        return {"status": "not_connected", "cli_version": _CLI_VERSION}

    async def dingtalk_auth_begin(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._locked_oauth_operation, scope, self._dingtalk_auth_begin)

    def _dingtalk_auth_begin(self, scope: DingTalkCliScope) -> dict[str, Any]:
        status = self._dingtalk_auth_status(scope)
        if status["status"] == "connected":
            return status
        if status["status"] == "authorizing":
            return status
        try:
            previous = self._oauth_current(scope).read_text(encoding='utf-8').strip()
        except OSError:
            previous = ''
        if (_DINGTALK_AUTH_OPERATION.fullmatch(previous)
                and previous.startswith(f'{self._scope_digest(scope)}.')):
            self._clear_dingtalk_auth(scope, previous)
        digest = self._scope_digest(scope)
        operation_id = f"{digest}.{uuid.uuid4().hex}"
        log_path, pid_path, exit_path = self._oauth_paths(scope, operation_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._oauth_current(scope).write_text(operation_id, encoding="utf-8")
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen(
                    self._oauth_command("auth", "login", "--device"), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, env=self._oauth_environment(scope),
                    cwd=str(self._oauth_root(scope)), start_new_session=True,
                )
            except OSError as exc:
                self._clear_dingtalk_auth(scope, operation_id)
                raise PersistentAgentError("钉钉用户授权进程启动失败", code="DINGTALK_CLI_AUTH_START_FAILED") from exc
        pid_path.write_text(str(process.pid), encoding="utf-8")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                exit_path.write_text(str(process.returncode), encoding="utf-8")
            try:
                output = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                output = ""
            url, code = self._oauth_device_details(output)
            if url and code and process.poll() is None:
                return {"status": "authorizing", "cli_version": _CLI_VERSION,
                        "operation_id": operation_id, "auth_url": url, "user_code": code}
            if process.poll() is not None:
                self._clear_dingtalk_auth(scope, operation_id)
                payload = self._json_response(output)
                error = payload.get('error') if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    message = '钉钉用户授权失败，请重试或检查钉钉账号与授权客户端配置。'
                    if '[900103]' in str(error.get('message', '')):
                        message = '钉钉拒绝用户授权：应用不存在（900103）。请检查授权客户端配置。'
                    raise PersistentAgentError(message, code='DINGTALK_CLI_AUTH_LOGIN_FAILED')
                raise PersistentAgentError("钉钉 CLI 未返回可识别的设备授权码", code="DINGTALK_CLI_AUTH_RESPONSE_INVALID")
            time.sleep(0.2)
        self._cancel_dingtalk_auth(scope, operation_id)
        raise PersistentAgentError("等待钉钉设备授权码超时", code="DINGTALK_CLI_AUTH_TIMEOUT")

    async def dingtalk_auth_cancel(self, scope: DingTalkCliScope, operation_id: str) -> dict[str, Any]:
        await asyncio.to_thread(self._locked_oauth_operation, scope, self._cancel_dingtalk_auth, operation_id)
        return await self.dingtalk_auth_status(scope)

    def _cancel_dingtalk_auth(self, scope: DingTalkCliScope, operation_id: str) -> None:
        if (
            not isinstance(operation_id, str) or not _DINGTALK_AUTH_OPERATION.fullmatch(operation_id)
            or not operation_id.startswith(f"{self._scope_digest(scope)}.")
        ):
            raise PersistentAgentError("invalid DingTalk authorization operation", code="BAD_REQUEST")
        log_path, pid_path, exit_path = self._oauth_paths(scope, operation_id)
        try:
            if self._oauth_process_running(pid_path):
                pid = int(pid_path.read_text(encoding="utf-8"))
                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        self._clear_dingtalk_auth(scope, operation_id)

    def _clear_dingtalk_auth(self, scope: DingTalkCliScope, operation_id: str) -> None:
        log_path, pid_path, exit_path = self._oauth_paths(scope, operation_id)
        for path in (log_path, pid_path, exit_path):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            current = self._oauth_current(scope)
            if current.read_text(encoding="utf-8").strip() == operation_id:
                current.unlink()
        except OSError:
            pass

    def _locked_oauth_operation(self, scope: DingTalkCliScope, operation: Callable, *args: Any) -> Any:
        lock = self._oauth_locks.setdefault(self._scope_digest(scope), threading.RLock())
        with lock:
            return operation(scope, *args)

    def _dingtalk_auth_disconnect(self, scope: DingTalkCliScope) -> dict[str, Any]:
        root = self._oauth_root(scope)
        try:
            operation_id = self._oauth_current(scope).read_text(encoding="utf-8").strip()
        except OSError:
            operation_id = ""
        if _DINGTALK_AUTH_OPERATION.fullmatch(operation_id) and operation_id.startswith(f"{self._scope_digest(scope)}."):
            self._cancel_dingtalk_auth(scope, operation_id)
        shutil.rmtree(root, ignore_errors=True)
        return {"status": "not_connected", "cli_version": _CLI_VERSION}

    async def dingtalk_auth_disconnect(self, scope: DingTalkCliScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._locked_oauth_operation, scope, self._dingtalk_auth_disconnect)

    def _binding(self, scope: DingTalkCliScope) -> tuple[str, str] | None:
        if scope.namespace_id != "local":
            return None
        config = self._config()
        channels = config.get("channels") if isinstance(config, Mapping) else None
        raw = channels.get("dingtalk") if isinstance(channels, Mapping) else None
        apps = dingtalk_apps(raw)
        defaults = [app for app in apps if app["is_default"]]
        if len(defaults) > 1:
            return None
        if scope.app_id is not None:
            matches = [app for app in apps if app["client_id"] == scope.app_id]
            if len(matches) != 1:
                return None
            app = matches[0]
            if app["persistent_agent_id"] and app["persistent_agent_id"] != scope.agent_id:
                return None
        elif defaults:
            app = defaults[0]
        elif len(apps) == 1:
            # Team mode uses one shared DingTalk application. Older channel configs may
            # have that sole app attached to its creator instead of marking it default.
            app = apps[0]
        else:
            employee_apps = [app for app in apps if app["persistent_agent_id"] == scope.agent_id]
            if len(employee_apps) != 1:
                return None
            app = employee_apps[0]
        if (
            not app["enabled"]
            or not app["client_id"]
            or not app["client_secret"]
            or not allow_from_valid(app)
        ):
            return None
        if sum(item["client_id"] == app["client_id"] for item in apps) != 1:
            return None
        return app["client_id"], app["client_secret"]

    def is_available(self, scope: DingTalkCliScope) -> bool:
        try:
            return self._binding(scope) is not None and (self._dws_path is not None or self._npx_path is not None)
        except (OSError, TypeError, ValueError, KeyError):
            return False

    def environment(self, scope: DingTalkCliScope) -> dict[str, str]:
        binding = self._binding(scope)
        if binding is None:
            return {key: value for key, value in os.environ.items() if not key.startswith("DWS_")}
        if self._dws_path is None and self._npx_path is None:
            raise RuntimeError("Node.js npx is unavailable for DingTalk CLI")
        return self._environment_for_root(self._profile_root(scope, binding), binding)

    def _environment_for_root(self, root: Path, binding: tuple[str, str]) -> dict[str, str]:
        """Share installation policy while keeping app and user credentials isolated."""
        home = root / "home"
        config_dir = root / "dws"
        cache_dir = config_dir / "cache"
        shared_npm_cache = self._npm_cache_dir()
        for directory in (home, config_dir, cache_dir, shared_npm_cache, home / ".config", home / ".cache"):
            private_dir(directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("DWS_")}
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "DWS_CLIENT_ID": binding[0],
            "DWS_CLIENT_SECRET": binding[1],
            "DWS_CONFIG_DIR": str(config_dir),
            "DWS_CACHE_DIR": str(cache_dir),
            "DWS_DISABLE_KEYCHAIN": "1",
            "DWS_TRUSTED_DOMAINS": "*.dingtalk.com",
            "DWS_ALLOW_HTTP_ENDPOINTS": "0",
            "DWS_NO_SKILLS": "1",
            "npm_config_cache": str(shared_npm_cache),
            # DWS's postinstall extracts the CLI binary; its optional global Skill setup is disabled above.
            "npm_config_ignore_scripts": "false",
            "NO_COLOR": "1",
        })
        registry = os.environ.get("JIUWENMATE_DINGTALK_CLI_NPM_REGISTRY", "").strip()
        if registry:
            env["npm_config_registry"] = registry
        return env

    def _command(self, *args: str) -> list[str]:
        if self._dws_path is not None:
            return [str(self._dws_path), *args]
        if self._npx_path is None:
            raise RuntimeError("Node.js npx is unavailable for DingTalk CLI")
        prefix = [str(self._npx_path)]
        if self._npx_path.suffix.lower() in {".cmd", ".bat"}:
            node = shutil.which("node")
            npm_cli = self._npx_path.parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
            if not node or not npm_cli.is_file():
                raise RuntimeError("Node.js npm CLI could not be resolved on this host")
            prefix = [node, str(npm_cli)]
        return [*prefix, "--yes", f"--package={_CLI_PACKAGE}", "--", "dws", *args]

    def _run_cli(
        self, argv: list[str], *, env: dict[str, str], cwd: Path, input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        cached_binary = self._cached_dws_binary() if self._dws_path is None else None
        uses_npx = self._dws_path is None and cached_binary is None

        def run(command: list[str]) -> subprocess.CompletedProcess:
            return self._runner(
                command, env=env, input_text=input_text,
                timeout=_CLI_REQUEST_TIMEOUT_SECONDS, cwd=str(cwd)
            )

        if uses_npx:
            lock_path = self._npm_cache_dir() / ".dingtalk-cli.lock"
            with _npm_cache_lock(lock_path, timeout=_NPM_CACHE_LOCK_TIMEOUT_SECONDS):
                cached_binary = self._cached_dws_binary()
                if cached_binary is not None:
                    return run([str(cached_binary), *argv])
                command = self._command(*argv)
                result = run(command)
                stderr = result.stderr if isinstance(result.stderr, str) else ""
                if result.returncode and re.search(
                    r"npm\s+(?:error\s+code\s+ENOTEMPTY|ERR!.*\bENOTEMPTY\b)", stderr, re.I
                ):
                    self._repair_npx_package_cache()
                    return run(command)
                return result

        command = [str(self._dws_path), *argv] if self._dws_path is not None else [str(cached_binary), *argv]
        return run(command)

    def _execute(self, scope: DingTalkCliScope, argv: list[str], *, redact_sensitive: bool = True) -> dict[str, Any]:
        argv = self._validate_argv(argv)
        if self._binding(scope) is None:
            raise RuntimeError("no usable default or employee-bound DingTalk application is configured")
        env = self.environment(scope)
        try:
            result = self._run_cli(argv, env=env, cwd=self._scope_root(scope))
        except subprocess.TimeoutExpired as exc:
            raise PersistentAgentError(
                "DingTalk CLI request timed out; stop retrying DingTalk API calls for this request",
                code="DINGTALK_CLI_TIMEOUT",
            ) from exc
        except OSError as exc:
            raise RuntimeError("DingTalk CLI could not be started; check Node.js/npm and network access") from exc
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            raise RuntimeError("DingTalk API response is too large")
        safe_stdout = stdout
        if redact_sensitive and env.get("DWS_CLIENT_SECRET"):
            safe_stdout = safe_stdout.replace(env["DWS_CLIENT_SECRET"], "[redacted]")
        response = self._json_response(safe_stdout)
        response_error = False
        if response is not None:
            response_error = (
                isinstance(response.get("error"), dict)
                or response.get("success") is False
                or (
                    response.get("errcode") is not None
                    and response.get("errcode") not in (0, "0")
                )
            )
        if response is not None and result.returncode == 0 and not response_error:
            return self._redact_sensitive_fields(response) if redact_sensitive else response
        if result.returncode or response_error:
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            if re.search(r"npm\s+(?:error\s+code\s+ENOTEMPTY|ERR!.*\bENOTEMPTY\b)", stderr, re.I):
                raise PersistentAgentError(
                    "DingTalk CLI package cache is busy; stop retrying DingTalk API calls for this request",
                    code="DINGTALK_CLI_CACHE_BUSY",
                )
            cli_error = response if response_error else self._json_response(stderr)
            error_detail = cli_error.get("error") if isinstance(cli_error, dict) else None
            cli_message = error_detail.get("message") if isinstance(error_detail, dict) else None
            if not isinstance(cli_message, str) and isinstance(cli_error, dict):
                cli_message = cli_error.get("message") or cli_error.get("errmsg")
            error_text = f"{stderr} {cli_message if isinstance(cli_message, str) else ''}"
            if re.search(r"\bEOF\b|connection reset|TLS handshake timeout|network is unreachable", error_text, re.I):
                raise PersistentAgentError(
                    "钉钉 API 连接中断，未确认调用结果；请先核对已有结果，不要重复执行写操作",
                    code="DINGTALK_CLI_API_ERROR",
                )
            if isinstance(cli_message, str):
                missing_scope = re.search(
                    r"requiredScopes=\[([A-Za-z][A-Za-z0-9_.-]{0,127})\]",
                    cli_message,
                ) or re.search(
                    r"(?:权限|permission)[^\[]{0,80}\[([A-Za-z][A-Za-z0-9_.-]{0,127})\]",
                    cli_message,
                    re.I,
                )
                if missing_scope:
                    method = argv[1].upper() if len(argv) > 1 else "API"
                    endpoint = _safe_api_path(argv[2]) if len(argv) > 2 else "unknown endpoint"
                    scope = missing_scope.group(1)
                    if (
                        endpoint == "/v1.0/im/innerGroups/members/query"
                        and scope == "Chat.OrgInnerGroup.Read"
                    ):
                        raise PersistentAgentError(
                            f"钉钉拒绝 {method} {endpoint}（DINGTALK_CLI_API_FORBIDDEN），未创建日程。"
                            "该响应中的权限标识无法在开发者平台核实，错误本身不足以判断为权限问题。",
                            code="DINGTALK_CLI_API_FORBIDDEN",
                        )
                    purpose = _PERMISSION_PURPOSES.get(scope)
                    purpose_text = f"（{purpose}）" if purpose else ""
                    apply_match = re.search(
                        r"https://open-dev\.dingtalk\.com/appscope/apply(?:\?[^\s,\]}]+)?",
                        cli_message,
                    )
                    apply_link = f"\n钉钉返回的权限申请入口：{apply_match.group(0)}" if apply_match else ""
                    raise PersistentAgentError(
                        f"钉钉拒绝 {method} {endpoint}：当前应用缺少权限标识 {scope}{purpose_text}。"
                        f"这是 API 返回的机器标识，钉钉控制台显示名称可能不同；请先为当前应用开通该接口权限。{apply_link}",
                        code="DINGTALK_CLI_API_FORBIDDEN",
                    )
            if re.search(
                r"\bHTTP(?:/\d(?:\.\d)?)?\s+403\b|\b403\s+Forbidden\b|\bstatus(?:Code)?\D{0,10}403\b",
                error_text,
                re.I,
            ):
                method = argv[1].upper() if len(argv) > 1 else "API"
                endpoint = _safe_api_path(argv[2]) if len(argv) > 2 else "unknown endpoint"
                message = f"DingTalk rejected {method} {endpoint} with HTTP 403; the configured app is not authorized for this API"
                if "Forbidden.AccessDenied.AccessTokenPermissionDenied" in error_text:
                    scope_match = re.search(
                        r"(?:permission|权限)[^\[]{0,80}\[([A-Za-z][A-Za-z0-9_.-]{0,127})\]",
                        stderr,
                        re.I,
                    )
                    if scope_match:
                        scope = scope_match.group(1)
                        purpose = _PERMISSION_PURPOSES.get(scope)
                        purpose_text = f"（{purpose}）" if purpose else ""
                        message = (
                            f"钉钉拒绝 {method} {endpoint}：当前应用缺少权限标识 {scope}{purpose_text}。"
                            "这是 API 返回的机器标识，钉钉控制台显示名称可能不同；请按此 API 路径核对应用的接口权限后再重试。"
                        )
                raise PersistentAgentError(
                    message,
                    code="DINGTALK_CLI_API_FORBIDDEN",
                )
            method = argv[1].upper() if len(argv) > 1 else "API"
            endpoint = _safe_api_path(argv[2]) if len(argv) > 2 else "unknown endpoint"
            upstream_code = None
            http_status = None
            if isinstance(cli_message, str):
                code_match = re.search(r"\berrcode\s*[:=]\s*([A-Za-z0-9_.-]{1,80})", cli_message, re.I)
                status_match = re.search(r"\bHTTP\s+(\d{3})\b", cli_message, re.I)
                upstream_code = code_match.group(1) if code_match else None
                http_status = status_match.group(1) if status_match else None
            if upstream_code is None and isinstance(error_detail, dict):
                raw_code = error_detail.get("code")
                if isinstance(raw_code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", raw_code):
                    upstream_code = raw_code
            if upstream_code is None and isinstance(cli_error, dict):
                raw_code = cli_error.get("errcode")
                if isinstance(raw_code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(raw_code)):
                    upstream_code = str(raw_code)
                raw_status = cli_error.get("statusCode")
                if isinstance(raw_status, int) and 100 <= raw_status <= 599:
                    http_status = str(raw_status)
            diagnostics = []
            if upstream_code is not None:
                diagnostics.append(f"errcode={upstream_code}")
            if http_status is not None:
                diagnostics.append(f"HTTP {http_status}")
            detail = f"（{'，'.join(diagnostics)}）" if diagnostics else "（DWS 未返回结构化错误码）"
            raise PersistentAgentError(
                f"钉钉 API {method} {endpoint} 调用失败{detail}。该错误不能单独证明权限不足或接口不支持；"
                "请依据上游错误码排查，未确认前不要重复调用。",
                code="DINGTALK_CLI_API_ERROR",
            )
        raise RuntimeError("DingTalk app-token API returned an invalid response")

    @staticmethod
    def _is_allowed_api_url(value: str, *, base_url: bool = False) -> bool:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"api.dingtalk.com", "oapi.dingtalk.com"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
        ):
            return False
        if base_url and (parsed.path not in {"", "/"} or parsed.query):
            return False
        return True

    @staticmethod
    def _validate_argv(argv: list[str]) -> list[str]:
        if not isinstance(argv, list) or not argv or len(argv) > 128:
            raise RuntimeError("invalid DingTalk CLI arguments")
        if any(not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192 for item in argv):
            raise RuntimeError("invalid DingTalk CLI arguments")
        if sum(len(item.encode("utf-8")) for item in argv) > 32_768:
            raise RuntimeError("DingTalk CLI arguments are too large")
        if argv[0].lower() != "api":
            raise RuntimeError("only the DWS app-token API command is available")
        if len(argv) < 3 or argv[1].upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise RuntimeError("DWS app-token API requires an HTTP method and DingTalk API path")
        target = argv[2]
        forbidden = {
            "--client-id", "--client-secret", "--config", "--config-dir", "--token", "--app-secret",
            "--allow-http", "--trusted-domains",
        }
        if any(item.split("=", 1)[0].lower() in forbidden for item in argv):
            raise RuntimeError("DingTalk CLI credential and profile overrides are not allowed")
        if target.startswith("/"):
            if target.startswith("//"):
                raise RuntimeError("DingTalk API path must not include an external host")
        elif not LocalDingTalkCliRuntime._is_allowed_api_url(target):
            raise RuntimeError(
                "DingTalk CLI only supports HTTPS API hosts api.dingtalk.com and oapi.dingtalk.com"
            )
        for index, item in enumerate(argv):
            if item == "--base-url":
                if index + 1 >= len(argv):
                    raise RuntimeError("DingTalk API base URL is missing")
                base_url = argv[index + 1]
            elif item.startswith("--base-url="):
                base_url = item.split("=", 1)[1]
            else:
                continue
            if not LocalDingTalkCliRuntime._is_allowed_api_url(base_url, base_url=True):
                raise RuntimeError("DingTalk CLI only supports HTTPS API hosts api.dingtalk.com and oapi.dingtalk.com")
        return list(argv)

    @staticmethod
    def _json_response(stdout: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stdout):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    @classmethod
    def _redact_sensitive_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[redacted]" if isinstance(key, str) and _SENSITIVE_FIELD.search(key)
                else cls._redact_sensitive_fields(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact_sensitive_fields(item) for item in value]
        return value

    async def execute(self, scope: DingTalkCliScope, argv: list[str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._execute, scope, argv)

    async def remove_runtime(self, scope: DingTalkCliScope) -> None:
        await asyncio.to_thread(shutil.rmtree, self._scope_root(scope), True)


def create_provider() -> LocalDingTalkCliRuntime:
    """Load the channel config and prepare a private DWS cache under JiuwenMate's data root."""
    from jiuwenswarm.server.jiuwenmate.service import default_data_dir

    configured_dws = os.environ.get("JIUWENMATE_DWS_BIN", "").strip()
    dws_path = Path(configured_dws).expanduser() if configured_dws else None
    root = Path(os.environ.get("JIUWENMATE_DINGTALK_CLI_HOME") or default_data_dir() / "dingtalk_cli")
    return LocalDingTalkCliRuntime(root=root, dws_path=dws_path)
