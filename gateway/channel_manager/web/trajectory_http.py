# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway HTTP read API for persisted single-Agent trajectory records.

The SQLite reader and ``trajectory_ui`` settings live in the AgentServer.
These routes keep the browser contract and forward each read as a
``trajectory.*`` request. An archive, or any body over 2 MiB, is pulled in
chunks and only then written to the client, so a failed query is still JSON.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import SplitResult, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openjiuwen_protocol.message import ReqMethod

from gateway.common.mode_matrix import (
    canonicalize_mode_text,
    is_single_agent_mode,
    is_team_mode,
)
from gateway.common.protocol_ids import is_valid_session_id
from gateway.common.security.ws_origin import (
    get_allowed_origin_hosts,
    is_allowed_browser_origin,
    is_origin_check_enabled,
)

if TYPE_CHECKING:
    from gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

TRAJECTORY_API_PREFIX = "/api/trajectory"
# The one entry of an archive zip: the store's archive lines, one JSON object
# per line, in the order the store yields them.
TRAJECTORY_ARCHIVE_ENTRY_NAME = "trajectory.jsonl"
# Reassembled archives this small stay in memory; a larger one moves to a file.
_ARCHIVE_SPOOL_MAX_BYTES = 16 * 1024 * 1024
_ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_MAX_SQLITE_INTEGER = (1 << 63) - 1
# Frames are far smaller than records, so a catch-up page carries more of
# them: a reader returning after a disconnect closes the gap in few
# round trips instead of many.
_MAX_FRAME_PAGE = 2000
# A chain hash is a sha256 in hex.
_SEQUENCE_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SEQUENCE_REQUEST = 200

_MAX_INTEGER_QUERY_CHARS = len(str(_MAX_SQLITE_INTEGER))
_MAX_CURSOR_LENGTH = 512
# A blob pull terminates on the peer's ``done`` flag; these guards keep a
# misbehaving peer (old AgentServer, broken blob read) from hanging the HTTP
# request in a no-progress loop or streaming an unbounded "archive".
_MAX_BLOB_CHUNKS = 4096

SessionMetadataLoader = Callable[[str, "str | None"], Awaitable[Mapping[str, Any]]]
TrajectoryFetch = Callable[
    [ReqMethod, dict[str, Any], str | None],
    Awaitable[tuple[bool, dict[str, Any]]],
]
_UNAVAILABLE_CODES = frozenset(
    {
        "SERVICE_UNAVAILABLE",
        "RUNTIME_WARMING",
        "RUNTIME_DRAINING",
        "RUNTIME_FAILED",
        "RUNTIME_QUEUE_FULL",
        "REQUEST_CANCELLED",
    },
)
_TIMEOUT_CODE = "AGENT_SERVER_TIMEOUT"


def error_status(code: str, reported_status: Any = None) -> int:
    """Return the HTTP status carried on an AgentServer error payload.

    Trajectory errors include ``status``. Transport failures that never reach
    the reader are mapped from ``code``. Anything else is 500.
    """
    if isinstance(reported_status, int) and not isinstance(reported_status, bool) and 100 <= reported_status <= 599:
        return reported_status
    if code in _UNAVAILABLE_CODES:
        return 503
    if code == _TIMEOUT_CODE:
        return 504
    logger.warning("[trajectory] error payload has no usable status, using 500: code=%s", code)
    return 500


