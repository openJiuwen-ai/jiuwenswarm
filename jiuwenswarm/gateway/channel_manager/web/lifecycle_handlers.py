"""Gateway lifecycle orchestration: archive checks state; deletion stops work."""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.common.schema.message import ReqMethod
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
                        if first or payload["revision"] <= previous:
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
            except Exception:
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
            if method == "project.delete":
                cc = resolve_cron()
                try:
                    if cc is None:
                        raise RuntimeError("cron service is unavailable")
                    cc.scheduler.remember_lifecycle_owner(user_id)
                except Exception as exc:
                    await channel.send_response(
                        ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
                    )
                    return
                ok, payload = await call(
                    method,
                    {**public, "_lifecycle_stage": "prepare"},
                    session_id,
                    user_id,
                )
                if ok and "operation_id" in payload:
                    token = dict(payload)
                    cc = resolve_cron()
                    try:
                        if cc is None:
                            raise RuntimeError("cron service is unavailable")
                        _, snapshot = await call(
                            "project.lifecycle",
                            {"project_id": public.get("project_id")},
                            session_id,
                            user_id,
                        )
                        operation = snapshot.get("operation") or {}
                        all_jobs = await cc.store.list_jobs()
                        jobs = []
                        for job in all_jobs:
                            if (
                                job.project_id == public.get("project_id")
                                and str(job.user_id or "") == str(user_id or "")
                            ):
                                jobs.append(job)
                        planned = list(
                            dict.fromkeys(
                                [
                                    *operation.get("planned_cron_job_ids", []),
                                    *(job.id for job in jobs),
                                ]
                            )
                        )
                        saved, failure = await call(
                            "project.lifecycle",
                            {**token, "planned_cron_job_ids": planned},
                            session_id,
                            user_id,
                        )
                        if not saved:
                            raise RuntimeError(
                                failure.get("error", "checkpoint failed")
                            )
                        completed = list(
                            operation.get("completed_items", {}).get("cron", [])
                        )

                        async def checkpoint_plan(job_ids):
                            nonlocal planned
                            planned = list(dict.fromkeys([*planned, *job_ids]))
                            saved, failure = await call(
                                "project.lifecycle",
                                {**token, "planned_cron_job_ids": planned},
                                session_id,
                                user_id,
                            )
                            if not saved:
                                raise RuntimeError(
                                    failure.get("error", "checkpoint failed")
                                )

                        async def checkpoint(job_id):
                            if job_id not in completed:
                                completed.append(job_id)
                            saved, error = await call(
                                "project.lifecycle",
                                {**token, "completed_cron_job_ids": completed},
                                session_id,
                                user_id,
                            )
                            if not saved:
                                raise RuntimeError(
                                    error.get("error", "cron progress commit failed")
                                )

                        await cc.delete_project_jobs(
                            str(public.get("project_id") or ""),
                            user_id=user_id,
                            checkpoint=checkpoint,
                            plan=checkpoint_plan,
                        )
                        ok, payload = await call(
                            method,
                            {
                                **public,
                                **token,
                                "_lifecycle_stage": "finish",
                                "deleted_cron_jobs": len(planned),
                                "completed_cron_job_ids": completed,
                            },
                            session_id,
                            user_id,
                        )
                    except Exception as exc:
                        phase = "delete_cron"
                        _, status = await call(
                            "project.lifecycle",
                            {
                                **token,
                                "failed": True,
                                "phase": phase,
                                "error": str(exc),
                            },
                            session_id,
                            user_id,
                        )
                        ok = False
                        status_fields = {}
                        for k in ("lifecycle_operation", "execution_blocked", "stop_pending"):
                            status_fields[k] = status.get(k)
                        payload = dict(
                            code="PARTIAL_PROJECT_DELETE_FAILED",
                            error=str(exc),
                            operation_id=token["operation_id"],
                            project_id=public.get("project_id"),
                            phase=phase,
                            retryable=True,
                            completed_session_ids=[],
                            completed_cron_job_ids=locals().get("completed", []),
                            failed_items=[
                                dict(
                                    resource_type="project",
                                    resource_id=public.get("project_id"),
                                    code="INTERNAL_ERROR",
                                    error=str(exc),
                                )
                            ],
                            **status_fields,
                        )
            else:
                ok, payload = await call(method, public, session_id, user_id)
            await channel.send_response(
                ws,
                req_id,
                ok=ok,
                payload=payload,
                error=None if ok else payload.get("error"),
                code=None if ok else payload.get("code"),
            )
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
                            await channel.send_event(ws, event, item)
                elif "operation_id" not in payload and (method != "project.delete" or payload.get("deleted")):
                    await channel.send_event(ws, event, payload)

        return handle

    for method in (
        "session.archive",
        "session.unarchive",
        "session.archived.list",
        "session.delete",
        "project.sessions.archive",
        "project.sessions.delete_archived",
        "project.delete",
    ):
        channel.register_method(method, handler(method))
