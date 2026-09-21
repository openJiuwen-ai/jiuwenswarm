# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway HTTP read API for persisted single-Agent trajectory records."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from jiuwenswarm.common.mode_matrix import (
    canonicalize_mode_text,
    is_single_agent_mode,
    is_team_mode,
)
from jiuwenswarm.common.security.ws_origin import (
    get_allowed_origin_hosts,
    is_allowed_browser_origin,
    is_origin_check_enabled,
)
from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    load_trajectory_store_settings,
)
from jiuwenswarm.observability.store import AsyncTrajectoryReader
from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

TRAJECTORY_API_PREFIX = "/api/trajectory"
# Archive envelope version. Version 2 is the store's schema-v4 contract; the
# viewer imports only content-addressed archives of this version.
TRAJECTORY_ARCHIVE_VERSION = 2
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

SessionMetadataLoader = Callable[[str], Mapping[str, Any]]


class TrajectoryHttpService:
    """Validated HTTP response layer over the asynchronous SQLite reader."""

    def __init__(
        self,
        settings: TrajectoryStoreSettings | None = None,
        *,
        reader: AsyncTrajectoryReader | None = None,
        metadata_loader: SessionMetadataLoader | None = None,
    ) -> None:
        # A pinned snapshot keeps callers that own their settings deterministic.
        # The gateway leaves it unset: `trajectory_ui.enabled` is toggled at
        # runtime through config.set, and the reload broadcast only reaches the
        # Agent runtime, so a snapshot taken at mount time would keep answering
        # TRAJECTORY_DISABLED until the gateway process restarts.
        self._pinned_settings = settings
        self._reader_override = reader
        self._reader: AsyncTrajectoryReader | None = None
        self._reader_database_path: Path | None = None
        self._metadata_loader = metadata_loader or _load_session_metadata

    @property
    def settings(self) -> TrajectoryStoreSettings:
        """Return the pinned snapshot, or the live ``trajectory_ui`` settings."""
        if self._pinned_settings is not None:
            return self._pinned_settings
        return load_trajectory_store_settings()

    @property
    def reader(self) -> AsyncTrajectoryReader:
        """Return the reader bound to the currently configured store root."""
        return self._reader_for(self.settings)

    def _reader_for(
        self,
        settings: TrajectoryStoreSettings,
    ) -> AsyncTrajectoryReader:
        """Return a reader for one resolved settings snapshot.

        Args:
            settings: Settings resolved once for the current request.

        Returns:
            Reader bound to ``settings.database_path``; rebuilt only when the
            configured store root changes.
        """
        if self._reader_override is not None:
            return self._reader_override
        database_path = settings.database_path
        reader = self._reader
        if reader is None or self._reader_database_path != database_path:
            reader = AsyncTrajectoryReader(database_path, session_scoped=True)
            self._reader = reader
            self._reader_database_path = database_path
        return reader

    async def list_subjects(
        self,
        session_id: str,
        *,
        after_revision: int,
    ) -> Response:
        """List the execution subjects that own a chain in one session."""
        settings = self.settings
        error = self._validate_access(session_id, settings)
        if error is not None:
            return error
        if after_revision < 0:
            return _error_response("after_revision must be >= 0", "BAD_REQUEST", 400)
        if after_revision > _MAX_SQLITE_INTEGER:
            return _error_response("after_revision is too large", "BAD_REQUEST", 400)
        try:
            items, store_epoch, watermark = await self._reader_for(settings).list_subjects(
                session_id,
                after_revision=after_revision,
            )
        except Exception:
            logger.exception("Trajectory subject listing failed: session_id=%s", session_id)
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        return _json_response(
            {
                "schema_version": 1,
                "session_id": session_id,
                "items": items,
                "watermark": watermark,
                "store_epoch": store_epoch,
            }
        )

    async def export_archive(
        self,
        session_id: str,
        *,
        addressed: bool = False,
    ) -> Response:
        """Export a stable archive of every current record in one session.

        Args:
            session_id: Session to export.
            addressed: Export references plus the dictionaries that resolve
                them, rather than putting every restated attribute back. This
                is the self-contained, compact form the trajectory viewer
                imports. Plain OTLP remains the default for other tools; it
                is an interoperability export, not an importable archive.
        """
        settings = self.settings
        error = self._validate_access(session_id, settings)
        if error is not None:
            return error
        try:
            records, store_epoch, revision, resolved = (
                await self._reader_for(settings).get_session_archive_records(
                    session_id,
                    rehydrate=not addressed,
                )
            )
        except Exception:
            logger.exception(
                "Trajectory archive query failed: session_id=%s",
                session_id,
            )
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        response = _json_response(
            {
                "format": "openjiuwen.trajectory.archive",
                "archive_version": TRAJECTORY_ARCHIVE_VERSION,
                "session_id": session_id,
                "exported_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "store_epoch": store_epoch,
                "revision": str(revision),
                "records": records,
                **({} if not addressed else {
                    "content_addressed": True,
                    "sequences": resolved.get("sequences", {}),
                    "blobs": resolved.get("blobs", {}),
                }),
            }
        )
        suffix = ".addressed" if addressed else ""
        response.headers["Content-Disposition"] = (
            f'attachment; filename="trajectory-{session_id}{suffix}.archive.json"'
        )
        return response

    async def get_session_usage(self, session_id: str) -> Response:
        """Return session-complete request usage partitioned by execution subject."""
        settings = self.settings
        error = self._validate_access(session_id, settings)
        if error is not None:
            return error
        try:
            items, store_epoch = await self._reader_for(settings).get_session_request_usage(
                session_id
            )
        except Exception:
            logger.exception(
                "Trajectory session-usage query failed: session_id=%s",
                session_id,
            )
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        return _json_response({
            "schema_version": 1,
            "session_id": session_id,
            "store_epoch": store_epoch,
            "scope": "session",
            "items": items,
        })

    async def get_subject(
        self,
        session_id: str,
        subject_id: str,
        *,
        since_revision: int,
        limit: int,
    ) -> Response:
        """Build one page of an execution subject's chain, in commit order."""
        settings = self.settings
        error = self._validate_access(session_id, settings)
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
            return _error_response(
                "limit must be between 1 and 1000",
                "BAD_REQUEST",
                400,
            )
        try:
            result = await self._reader_for(settings).get_subject_records(
                session_id,
                normalized_subject_id,
                since_revision=since_revision,
                limit=limit,
                max_bytes=settings.detail_max_bytes,
            )
        except Exception:
            logger.exception(
                "Trajectory subject-detail query failed: session_id=%s subject_id=%s",
                session_id,
                normalized_subject_id,
            )
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        if result is None:
            return _error_response("subject not found", "NOT_FOUND", 404)
        # One page states its records as references and resolves them once,
        # so content several records share crosses the wire a single time and
        # content the reader already holds does not cross it at all.
        head_hashes: set[str] = set()
        for record in result.get("records", ()):
            for reference in (record.get("sequences") or {}).values():
                if reference.get("hash"):
                    head_hashes.add(str(reference["hash"]))
        heads = sorted(head_hashes)
        resolved: dict[str, Any] = {"sequences": {}, "blobs": {}}
        if heads:
            try:
                found = await self._reader_for(settings).resolve_sequences(
                    session_id,
                    heads,
                    since_revision=since_revision,
                )
            except Exception:
                logger.exception(
                    "Trajectory sequence resolution failed: session_id=%s",
                    session_id,
                )
                return _error_response(
                    "trajectory query failed",
                    "TRAJECTORY_QUERY_FAILED",
                    500,
                )
            if found is not None:
                resolved = found
        return _json_response(
            {
                "schema_version": 1,
                "session_id": session_id,
                "subject_id": normalized_subject_id,
                **result,
                **resolved,
            }
        )

    async def get_stream_frames(
        self,
        session_id: str,
        *,
        since_frame_seq: int,
        limit: int,
    ) -> Response:
        """Build one page of a session's stream frames, in commit order.

        A reader that fell behind -- a reconnect, a slow tab -- resumes from
        the last frame it holds and walks forward, rather than waiting for
        the answer to finish before it can show anything.
        """
        settings = self.settings
        error = self._validate_access(session_id, settings)
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
        try:
            result = await self._reader_for(settings).get_stream_frames(
                session_id,
                since_frame_seq=since_frame_seq,
                limit=limit,
            )
        except Exception:
            logger.exception(
                "Trajectory stream-frame query failed: session_id=%s",
                session_id,
            )
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        if result is None:
            return _error_response("session not found", "NOT_FOUND", 404)
        return _json_response(
            {
                "schema_version": 1,
                "session_id": session_id,
                **result,
            }
        )

    async def get_sequences(
        self,
        session_id: str,
        seq_hashes: Sequence[str],
        *,
        since_revision: int = 0,
    ) -> Response:
        """Resolve chains by hash, for a reader whose cache lost them.

        The page read already carries what a reader following along needs.
        This is the path back for one that does not have it: a reload, a
        second device, an entry expired from the browser's cache.
        """
        settings = self.settings
        error = self._validate_access(session_id, settings)
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
        try:
            resolved = await self._reader_for(settings).resolve_sequences(
                session_id,
                requested,
                since_revision=max(0, int(since_revision)),
            )
        except Exception:
            logger.exception(
                "Trajectory sequence query failed: session_id=%s",
                session_id,
            )
            return _error_response("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)
        if resolved is None:
            return _error_response("session not found", "NOT_FOUND", 404)
        return _json_response({
            "schema_version": 1,
            "session_id": session_id,
            **resolved,
        })

    async def get_raw_record(
        self,
        session_id: str,
        trace_id: str,
        span_id: str,
    ) -> Response:
        """Return the exact stored OTLP bytes for one session-owned span."""
        settings = self.settings
        error = self._validate_access(session_id, settings)
        if error is not None:
            return error
        normalized_trace_id = str(trace_id or "").strip().lower()
        normalized_span_id = str(span_id or "").strip().lower()
        if _TRACE_ID_PATTERN.fullmatch(normalized_trace_id) is None:
            return _error_response("invalid trace_id", "BAD_REQUEST", 400)
        if _SPAN_ID_PATTERN.fullmatch(normalized_span_id) is None:
            return _error_response("invalid span_id", "BAD_REQUEST", 400)
        try:
            raw_json = await self._reader_for(settings).get_raw_record(
                session_id,
                normalized_trace_id,
                normalized_span_id,
            )
        except Exception:
            logger.exception(
                "Trajectory raw-record query failed: session_id=%s trace_id=%s span_id=%s",
                session_id,
                normalized_trace_id,
                normalized_span_id,
            )
            return _error_response(
                "trajectory query failed",
                "TRAJECTORY_QUERY_FAILED",
                500,
            )
        if raw_json is None:
            return _error_response("span not found", "NOT_FOUND", 404)
        return Response(
            content=raw_json,
            status_code=200,
            headers={
                **_NO_STORE_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    def _validate_access(
        self,
        session_id: str,
        settings: TrajectoryStoreSettings,
    ) -> Response | None:
        if not settings.enabled:
            return _error_response(
                "trajectory UI is disabled",
                "TRAJECTORY_DISABLED",
                503,
            )
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id != session_id or not is_valid_session_id(normalized_session_id):
            return _error_response("invalid session_id", "BAD_REQUEST", 400)
        try:
            metadata = self._metadata_loader(normalized_session_id)
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


def attach_trajectory_routes(
    app: FastAPI,
    channel: WebChannel,
    *,
    settings: TrajectoryStoreSettings | None = None,
    reader: AsyncTrajectoryReader | None = None,
    metadata_loader: SessionMetadataLoader | None = None,
) -> TrajectoryHttpService:
    """Mount the trajectory HTTP routes on the WebChannel FastAPI app.

    Args:
        app: WebChannel FastAPI application.
        channel: Owning WebChannel, retained on app state for route provenance.
        settings: Optional pinned settings override. Omit it so every request
            resolves the live ``trajectory_ui`` settings.
        reader: Optional reader override for tests.
        metadata_loader: Optional session metadata loader override.

    Returns:
        Mounted service instance.
    """
    service = TrajectoryHttpService(
        settings,
        reader=reader,
        metadata_loader=metadata_loader,
    )
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
        return await service.list_subjects(session_id, after_revision=parsed_after)


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
        )

    @app.get(f"{TRAJECTORY_API_PREFIX}/sessions/{{session_id}}/archive")
    async def export_trajectory_archive(
        session_id: str,
        request: Request,
        archive_format: str = Query(default="otlp", alias="format"),
    ) -> Response:
        """Export all current trajectory records for one session."""
        request.state.trajectory_route_handled = True
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error
        if archive_format not in ("otlp", "addressed"):
            return _error_response("format must be otlp or addressed", "BAD_REQUEST", 400)
        return await service.export_archive(
            session_id,
            addressed=archive_format == "addressed",
        )

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
        return await service.get_session_usage(session_id)

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
        return await service.get_raw_record(session_id, trace_id, span_id)

    return service


def _load_session_metadata(session_id: str) -> Mapping[str, Any]:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    return get_session_metadata(
        session_id,
        cache_bust=True,
        enable_writeback=False,
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


__all__ = [
    "TRAJECTORY_API_PREFIX",
    "TrajectoryHttpService",
    "attach_trajectory_routes",
]