class TrajectoryHttpService:
    """Validated HTTP response layer over an AgentServer trajectory read."""

    def __init__(
        self,
        *,
        metadata_loader: SessionMetadataLoader | None = None,
        fetch: TrajectoryFetch,
    ) -> None:
        """Bind the session gate and the AgentServer caller.

        Args:
            metadata_loader: Session metadata source for the mode gate.
            fetch: Sends one ``trajectory.*`` request and returns ``(ok, payload)``.
        """
        self._metadata_loader = metadata_loader or _load_session_metadata
        self._fetch = fetch

    async def list_subjects(
        self,
        session_id: str,
        *,
        after_revision: int,
        user_id: str | None = None,
    ) -> Response:
        """List the execution subjects that own a chain in one session."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        if after_revision < 0:
            return _error_response("after_revision must be >= 0", "BAD_REQUEST", 400)
        if after_revision > _MAX_SQLITE_INTEGER:
            return _error_response("after_revision is too large", "BAD_REQUEST", 400)
        return await self._json_query(
            ReqMethod.TRAJECTORY_SUBJECTS_LIST,
            {"session_id": session_id, "after_revision": after_revision},
            user_id=user_id,
        )

    async def export_archive(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> Response:
        """Export one session's trajectory as a zipped JSONL archive.

        The AgentServer builds the zip before the first byte is returned.
        This method then pulls the chunks and only starts the download once
        the whole archive is in hand, so a failed query is still JSON.

        Args:
            session_id: Session to export.
            user_id: Caller identity forwarded when the request has one.

        Returns:
            An ``application/zip`` download holding one ``trajectory.jsonl``
            entry, or a JSON error response.
        """
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        ok, payload = await self._call(
            ReqMethod.TRAJECTORY_ARCHIVE_OPEN,
            {"session_id": session_id},
            user_id,
        )
        if not ok:
            return _failure(payload)
        if not payload.get("chunked"):
            return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
        export_id = str(payload.get("export_id") or "")
        spool = tempfile.SpooledTemporaryFile(max_size=_ARCHIVE_SPOOL_MAX_BYTES)
        try:
            await self._copy_blob(export_id, spool, user_id)
            size = spool.seek(0, io.SEEK_END)
            spool.seek(0)
        except _QueryFailed as exc:
            spool.close()
            return exc.response
        except Exception:
            spool.close()
            logger.exception("Trajectory archive query failed: session_id=%s", session_id)
            return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
        return StreamingResponse(
            _iter_spooled_chunks(spool),
            status_code=200,
            media_type="application/zip",
            headers={
                **_NO_STORE_HEADERS,
                "Content-Length": str(size),
                "Content-Disposition": (
                    f'attachment; filename="trajectory-{session_id}.trajectory.zip"'
                ),
            },
        )

    async def get_session_usage(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> Response:
        """Return session-complete request usage partitioned by execution subject."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        return await self._json_query(
            ReqMethod.TRAJECTORY_USAGE_GET,
            {"session_id": session_id},
            user_id=user_id,
        )

    async def get_checkpoints(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> Response:
        """Return what retention left for one session's removed turns, with its content."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        return await self._json_query(
            ReqMethod.TRAJECTORY_CHECKPOINTS_GET,
            {"session_id": session_id},
            user_id=user_id,
        )

    async def get_subject(
        self,
        session_id: str,
        subject_id: str,
        *,
        since_revision: int,
        limit: int,
        user_id: str | None = None,
    ) -> Response:
        """Build one page of an execution subject's chain, in commit order."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        normalized_subject_id = str(subject_id or "").strip()
        if not normalized_subject_id or len(normalized_subject_id) > 256:
            return _error_response("invalid subject_id", "BAD_REQUEST", 400)
        if since_revision < 0:
            return _error_response("since_revision must be >= 0", "BAD_REQUEST", 400)
        if since_revision > _MAX_SQLITE_INTEGER:
            return _error_response("since_revision is too large", "BAD_REQUEST", 400)
        if not 1 <= limit <= 1000:
            return _error_response("limit must be between 1 and 1000", "BAD_REQUEST", 400)
        return await self._json_query(
            ReqMethod.TRAJECTORY_SUBJECT_GET,
            {
                "session_id": session_id,
                "subject_id": normalized_subject_id,
                "since_revision": since_revision,
                "limit": limit,
            },
            user_id=user_id,
        )

    async def get_stream_frames(
        self,
        session_id: str,
        *,
        since_frame_seq: int,
        limit: int,
        user_id: str | None = None,
    ) -> Response:
        """Build one page of a session's stream frames, in commit order."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        if since_frame_seq < 0:
            return _error_response("since_frame_seq must be >= 0", "BAD_REQUEST", 400)
        if since_frame_seq > _MAX_SQLITE_INTEGER:
            return _error_response("since_frame_seq is too large", "BAD_REQUEST", 400)
        if not 1 <= limit <= _MAX_FRAME_PAGE:
            return _error_response(
                f"limit must be between 1 and {_MAX_FRAME_PAGE}",
                "BAD_REQUEST",
                400,
            )
        return await self._json_query(
            ReqMethod.TRAJECTORY_STREAM_FRAMES_GET,
            {
                "session_id": session_id,
                "since_frame_seq": since_frame_seq,
                "limit": limit,
            },
            user_id=user_id,
        )

    async def get_sequences(
        self,
        session_id: str,
        seq_hashes: Sequence[str],
        *,
        since_revision: int = 0,
        user_id: str | None = None,
    ) -> Response:
        """Resolve chains by hash, for a reader whose cache lost them."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        requested = [str(value or "").strip() for value in seq_hashes]
        requested = [value for value in requested if value]
        if not requested:
            return _error_response("at least one sequence hash is required", "BAD_REQUEST", 400)
        if len(requested) > _MAX_SEQUENCE_REQUEST:
            return _error_response(
                f"at most {_MAX_SEQUENCE_REQUEST} sequences per request",
                "BAD_REQUEST",
                400,
            )
        if any(_SEQUENCE_HASH_PATTERN.fullmatch(value) is None for value in requested):
            return _error_response("invalid sequence hash", "BAD_REQUEST", 400)
        return await self._json_query(
            ReqMethod.TRAJECTORY_SEQUENCES_GET,
            {
                "session_id": session_id,
                "hashes": requested,
                "since_revision": max(0, int(since_revision)),
            },
            user_id=user_id,
        )

    async def get_raw_record(
        self,
        session_id: str,
        trace_id: str,
        span_id: str,
        user_id: str | None = None,
    ) -> Response:
        """Return the exact stored OTLP bytes for one session-owned span."""
        error = await self._validate_access(session_id, user_id)
        if error is not None:
            return error
        normalized_trace_id = str(trace_id or "").strip().lower()
        normalized_span_id = str(span_id or "").strip().lower()
        if _TRACE_ID_PATTERN.fullmatch(normalized_trace_id) is None:
            return _error_response("invalid trace_id", "BAD_REQUEST", 400)
        if _SPAN_ID_PATTERN.fullmatch(normalized_span_id) is None:
            return _error_response("invalid span_id", "BAD_REQUEST", 400)
        ok, payload = await self._call(
            ReqMethod.TRAJECTORY_RAW_GET,
            {
                "session_id": session_id,
                "trace_id": normalized_trace_id,
                "span_id": normalized_span_id,
            },
            user_id,
        )
        if not ok:
            return _failure(payload)
        try:
            raw = await self._payload_bytes(payload, user_id)
        except _QueryFailed as exc:
            return exc.response
        return Response(
            content=raw,
            status_code=200,
            headers={
                **_NO_STORE_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    async def _validate_access(self, session_id: str, user_id: str | None) -> Response | None:
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id != session_id or not is_valid_session_id(normalized_session_id):
            return _error_response("invalid session_id", "BAD_REQUEST", 400)
        try:
            metadata = await self._metadata_loader(normalized_session_id, user_id)
        except Exception:
            logger.exception(
                "Trajectory session metadata lookup failed: session_id=%s",
                normalized_session_id,
            )
            return _error_response(
                "session lookup failed",
                "SESSION_LOOKUP_FAILED",
                500,
            )
        if not metadata:
            return _error_response("session not found", "NOT_FOUND", 404)
        raw_mode = metadata.get("mode")
        raw_mode_value = getattr(raw_mode, "value", raw_mode)
        if not isinstance(raw_mode_value, str) or not raw_mode_value.strip():
            return _error_response(
                "trajectory UI supports known Agent sessions only",
                "UNSUPPORTED_SESSION_MODE",
                403,
            )
        normalized_mode = canonicalize_mode_text(raw_mode)
        team_name = str(metadata.get("team_name") or "").strip()
        single_agent_session = is_single_agent_mode(normalized_mode) and not team_name
        team_session = is_team_mode(normalized_mode) and bool(team_name)
        if not single_agent_session and not team_session:
            return _error_response(
                "trajectory UI supports Agent and Team sessions only",
                "UNSUPPORTED_SESSION_MODE",
                403,
            )
        return None

    async def _json_query(
        self,
        method: ReqMethod,
        params: dict[str, Any],
        *,
        user_id: str | None,
    ) -> Response:
        ok, payload = await self._call(method, params, user_id)
        if not ok:
            return _failure(payload)
        if payload.get("chunked"):
            try:
                raw = await self._payload_bytes(payload, user_id)
                body = json.loads(raw)
            except _QueryFailed as exc:
                return exc.response
            except Exception:
                logger.exception("[trajectory] chunked JSON body was not an object")
                return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
            if not isinstance(body, dict):
                return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
            return _json_response(body)
        result = payload.get("result")
        if not isinstance(result, dict):
            return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
        return _json_response(result)

    async def _call(
        self,
        method: ReqMethod,
        params: dict[str, Any],
        user_id: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            ok, payload = await self._fetch(method, params, user_id)
        except Exception:
            logger.exception("[trajectory] proxy failed: method=%s", getattr(method, "value", method))
            return False, {"error": "trajectory query failed", "code": "TRAJECTORY_QUERY_FAILED"}
        if not isinstance(payload, dict):
            return False, {"error": "trajectory query failed", "code": "TRAJECTORY_QUERY_FAILED"}
        return bool(ok), payload

    async def _payload_bytes(self, payload: dict[str, Any], user_id: str | None) -> bytes:
        if not payload.get("chunked"):
            encoded = payload.get("raw_b64")
            if not isinstance(encoded, str):
                raise _QueryFailed(_error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500))
            return base64.b64decode(encoded)
        export_id = str(payload.get("export_id") or "")
        buffer = io.BytesIO()
        try:
            await self._copy_blob(export_id, buffer, user_id)
        except Exception:
            buffer.close()
            raise
        return buffer.getvalue()

    async def _copy_blob(self, export_id: str, target: IO[bytes], user_id: str | None) -> None:
        offset = 0
        chunks = 0
        try:
            while True:
                ok, payload = await self._call(
                    ReqMethod.TRAJECTORY_BLOB_READ,
                    {"export_id": export_id, "offset": offset},
                    user_id,
                )
                if not ok:
                    raise _QueryFailed(_failure(payload))
                encoded = payload.get("data")
                if not isinstance(encoded, str):
                    raise _QueryFailed(_error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500))
                target.write(base64.b64decode(encoded))
                chunks += 1
                if payload.get("done"):
                    return
                next_offset = _blob_next_offset(payload.get("next_offset"))
                if next_offset is None or next_offset <= offset:
                    # A peer that keeps reporting the same window would spin
                    # here forever; fail closed so the request still ends in
                    # JSON and the finally below returns the held blob.
                    logger.warning(
                        "[trajectory] blob read made no progress: export_id=%s offset=%s",
                        export_id,
                        offset,
                    )
                    raise _QueryFailed(
                        _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
                    )
                if chunks >= _MAX_BLOB_CHUNKS:
                    raise _QueryFailed(
                        _error_response(
                            "trajectory export exceeds the chunk limit",
                            "TRAJECTORY_EXPORT_TOO_LARGE",
                            500,
                        )
                    )
                offset = next_offset
        finally:
            await self._call(ReqMethod.TRAJECTORY_BLOB_CLOSE, {"export_id": export_id}, user_id)


class _QueryFailed(Exception):
    """A blob read that already has an HTTP response to return."""

    def __init__(self, response: Response) -> None:
        super().__init__("trajectory query failed")
        self.response = response


def _failure(payload: Mapping[str, Any]) -> JSONResponse:
    code = str(payload.get("code") or "TRAJECTORY_QUERY_FAILED")
    return _error_response(
        str(payload.get("error") or "trajectory query failed"),
        code,
        error_status(code, payload.get("status")),
    )


def _blob_next_offset(value: Any) -> int | None:
    """Return the peer's next blob offset, or None when it is malformed."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def build_trajectory_fetch(channel: WebChannel) -> TrajectoryFetch:
    """Return the callable that sends one ``trajectory.*`` request."""

    async def fetch(
        method: ReqMethod,
        params: dict[str, Any],
        user_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        from gateway.routing.e2a_proxy import fetch_agent_unary

        client = getattr(channel, "trajectory_agent_client", None)
        return await fetch_agent_unary(
            agent_client=client,
            req_method=method,
            params=params,
            session_id=str(params.get("session_id") or "") or None,
            user_id=user_id,
            channel_id="web",
            label="trajectory",
        )

    return fetch


def _request_user_id(request: Request) -> str | None:
    """Resolve the caller identity the AgentServer routes the read by.

    A middleware-injected ``request.state.agentos_user_id`` wins; otherwise
    this resolves exactly like the container file API (claimed query value,
    ``x-user-id`` header, then the token username), so an AgentOS/ADX
    multi-user deployment gets the non-empty routing identity its routing
    layer requires. Single-user routing tolerates the omitted case, which
    stays None. A claim that contradicts the IAM identity raises
    FileApiUserMismatch; the route boundary maps that to a 403.
    """
    value = getattr(request.state, "agentos_user_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    from gateway.channel_manager.web.container_file_http import (
        resolve_request_user_id,
    )

    uid = resolve_request_user_id(request, request.query_params.get("user_id"))
    return uid or None


def attach_trajectory_routes(
    app: FastAPI,
    channel: WebChannel,
    *,
    metadata_loader: SessionMetadataLoader | None = None,
    fetch: TrajectoryFetch | None = None,
) -> TrajectoryHttpService:
    """Mount the trajectory HTTP routes on the WebChannel FastAPI app.

    Args:
        app: WebChannel FastAPI application.
        channel: Owning WebChannel, retained on app state for route provenance.
        metadata_loader: Optional session metadata loader override.
        fetch: Optional AgentServer caller. Production uses ``channel``.

    Returns:
        Mounted service instance.
    """
    service = TrajectoryHttpService(
        metadata_loader=metadata_loader,
        fetch=fetch or build_trajectory_fetch(channel),
    )
    from gateway.channel_manager.web.container_file_http import FileApiUserMismatch

    app.state.trajectory_http_service = service
    app.state.trajectory_web_channel = channel

    @app.middleware("http")
    async def trajectory_error_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Keep framework-level trajectory failures private and non-cacheable."""
        path = request.url.path
        if (
            path != TRAJECTORY_API_PREFIX
            and not path.startswith(f"{TRAJECTORY_API_PREFIX}/")
        ):
            return await call_next(request)
        try:
            response = await call_next(request)
        except FileApiUserMismatch:
            return _error_response("user_id 与 token 不匹配", "USER_MISMATCH", 403)
        except Exception:
            logger.exception("Unhandled trajectory HTTP request failure: path=%s", path)
            return _error_response(
                "trajectory request failed",
                "TRAJECTORY_REQUEST_FAILED",
                500,
            )
        route_handled = bool(
            getattr(request.state, "trajectory_route_handled", False)
        )
        if response.status_code == 404 and not route_handled:
            return _error_response("trajectory route not found", "NOT_FOUND", 404)
        if response.status_code == 405 and not route_handled:
            error_response = _error_response(
                "trajectory method not allowed",
                "METHOD_NOT_ALLOWED",
                405,
            )
            allowed_methods = response.headers.get("allow")
            if allowed_methods:
                error_response.headers["Allow"] = allowed_methods
            return error_response
        if response.status_code == 422:
            return _error_response("invalid trajectory request", "BAD_REQUEST", 400)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/subjects")
    async def list_trajectory_subjects(
        session_id: str,
        request: Request,
        after_revision: str = Query(default="0"),
    ) -> Response:
        """List the execution subjects that own a chain in one session."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        parsed_after = _parse_integer_query(after_revision)
        if parsed_after is None:
            return _error_response("after_revision must be an integer", "BAD_REQUEST", 400)
        return await service.list_subjects(
            session_id,
            after_revision=parsed_after,
            user_id=_request_user_id(request),
        )


    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/stream-frames")
    async def get_trajectory_stream_frames(
        session_id: str,
        request: Request,
        since_frame_seq: str = Query(default="0"),
        limit: str = Query(default="500"),
    ) -> Response:
        """Read one page of a session's stream frames, in commit order."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        parsed_since = _parse_integer_query(since_frame_seq)
        parsed_limit = _parse_integer_query(limit)
        if parsed_since is None:
            return _error_response(
                "since_frame_seq must be an integer",
                "BAD_REQUEST",
                400,
            )
        if parsed_limit is None:
            return _error_response("limit must be an integer", "BAD_REQUEST", 400)
        return await service.get_stream_frames(
            session_id,
            since_frame_seq=parsed_since,
            limit=parsed_limit,
            user_id=_request_user_id(request),
        )

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/sequences")
    async def get_trajectory_sequences(
        session_id: str,
        request: Request,
        hashes: str = Query(default=""),
        since_revision: str = Query(default="0"),
    ) -> Response:
        """Resolve content-addressed chains a reader no longer holds."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        parsed_since = _parse_integer_query(since_revision)
        if parsed_since is None:
            return _error_response("since_revision must be an integer", "BAD_REQUEST", 400)
        requested = [value for value in hashes.split(",") if value]
        return await service.get_sequences(
            session_id,
            requested,
            since_revision=parsed_since,
            user_id=_request_user_id(request),
        )

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/archive")
    async def export_trajectory_archive(
        session_id: str,
        request: Request,
    ) -> Response:
        """Export all current trajectory records for one session as a zip archive."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        return await service.export_archive(session_id, user_id=_request_user_id(request))

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/checkpoints")
    async def get_trajectory_checkpoints(
        session_id: str,
        request: Request,
    ) -> Response:
        """Read the retention checkpoints a session's views seed themselves from."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        return await service.get_checkpoints(session_id, user_id=_request_user_id(request))

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/usage")
    async def get_trajectory_session_usage(
        session_id: str,
        request: Request,
    ) -> Response:
        """Read session-complete cumulative request usage."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        return await service.get_session_usage(session_id, user_id=_request_user_id(request))

    @app.get(
        f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/subjects/{{subject_id}}/records"
    )
    async def get_trajectory_subject_records(
        session_id: str,
        subject_id: str,
        request: Request,
        since_revision: str = Query(default="0"),
        limit: str = Query(default="1000"),
    ) -> Response:
        """Read one page of an execution subject's chain, in commit order."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        parsed_since_revision = _parse_integer_query(since_revision)
        parsed_limit = _parse_integer_query(limit)
        if parsed_since_revision is None:
            return _error_response(
                "since_revision must be an integer",
                "BAD_REQUEST",
                400,
            )
        if parsed_limit is None:
            return _error_response("limit must be an integer", "BAD_REQUEST", 400)
        return await service.get_subject(
            session_id,
            subject_id,
            since_revision=parsed_since_revision,
            limit=parsed_limit,
            user_id=_request_user_id(request),
        )

    @app.get(
        f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/traces/{{trace_id}}/spans/{{span_id}}/raw"
    )
    async def get_trajectory_raw_record(
        session_id: str,
        trace_id: str,
        span_id: str,
        request: Request,
    ) -> Response:
        """Read one lossless raw OTLP record."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        return await service.get_raw_record(
            session_id,
            trace_id,
            span_id,
            user_id=_request_user_id(request),
        )

    return service


async def _load_session_metadata(
    session_id: str,
    user_id: str | None = None,
) -> Mapping[str, Any]:
    from gateway.common.external_providers import provider

    return await provider("session_metadata").get_session_metadata_async(
        session_id,
        cache_bust=True,
        enable_writeback=False,
        user_id=user_id or None,
    )


def _parse_integer_query(value: str) -> int | None:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > _MAX_INTEGER_QUERY_CHARS:
        return None
    if not normalized.isascii() or not normalized.isdecimal():
        return None
    try:
        parsed = int(normalized)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed <= _MAX_SQLITE_INTEGER else None


def _is_malformed_raw_host(raw_host: str) -> bool:
    """Return whether a raw Host header value is unusable before parsing."""
    if not raw_host or len(raw_host) > 512:
        return True
    if not raw_host.isascii() or raw_host.endswith(":"):
        return True
    has_control_character = any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw_host
    )
    if has_control_character:
        return True
    return any(
        separator in raw_host
        for separator in ("/", "\\", "?", "#", "@", ",")
    )


