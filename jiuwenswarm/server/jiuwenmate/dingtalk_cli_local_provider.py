"""Run the official DWS app-token API with the shared default DingTalk app."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping
from urllib.parse import quote, urlsplit
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
_SENSITIVE_FIELD = re.compile(
    r"(?:secret|password|authorization|(?:access|refresh|app)[_-]?token)", re.I
)
_PERMISSION_PURPOSES = {
    "qyapi_get_member": "通讯录用户详情读取权限",
    "Contact.User.Read": "企业用户读取权限",
    "ConvFile.Space.Read": "群文件空间读权限",
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
        self._group_members_app_token_forbidden: set[str] = set()

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
        if binding[0] in self._group_members_app_token_forbidden:
            return await self._query_group_staff_ids_as_user(
                scope, conversation_id=conversation_id, sender_staff_id=sender_staff_id
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
                    self._group_members_app_token_forbidden.add(binding[0])
                    return await self._query_group_staff_ids_as_user(
                        scope, conversation_id=conversation_id, sender_staff_id=sender_staff_id
                    )
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
                    f"共享 DWS 账号读取群成员失败：{detail}；未创建日程",
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
                "共享 DWS 账号的群成员命令未返回成员列表，未创建日程",
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
                "共享 DWS 账号的群成员结果缺少有效分页信息，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        return members, has_more, next_cursor

    def _dws_user_environment(self) -> dict[str, str]:
        """Reuse the host's shared DWS user session; never start or prompt for login."""
        env = dict(os.environ)
        for name in (
            "DWS_CLIENT_ID", "DWS_CLIENT_SECRET", "DWS_APP_KEY", "DWS_APP_SECRET",
            "DWS_CONFIG_DIR", "DWS_CACHE_DIR", "DWS_HOME", "DWS_DISABLE_KEYCHAIN",
        ):
            env.pop(name, None)
        env.setdefault("HOME", str(Path.home()))
        env.setdefault("USERPROFILE", env["HOME"])
        env["NO_COLOR"] = "1"
        env["DWS_NO_SKILLS"] = "1"
        env["npm_config_cache"] = str(self._npm_cache_dir())
        return env

    async def _query_group_staff_ids_as_user(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
    ) -> list[str]:
        """Read a full group roster through DWS's supported signed-in-user command."""
        staff_ids: list[str] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = "0"
        page_limit = (_MAX_GROUP_CALENDAR_ATTENDEES + _GROUP_MEMBER_PAGE_SIZE - 1) // _GROUP_MEMBER_PAGE_SIZE
        for _ in range(page_limit):
            if cursor in seen_cursors:
                raise PersistentAgentError(
                    "DWS 群成员命令返回了重复分页游标，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            seen_cursors.add(cursor)
            argv = [
                "chat", "group", "members", "--id", conversation_id,
                "--cursor", cursor, "--format", "json",
            ]
            try:
                result = await asyncio.to_thread(
                    self._run_cli,
                    argv,
                    env=self._dws_user_environment(),
                    cwd=Path.home(),
                )
            except PersistentAgentError:
                raise
            except Exception as exc:
                raise PersistentAgentError(
                    f"共享 DWS 账号读取群成员失败（{type(exc).__name__}）；未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                ) from exc

            stdout = result.stdout if isinstance(result.stdout, str) else ""
            if len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
                raise PersistentAgentError(
                    "DWS 群成员响应超过大小限制，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            response = self._json_response(stdout)
            if result.returncode != 0 or response is None:
                stderr = result.stderr if isinstance(result.stderr, str) else ""
                error = self._json_response(stderr)
                error_detail = error.get("error") if isinstance(error, Mapping) else None
                message = error_detail.get("message") if isinstance(error_detail, Mapping) else None
                if not isinstance(message, str):
                    message = "共享 DWS 登录态不可用或该账号无法读取目标群"
                raise PersistentAgentError(
                    f"{message}；未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )

            page_members, has_more, next_cursor = self._dws_user_group_member_page(response)
            for member in page_members:
                if isinstance(member, str):
                    staff_id = member
                elif isinstance(member, Mapping):
                    staff_id = member.get("userId") or member.get("userid") or member.get("user_id")
                else:
                    staff_id = None
                if not isinstance(staff_id, str) or not staff_id:
                    raise PersistentAgentError(
                        "DWS 群成员结果缺少 userId，未创建日程",
                        code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                    )
                if staff_id not in seen_ids:
                    staff_ids.append(staff_id)
                    seen_ids.add(staff_id)
            if len(staff_ids) > _MAX_GROUP_CALENDAR_ATTENDEES:
                raise PersistentAgentError(
                    f"当前群成员超过日程参与人上限 {_MAX_GROUP_CALENDAR_ATTENDEES}，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            if not has_more:
                break
            if next_cursor is None or next_cursor in seen_cursors:
                raise PersistentAgentError(
                    "DWS 群成员命令返回了无效分页游标，未创建日程",
                    code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
                )
            cursor = next_cursor
        else:
            raise PersistentAgentError(
                "DWS 群成员命令超过分页上限，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )

        if sender_staff_id not in seen_ids:
            raise PersistentAgentError(
                "共享 DWS 账号返回的群成员名单不包含当前消息发送者，未创建日程",
                code="DINGTALK_CLI_GROUP_MEMBERS_UNAVAILABLE",
            )
        return staff_ids

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

    async def create_group_document(
        self,
        scope: DingTalkCliScope,
        *,
        conversation_id: str,
        sender_staff_id: str,
        title: str,
        content: str = "",
    ) -> dict[str, Any]:
        """Create an online document directly in the current group's file space."""
        if not self.is_available(scope):
            raise PersistentAgentError("No enabled DingTalk application is available", code="DINGTALK_CLI_APP_NOT_CONFIGURED")
        clean_title = title.strip()
        if (
            not clean_title
            or len(clean_title) > 120
            or any(char in clean_title for char in "\\/:*?\"<>|\r\n")
            or "\t" in clean_title
            or clean_title.endswith(".")
            or not isinstance(content, str)
            or len(content) > 100_000
        ):
            raise PersistentAgentError("Invalid DingTalk group document title or content", code="BAD_REQUEST")
        title_base = clean_title[:-5] if clean_title.lower().endswith(".docx") else clean_title
        file_name = clean_title if clean_title.lower().endswith(".docx") else f"{clean_title}.docx"
        document = self._docx_bytes(content)

        user = await self._execute_internal_api(scope, [
            "api", "POST", "/topapi/v2/user/get", "--base-url", "https://oapi.dingtalk.com",
            "--data", json.dumps({"userid": sender_staff_id, "language": "zh_CN"}, ensure_ascii=False),
        ])
        user_result = user.get("result") if isinstance(user.get("result"), dict) else user
        union_id = user_result.get("unionid") if isinstance(user_result, dict) else None
        if not isinstance(union_id, str) or not union_id:
            errcode = user.get("errcode")
            detail = "; 请为钉钉应用开通通讯录用户详情读取权限" if errcode not in (None, 0, "0") else ""
            raise PersistentAgentError(
                f"无法将当前群消息发送者映射为钉钉 unionId{detail}", code="DINGTALK_CLI_IDENTITY_UNAVAILABLE"
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

        root_uuid = space.get("rootDentryUuid") or space.get("rootFolderId")
        if isinstance(root_uuid, str):
            root_uuid = root_uuid.strip()
        if not isinstance(root_uuid, str) or not root_uuid:
            root_uuid = None
        if isinstance(root_uuid, str) and root_uuid.strip():
            encoded_root_uuid = quote(root_uuid, safe="")
            upload_endpoint = f"/v2.0/storage/spaces/files/{encoded_root_uuid}/uploadInfos/query"
            upload_data = {
                "protocol": "HEADER_SIGNATURE",
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {"size": len(document), "name": file_name},
                },
            }
            upload_args = [
                "api", "POST", upload_endpoint,
                "--params", json.dumps({"unionId": union_id}),
                "--data", json.dumps(upload_data, ensure_ascii=False),
            ]
        else:
            # Group-space lookup currently guarantees spaceId but may omit the root UUID.
            # The spaceId upload flow still creates a group-visible DOCX file; it cannot
            # request online-DOC conversion because the legacy commit API has no such field.
            upload_endpoint = f"/v1.0/storage/spaces/{space_id}/files/uploadInfos/query"
            upload_data = {
                "protocol": "HEADER_SIGNATURE",
                "multipart": False,
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {
                        "md5": hashlib.md5(document).hexdigest(),
                        "size": len(document),
                        "parentId": "0",
                        "name": file_name,
                    },
                },
            }
            upload_args = [
                "api", "POST", upload_endpoint,
                "--params", json.dumps({"unionId": union_id}),
                "--data", json.dumps(upload_data, ensure_ascii=False),
            ]
        upload_info = await self._execute_internal_api(scope, upload_args)
        upload_key = upload_info.get("uploadKey")
        signature = upload_info.get("headerSignatureInfo")
        urls = signature.get("resourceUrls") if isinstance(signature, dict) else None
        headers = signature.get("headers") if isinstance(signature, dict) else None
        if not isinstance(upload_key, str) or not upload_key or not urls or not isinstance(urls[0], str) or not isinstance(headers, dict):
            raise PersistentAgentError("钉钉未返回可用的群文档上传凭证", code="DINGTALK_CLI_UPLOAD_INFO_INVALID")
        try:
            await self._signed_upload(urls[0], headers, document)
        except Exception as exc:
            raise PersistentAgentError(
                "上传文档内容到钉钉存储失败；文档尚未创建",
                code="DINGTALK_CLI_UPLOAD_FAILED",
            ) from exc

        if isinstance(root_uuid, str) and root_uuid.strip():
            commit_endpoint = f"/v2.0/storage/spaces/files/{quote(root_uuid, safe='')}/commit"
            commit_data = {
                "uploadKey": upload_key,
                "name": file_name,
                "option": {
                    "size": len(document),
                    "conflictStrategy": "AUTO_RENAME",
                    "convertToOnlineDoc": True,
                    "convertToOnlineDocTargetDocumentType": "DOC",
                },
            }
            document_type = "online_doc"
        else:
            commit_endpoint = f"/v1.0/storage/spaces/{space_id}/files/commit"
            commit_data = {
                "uploadKey": upload_key,
                "name": file_name,
                "parentId": "0",
                "option": {"size": len(document), "conflictStrategy": "AUTO_RENAME"},
            }
            document_type = "docx_file"
        committed = await self._execute_internal_api(scope, [
            "api", "POST", commit_endpoint,
            "--params", json.dumps({"unionId": union_id}),
            "--data", json.dumps(commit_data, ensure_ascii=False),
        ])
        dentry = committed.get("dentry") if isinstance(committed.get("dentry"), dict) else {}
        dentry_id = dentry.get("id")
        dentry_uuid = dentry.get("uuid")
        if not isinstance(dentry_id, str) or not dentry_id or not isinstance(dentry_uuid, str) or not dentry_uuid:
            raise PersistentAgentError("钉钉未确认群文档创建结果", code="DINGTALK_CLI_CREATE_RESULT_INVALID")
        result: dict[str, Any] = {
            "created": True,
            "title": title_base,
            "documentType": document_type,
            "dentryId": dentry_id,
            "dentryUuid": dentry_uuid,
        }
        try:
            open_info = await self._execute_internal_api(scope, [
                "api", "POST",
                f"/v1.0/storage/spaces/{quote(space_id, safe='')}/dentries/{quote(dentry_id, safe='')}/openInfos/query",
                "--params", json.dumps({"unionId": union_id}),
                "--data", "{}",
            ])
            candidate_url = open_info.get("url")
            if isinstance(candidate_url, str):
                parsed_url = urlsplit(candidate_url)
                if parsed_url.scheme == "https" and (parsed_url.hostname or "").lower().endswith(".dingtalk.com"):
                    result["url"] = candidate_url
        except Exception:
            # The upload is committed already. Report creation success and avoid a retry
            # that could create a duplicate just because DingTalk did not return a link.
            pass
        if "url" not in result:
            result["linkUnavailable"] = True
        return result

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
        root = self._profile_root(scope, binding)
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

    def _run_cli(self, argv: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
        cached_binary = self._cached_dws_binary() if self._dws_path is None else None
        uses_npx = self._dws_path is None and cached_binary is None

        def run(command: list[str]) -> subprocess.CompletedProcess:
            return self._runner(
                command, env=env, input_text=None,
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
