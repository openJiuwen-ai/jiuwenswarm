from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator

from jiuwenclaw.e2a.constants import (
    FILE_TRANSFER_START,
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
)
from jiuwenclaw.e2a.agent_compat import e2a_to_agent_request
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.gateway.agent_client import AgentServerClient
from jiuwenclaw.gateway.session_map import load_session_map_scope
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.utils import FileTransferStartParams

logger = logging.getLogger(__name__)


class YuanrongFrontendAgentClient(AgentServerClient):
    """Yuanrong frontend invocations HTTP client."""

    def __init__(
        self,
        *,
        frontend_endpoint: str,
        function_version_urn: str,
        concurrency: int = 1,
        invoke_timeout_s: float = 60.0,
    ) -> None:
        self._frontend_endpoint = (frontend_endpoint or "").rstrip("/")
        self._function_version_urn = (function_version_urn or "").strip()
        self._concurrency = max(int(concurrency), 1)
        self._invoke_timeout_s = float(invoke_timeout_s)
        self._connected = False
        self._server_ready = False
        self._session_instance_map: dict[str, tuple[str, str | None]] = {}
        self._session_instance_lock = threading.Lock()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    @property
    def server_ready(self) -> bool:
        return self._server_ready

    async def connect(self, uri: str) -> None:
        endpoint = (uri or "").strip()
        if endpoint and endpoint.lower().startswith(("http://", "https://")):
            self._frontend_endpoint = endpoint.rstrip("/")
        if not self._frontend_endpoint:
            raise ValueError("frontend_endpoint cannot be empty")
        if not self._function_version_urn:
            raise ValueError("function_version_urn cannot be empty")
        self._connected = True
        self._server_ready = True
        logger.info("[YuanrongFrontendAgentClient] ready: endpoint=%s", self._frontend_endpoint)

    async def disconnect(self) -> None:
        self._connected = False
        self._server_ready = False
        logger.info("[YuanrongFrontendAgentClient] disconnected")

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    @staticmethod
    def _unwrap_payload(payload: Any) -> dict[str, Any]:
        """Normalize AgentResponse.payload into a dict for file-transfer handlers.

        This client currently wraps HTTP response JSON into payload={"content": parsed}.
        For file-transfer RPCs, Gateway expects top-level keys like accepted/success/error.
        """
        if not payload:
            return {}
        if isinstance(payload, dict):
            if "content" in payload and isinstance(payload.get("content"), dict):
                return dict(payload["content"])
            return dict(payload)
        return {"content": payload}

    @staticmethod
    def _md5_id(*parts: str) -> str:
        return hashlib.md5("::".join(parts).encode("utf-8")).hexdigest()

    def _session_id_to_invoke_ids(self, session_id: str) -> tuple[str, str | None]:
        with self._session_instance_lock:
            cached = self._session_instance_map.get(session_id)
            if cached:
                return cached

        # SessionMap-generated ids:
        # per_chat_bot: provider::chat::bot::ts::suffix
        # per_chat_bot_user: provider::chat::bot::user::ts::suffix
        # For non-standard ids, fallback to md5(session_id) + no space_id.
        _ = load_session_map_scope()
        parts = session_id.split("::")
        if len(parts) == 6:
            _provider, chat_id, bot_id, user_id, _ts, _suffix = parts
            pair = (self._md5_id(chat_id, bot_id), user_id)
        elif len(parts) == 5:
            _provider, chat_id, bot_id, _ts, _suffix = parts
            pair = (self._md5_id(chat_id, bot_id), None)
        else:
            pair = (hashlib.md5(session_id.encode("utf-8")).hexdigest(), None)

        with self._session_instance_lock:
            self._session_instance_map.setdefault(session_id, pair)
            return self._session_instance_map[session_id]

    def _invoke_url(self) -> str:
        urn = urllib.parse.quote(self._function_version_urn, safe="")
        return f"{self._frontend_endpoint}/serverless/v1/functions/{urn}/invocations"

    def _do_invoke(self, payload: dict[str, Any], session_id: str) -> tuple[int, str]:
        service_id, agent_id = self._session_id_to_invoke_ids(session_id or "")
        headers = {
            "Content-Type": "application/json",
            "X-Instance-Session": json.dumps(
                {"sessionID": service_id, "concurrency": self._concurrency},
                ensure_ascii=False,
            ),
        }
        body = dict(payload)
        body["service_id"] = service_id
        body["agent_id"] = agent_id or ""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._invoke_url(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._invoke_timeout_s) as resp:
                status = int(getattr(resp, "status", 200))
                text = resp.read().decode("utf-8", errors="replace")
                return status, text
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace") if err.fp else str(err)
            return int(getattr(err, "code", 500) or 500), text

    @staticmethod
    def _iter_sse_data_events(buffer: str) -> tuple[list[str], str]:
        events: list[str] = []
        normalized = buffer.replace("\r\n", "\n")
        while True:
            boundary = normalized.find("\n\n")
            if boundary < 0:
                break
            raw_event = normalized[:boundary]
            normalized = normalized[boundary + 2:]
            if not raw_event.strip():
                continue
            data_lines = []
            for line in raw_event.split("\n"):
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                events.append("\n".join(data_lines).strip())
        return events, normalized

    def _do_invoke_stream(
        self,
        payload: dict[str, Any],
        session_id: str,
        out_queue: "asyncio.Queue[tuple[str, str | None]]",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        service_id, agent_id = self._session_id_to_invoke_ids(session_id or "")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Instance-Session": json.dumps(
                {"sessionID": service_id, "concurrency": self._concurrency},
                ensure_ascii=False,
            ),
        }
        body = dict(payload)
        body["service_id"] = service_id
        body["agent_id"] = agent_id or ""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._invoke_url(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._invoke_timeout_s) as resp:
                status = int(getattr(resp, "status", 200))
                if not (200 <= status < 300):
                    text = resp.read().decode("utf-8", errors="replace")
                    loop.call_soon_threadsafe(
                        out_queue.put_nowait,
                        ("error", json.dumps({"http_status": status, "body": text}, ensure_ascii=False)),
                    )
                    return
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    loop.call_soon_threadsafe(out_queue.put_nowait, ("chunk", chunk.decode("utf-8", errors="replace")))
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace") if err.fp else str(err)
            loop.call_soon_threadsafe(
                out_queue.put_nowait,
                (
                    "error",
                    json.dumps({
                        "http_status": int(getattr(err, "code", 500) or 500),
                        "body": text
                    }, ensure_ascii=False),
                ),
            )
        except Exception as err:
            loop.call_soon_threadsafe(out_queue.put_nowait, ("exception", str(err)))
        finally:
            loop.call_soon_threadsafe(out_queue.put_nowait, ("done", None))

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self._ensure_connected()
        request = e2a_to_agent_request(envelope)
        payload = {
            "request_id": request.request_id,
            "channel_id": request.channel_id,
            "session_id": request.session_id,
            "req_method": request.req_method.value if request.req_method else None,
            "params": request.params,
            "is_stream": False,
            "timestamp": request.timestamp,
            "metadata": request.metadata,
        }
        status, body = await asyncio.to_thread(self._do_invoke, payload, request.session_id or "")
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"content": body}
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=200 <= status < 300,
            payload={"content": parsed},
            metadata={"http_status": status},
        )

    # =========================================================================
    # File transfer methods (Gateway -> AgentServer)
    # =========================================================================

    async def file_transfer_start(
        self,
        params: FileTransferStartParams,
    ) -> dict[str, Any]:
        """Send FILE_TRANSFER_START (compatible with WebSocketAgentServerClient)."""
        self._ensure_connected()
        envelope = E2AEnvelope(
            request_id=f"ft_start_{params.transfer_id}",
            channel=params.channel_id,
            method=FILE_TRANSFER_START,
            params={
                "event_type": FILE_TRANSFER_START,
                "transfer_id": params.transfer_id,
                "filename": params.filename,
                "file_size": params.file_size,
                "sha256": params.sha256,
                "total_chunks": params.total_chunks,
                "chunk_size": params.chunk_size,
                "mime_type": params.mime_type,
            },
            session_id=params.session_id,
            is_stream=False,
        )
        resp = await self.send_request(envelope)
        out = self._unwrap_payload(resp.payload)
        out["success"] = bool(resp.ok)
        return out

    async def file_transfer_chunk(
        self,
        transfer_id: str,
        chunk_index: int,
        base64_data: str,
        chunk_size: int,
        channel_id: str = "",
    ) -> dict[str, Any]:
        """Send FILE_TRANSFER_CHUNK (compatible with WebSocketAgentServerClient)."""
        self._ensure_connected()
        envelope = E2AEnvelope(
            request_id=f"ft_chunk_{transfer_id}_{chunk_index}",
            channel=channel_id,
            method=FILE_TRANSFER_CHUNK,
            params={
                "event_type": FILE_TRANSFER_CHUNK,
                "transfer_id": transfer_id,
                "chunk_index": chunk_index,
                "base64_data": base64_data,
                "chunk_size": chunk_size,
            },
            is_stream=False,
        )
        resp = await self.send_request(envelope)
        out = self._unwrap_payload(resp.payload)
        out["success"] = bool(resp.ok)
        return out

    async def file_transfer_complete(
        self,
        transfer_id: str,
        sha256: str,
        channel_id: str = "",
    ) -> dict[str, Any]:
        """Send FILE_TRANSFER_COMPLETE (compatible with WebSocketAgentServerClient)."""
        self._ensure_connected()
        envelope = E2AEnvelope(
            request_id=f"ft_complete_{transfer_id}",
            channel=channel_id,
            method=FILE_TRANSFER_COMPLETE,
            params={
                "event_type": FILE_TRANSFER_COMPLETE,
                "transfer_id": transfer_id,
                "sha256": sha256,
            },
            is_stream=False,
        )
        resp = await self.send_request(envelope)
        out = self._unwrap_payload(resp.payload)
        out["success"] = bool(resp.ok)
        return out

    async def send_file(
        self,
        file_path: str,
        session_id: str = "",
        channel_id: str = "",
    ) -> dict[str, Any]:
        """Send a local file to AgentServer via file.transfer.* RPCs (chunked)."""
        from pathlib import Path
        import base64

        self._ensure_connected()
        path = Path(file_path)
        if not path.exists():
            return {"success": False, "error": f"file not found: {file_path}"}

        try:
            file_data = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"read file failed: {exc}"}

        file_size = len(file_data)
        filename = path.name
        sha256 = hashlib.sha256(file_data).hexdigest()
        chunk_size = 65536
        total_chunks = (file_size + chunk_size - 1) // chunk_size if chunk_size > 0 else 1
        transfer_id = (
            f"up_{int(time.time() * 1000)}_{hashlib.md5(filename.encode()).hexdigest()[:8]}"
        )

        start_params = FileTransferStartParams(
            transfer_id=transfer_id,
            filename=filename,
            file_size=file_size,
            sha256=sha256,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            session_id=session_id,
            channel_id=channel_id,
        )
        start_resp = await self.file_transfer_start(start_params)
        if not start_resp.get("accepted"):
            return {
                "success": False,
                "error": start_resp.get("error", "transfer start rejected"),
            }

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, file_size)
            chunk_data = file_data[start:end]
            b64_data = base64.b64encode(chunk_data).decode("utf-8")

            chunk_resp = await self.file_transfer_chunk(
                transfer_id=transfer_id,
                chunk_index=i,
                base64_data=b64_data,
                chunk_size=len(chunk_data),
                channel_id=channel_id,
            )
            if not chunk_resp.get("accepted"):
                logger.warning(
                    "[YuanrongFrontendAgentClient] chunk send failed: transfer_id=%s chunk=%d error=%s",
                    transfer_id,
                    i,
                    chunk_resp.get("error"),
                )

        complete_resp = await self.file_transfer_complete(
            transfer_id=transfer_id,
            sha256=sha256,
            channel_id=channel_id,
        )
        return complete_resp

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        self._ensure_connected()
        request = e2a_to_agent_request(envelope)
        payload = {
            "request_id": request.request_id,
            "channel_id": request.channel_id,
            "session_id": request.session_id,
            "req_method": request.req_method.value if request.req_method else None,
            "params": request.params,
            "is_stream": True,
            "timestamp": request.timestamp,
            "metadata": request.metadata,
        }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        reader_task = asyncio.create_task(
            asyncio.to_thread(self._do_invoke_stream, payload, request.session_id or "", queue, loop)
        )
        sse_buffer = ""
        try:
            while True:
                item_type, text = await queue.get()
                if item_type == "chunk" and text:
                    sse_buffer += text
                    events, sse_buffer = self._iter_sse_data_events(sse_buffer)
                    for event_data in events:
                        if not event_data or event_data == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(event_data)
                        except Exception:
                            parsed = {"content": event_data}
                        parsed_obj = parsed if isinstance(parsed, dict) else {"content": parsed}
                        if "[DONE]" in event_data:
                            break
                        yield AgentResponseChunk(
                            request_id=str(parsed_obj.get("request_id") or request.request_id),
                            channel_id=str(parsed_obj.get("channel_id") or request.channel_id),
                            payload=parsed_obj.get("payload", parsed_obj.get("content")),
                            is_complete=bool(parsed_obj.get("is_complete", False)),
                        )
                elif item_type == "error":
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"error": text or "invoke stream failed"},
                        is_complete=False,
                    )
                elif item_type == "exception":
                    raise RuntimeError(f"invoke stream failed: {text}")
                elif item_type == "done":
                    break

            if sse_buffer.strip():
                tail_events, _ = self._iter_sse_data_events(sse_buffer + "\n\n")
                for event_data in tail_events:
                    if not event_data or event_data == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(event_data)
                    except Exception:
                        parsed = {"content": event_data}
                    parsed_obj = parsed if isinstance(parsed, dict) else {"content": parsed}
                    yield AgentResponseChunk(
                        request_id=str(parsed_obj.get("request_id") or request.request_id),
                        channel_id=str(parsed_obj.get("channel_id") or request.channel_id),
                        payload=parsed_obj.get("payload", parsed_obj.get("content")),
                        is_complete=bool(parsed_obj.get("is_complete", False)),
                    )
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=None,
                is_complete=True,
            )
        finally:
            try:
                await reader_task
            except Exception as exc:
                logger.exception("Error awaiting reader_task in send_request_stream: %s", exc)