def _has_unexpected_host_parts(parsed_host: SplitResult) -> bool:
    """Return whether a parsed Host authority carries non-authority parts."""
    if parsed_host.username is not None or parsed_host.password is not None:
        return True
    return bool(parsed_host.path or parsed_host.query or parsed_host.fragment)


def _request_host_name(request: Request) -> str | None:
    """Return a canonical Host hostname without trusting forwarding headers."""
    host_values = request.headers.getlist("host")
    if len(host_values) != 1:
        return None
    raw_host = str(host_values[0] or "").strip()
    if _is_malformed_raw_host(raw_host):
        return None
    try:
        parsed_host = urlsplit(f"//{raw_host}")
        hostname = parsed_host.hostname
        parsed_host.port
    except ValueError:
        return None
    if hostname is None or _has_unexpected_host_parts(parsed_host):
        return None
    normalized_hostname = hostname.lower().rstrip(".")
    if not normalized_hostname or not normalized_hostname.isascii():
        return None
    return None if "%" in normalized_hostname else normalized_hostname


def _is_allowed_request_host(request: Request) -> bool:
    """Return whether the browser-facing Host belongs to the configured allowlist."""
    hostname = _request_host_name(request)
    if hostname is None:
        return False
    allowed_hosts = {
        host.lower().rstrip(".")
        for host in get_allowed_origin_hosts()
        if host.lower().rstrip(".") != "none"
    }
    return hostname in allowed_hosts


