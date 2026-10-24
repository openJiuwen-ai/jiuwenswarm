"""Gateway lifecycle orchestration: archive checks state; deletion stops work."""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.agent_client import DuplicateRequestIdError
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary


def register_lifecycle_handlers(channel, resolve_client, resolve_cron):
    watchers = {}

    async def call(method, params, session_id, user_id):
        return await fetch_agent_unary(
            agent_client=resolve_client(),
            req_method=ReqMethod(method),
            params=params,
            session_id=session_id,
            user_id=user_id,
            channel_id=channel.channel_id,
            timeout_seconds=120,
        )

    async def watch(user_id):
        revisions = {}
        first = True
        while True:
            clients = [
                ws
                for ws in getattr(channel, "clients", ())
                if str(channel.connection_user_id(ws) or "") == str(user_id or "")
            ]
            if not clients:
                return
            try:
                ok, response = await call(
                    "project.lifecycle", {"events": True}, None, user_id
                )
                if ok:
                    for entry in response.get("events", []):
                        payload = entry["payload"]
                        key = (entry["event"], payload["resource_id"])
                        previous = revisions.get(key, -1)
                        revisions[key] = payload["revision"]
                        if payload["revision"] <= previous:
                            continue
                        if entry["completed"] and entry["kind"] in {"archive", "delete"}:
                            if entry["event"].startswith("session."):
                                mark_sessions_idle([payload["resource_id"]])
                        if first:
                            continue
                        for ws in clients:
                            await channel.send_event(ws, entry["event"], payload)
                            if entry["completed"] and not (
                                entry["kind"] == "delete"
                                and entry["event"].startswith("project.")
                                and not entry["result"].get("deleted")
                            ):
                                suffix = {
                                    "archive": "archived",
                                    "unarchive": "unarchived",
                                    "delete": "deleted",
                                }[entry["kind"]]
                                await channel.send_event(
                                    ws,
                                    entry["event"].split(".")[0] + "." + suffix,
                                    {
                                        **entry["result"],
                                        "project_id": payload["project_id"],
                                    },
                                )
                    first = False
            except DuplicateRequestIdError:
                # request_id 撞号说明 id 生成器出了问题（或同一连接上请求串行
                # 堆积），不是"服务暂不可用"的正常抖动，必须可见。
                logging.getLogger(__name__).warning(
                    "lifecycle event polling dropped: duplicate request_id",
                    exc_info=True,
                )
            except Exception:
                # 后台轮询，AgentServer 短暂不可用时每 2s 都会走到这里，
                # 保持 debug，避免刷屏。
                logging.getLogger(__name__).debug(
                    "lifecycle event polling deferred", exc_info=True
                )
            await asyncio.sleep(2)

    def ensure_watch(user_id):
        owner = str(user_id or "")
        if owner not in watchers or watchers[owner].done():
            watchers[owner] = asyncio.create_task(
                watch(owner), name="web-lifecycle-events"
            )

    channel.ensure_lifecycle_watch = ensure_watch

    def _completed_session_ids(payload: dict) -> list[str]:
        results = payload.get("results")
        items = results if isinstance(results, list) else [payload]
        session_ids = []
        for item in items:
            if not isinstance(item, dict) or not item.get("ok", True):
                continue
            if item.get("finalizing") or item.get("stop_pending"):
                continue
            if item.get("accepted") and item.get("finalizing") is not False:
                continue
            session_id = item.get("session_id")
            if session_id:
                session_ids.append(str(session_id))
        return session_ids

    def mark_sessions_idle(session_ids: list[str]) -> None:
        """Clear the cached busy flag for Sessions a lifecycle action stopped.

        Archive/delete stop runtime work in the AgentServer without emitting
        ``chat.processing_status(false)``.  Without this, a later
        ``session.get_metadata`` (for example after unarchive) would report a
        stopped Session as still processing.
        """
        clear = getattr(channel, "clear_session_busy", None)
        if not callable(clear):
            return
        for sid in dict.fromkeys(session_ids):
            clear(sid)

    def handler(method):
        async def handle(ws, req_id, params, session_id, user_id=None):
            ensure_watch(user_id)
            if not isinstance(params, dict):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="params must be object",
                    code="BAD_REQUEST",
                )
                return
            # Browser callers cannot inject internal stage acknowledgements.
            public = {}
            for k, v in params.items():
                if k not in {
                    "_lifecycle_stage",
                    "operation_id",
                    "generation",
                    "stopped_cron_jobs",
                    "deleted_cron_jobs",
                    "completed_cron_job_ids",
                }:
                    public[k] = v
            ok, payload = await call(method, public, session_id, user_id)
            await channel.send_response(
                ws,
                req_id,
                ok=ok,
                payload=payload,
                error=None if ok else payload.get("error"),
                code=None if ok else payload.get("code"),
            )
            if ok and method in {
                "session.archive",
                "session.delete",
                "project.sessions.archive",
                "project.sessions.delete_archived",
            }:
                # Acceptance does not prove that file writers have stopped.
                # Pending work keeps its busy guard until the completion poll.
                mark_sessions_idle(_completed_session_ids(payload))
            if ok and method == "session.unarchive":
                project_ids = {
                    item.get("project_id") for item in payload.get("results", [payload])
                    if item.get("ok", True) and item.get("restored") and item.get("project_id")
                }
                for client_ws in list(getattr(channel, "clients", ()) or ()):
                    if str(channel.connection_user_id(client_ws) or "") != str(user_id or ""):
                        continue
                    for project_id in project_ids:
                        try:
                            await channel.send_event(client_ws, "project.restored", {"project_id": project_id})
                        except Exception:
                            logging.getLogger(__name__).debug("project restore broadcast failed", exc_info=True)
            if ok and not method.endswith(".list"):
                event = (
                    method.replace(".archive", ".archived")
                    .replace(".unarchive", ".unarchived")
                    .replace(".delete", ".deleted")
                )
                # Match the caller's user boundary; never broadcast personal IDs
                # across all tenants connected to the Gateway.
                if method.startswith("project.sessions."):
                    event = "session.archived" if method.endswith(".archive") else "session.deleted"
                if method.startswith(("session.", "project.sessions.")):
                    for item in payload.get("results", [payload]):
                        if item.get("ok", True) and item.get("session_id"):
                            clear_expert = getattr(channel, "clear_expert_processing", None)
                            session_closed = (
                                event in {"session.archived", "session.deleted"}
                                and "operation_id" not in payload
                            )
                            if callable(clear_expert) and session_closed:
                                clear_expert(item["session_id"])
                            send_event = getattr(channel, "send_event", None)
                            if callable(send_event):
                                await send_event(ws, event, item)
                elif "operation_id" not in payload and (method != "project.delete" or payload.get("deleted")):
                    send_event = getattr(channel, "send_event", None)
                    if callable(send_event):
                        await send_event(ws, event, payload)

        return handle

    for method in (
        "session.archive",
        "session.unarchive",
        "session.archived.list",
        "session.delete",
        "project.sessions.archive",
        "project.sessions.delete_archived",
    ):
        channel.register_method(method, handler(method))