def _validate_http_origin(request: Request) -> Response | None:
    """Apply the WebChannel browser-origin gate to sensitive trace reads."""
    if not is_origin_check_enabled():
        return None
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "cross-site":
        logger.warning(
            "Trajectory HTTP cross-site request rejected: path=%s host=%s",
            request.url.path,
            request.headers.get("host"),
        )
        return _error_response("origin not allowed", "FORBIDDEN_ORIGIN", 403)
    origin = request.headers.get("origin")
    if origin is not None and is_allowed_browser_origin(origin):
        return None
    referer = request.headers.get("referer")
    if origin is None and _is_allowed_request_host(request):
        fetch_metadata_allows = fetch_site in {"same-origin", "none"}
        referer_allows = (
            not fetch_site
            and referer is not None
            and is_allowed_browser_origin(referer)
        )
        non_browser_allows = (
            not fetch_site
            and referer is None
            and is_allowed_browser_origin(None)
        )
        if fetch_metadata_allows or referer_allows or non_browser_allows:
            return None
    logger.warning(
        "Trajectory HTTP request rejected: path=%s host=%s origin=%s fetch_site=%s",
        request.url.path,
        request.headers.get("host"),
        origin,
        fetch_site,
    )
    return _error_response("origin not allowed", "FORBIDDEN_ORIGIN", 403)


def _iter_spooled_chunks(spool: IO[bytes]) -> Iterator[bytes]:
    """Read a spooled archive in chunks, closing it once the body is done."""
    try:
        while True:
            chunk = spool.read(_ARCHIVE_READ_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        spool.close()


def _json_response(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers=_NO_STORE_HEADERS,
    )


def _error_response(error: str, code: str, status_code: int) -> JSONResponse:
    return _json_response(
        {"error": error, "code": code},
        status_code=status_code,
    )


def validate_http_origin(request: Request) -> Response | None:
    """Apply the shared browser-origin gate; ``None`` means the request may proceed."""
    return _validate_http_origin(request)


async def load_session_metadata(
    session_id: str,
    user_id: str | None = None,
) -> Mapping[str, Any]:
    """Load persisted session metadata without writing back."""
    return await _load_session_metadata(session_id, user_id)


__all__ = [
    "TRAJECTORY_API_PREFIX",
    "TRAJECTORY_ARCHIVE_ENTRY_NAME",
    "TrajectoryHttpService",
    "attach_trajectory_routes",
    "load_session_metadata",
    "validate_http_origin",
]
