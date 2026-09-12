"""AgentServer RSI 分发层：16 个 ``_handle_rsi_*`` 的统一接线面（B2）。

- ``RsiAgentServerHandlers`` 聚合服务域（``RsiServiceContext``）+ 推送发送器，
  ``handle(request)`` 按 ``req_method`` 分发；
- 保持 ``agent_ws_server.py`` 改动最小：dispatch 链新增一行分支 → ``rsi_handlers.handle(request)``。
- 推送：AgentServer 注入 ``send_push(event_type, task_id, payload)`` 包装
  （P1 状态钩子 / P2 进度 / P3 树增量；P2/P3 消费接线在事件链路，见 event_consumer）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

from jiuwenswarm.agents.harness.common.rsi.errors import RsiBadRequest, RsiError, RsiNotReady

logger = logging.getLogger(__name__)

#: 推送事件类型（web 契约 v0.3 §4 P1/P2/P3）
RSI_PUSH_STATUS_CHANGED = "rsi.training.status.changed"
RSI_PUSH_PROGRESS = "rsi.training.progress"
RSI_PUSH_TREE_DELTA = "rsi.training.tree.delta"

#: 18 个 web method → 服务域方法名（I1–I13 + control/artifact/tree + Harness lifecycle）
_METHOD_DISPATCH: dict[str, str] = {
    "rsi.dataset.validate": "dataset_validate",
    "rsi.task.create": "task_create",
    "rsi.task.list": "task_list",
    "rsi.task.get": "task_get",
    "rsi.task.delete": "task_delete",
    "rsi.training.start": "training_start",
    "rsi.training.pause": "training_pause",
    "rsi.training.resume": "training_resume",
    "rsi.training.terminate": "training_terminate",
    "rsi.report.get": "report_get",
    "rsi.usage.get": "usage_get",
    "rsi.artifact.download": "artifact_download",
    "rsi.artifact.files.list": "artifact_files_list",
    "rsi.artifact.files.get": "artifact_files_get",
    "rsi.tree.get": "tree_get",
    "rsi.harness.install": "harness_install",
    "rsi.harness.versions.list": "harness_versions_list",
    "rsi.harness.rollback": "harness_rollback",
}


class RsiAgentServerHandlers:
    """AgentServer 侧 RSI 分发 + 推送发送（依赖注入，便于测试）。"""

    def __init__(
        self,
        context: Any,
        *,
        send_push: Callable[[dict[str, Any]], bool] | None = None,
        harness_refs_provider: Callable[[], str | None] | None = None,
        default_channel_id: str = "web",
    ) -> None:
        self.context = context
        self.send_push = send_push or (lambda msg: False)
        self.default_channel_id = default_channel_id
        # 服务域接线：harness_refs 快照提供方（默认 auto_harness 激活 refs）
        if harness_refs_provider is not None:
            self.context.bind_task_service(harness_refs_provider=harness_refs_provider)
        # Provider registration is complete before handlers are constructed.
        # Reconcile durable active tasks before accepting the first RSI call;
        # recovery never re-enqueues work or invokes the engine.
        self.recovery_summary = self.context.recover_workspace()
        # P1 状态钩子：TaskStore.update_status → send_push（服务侧权威，不依赖引擎事件）
        self._bind_status_push()
        # P2/P3 推送：事件链路回调（progress/tree.delta）
        self._bind_event_push()

    # -- 分发 --

    def handle(self, request: Any) -> dict[str, Any]:
        """按 req_method 分发；服务域异常 → 统一 RsiError 语义载荷。"""
        req_method = getattr(request, "req_method", None)
        method = getattr(req_method, "value", None) if req_method is not None else None
        handler_name = _METHOD_DISPATCH.get(method)
        if handler_name is None:
            return {"ok": False, "error": f"unsupported rsi method: {method}", "code": "BAD_REQUEST"}
        handler = getattr(self, f"_do_{handler_name}", None)
        if handler is None:
            return {"ok": False, "error": f"handler not implemented: {method}", "code": "INTERNAL_ERROR"}
        raw_params = request.params if isinstance(request.params, dict) else {}
        # Keep the AgentRequest transport session separate from the public
        # method payload.  The task service stores this private marker so
        # later Provider events can be sent to the originating WebSocket.
        params = dict(raw_params)
        request_session_id = str(getattr(request, "session_id", None) or "").strip()
        if request_session_id:
            params.setdefault("_rsi_session_id", request_session_id)
        try:
            payload = handler(params)
            return {"ok": True, "payload": payload}
        except RsiError as exc:
            return {"ok": False, "error": exc.message, "code": exc.code}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一 INTERNAL_ERROR 语义
            logger.exception("[RSI] %s failed: %s", method, exc)
            return {"ok": False, "error": str(exc), "code": "INTERNAL_ERROR"}

    async def handle_async(self, request: Any) -> dict[str, Any]:
        """Asynchronously dispatch RSI methods that may return coroutines.

        Existing service methods are synchronous and continue to use
        :meth:`handle`.  The Harness installation path must await the
        AgentManager/DeepAgent hot-load chain, so AgentServer uses this entry
        point to preserve the same error normalization after awaiting.
        """
        req_method = getattr(request, "req_method", None)
        method = getattr(req_method, "value", None) if req_method is not None else None
        handler_name = _METHOD_DISPATCH.get(method)
        if handler_name is None:
            return {"ok": False, "error": f"unsupported rsi method: {method}", "code": "BAD_REQUEST"}
        handler = getattr(self, f"_do_{handler_name}", None)
        if handler is None:
            return {"ok": False, "error": f"handler not implemented: {method}", "code": "INTERNAL_ERROR"}
        raw_params = request.params if isinstance(request.params, dict) else {}
        params = dict(raw_params)
        request_session_id = str(getattr(request, "session_id", None) or "").strip()
        if request_session_id:
            params.setdefault("_rsi_session_id", request_session_id)
        try:
            payload = handler(params)
            if inspect.isawaitable(payload):
                payload = await payload
            return {"ok": True, "payload": payload}
        except RsiError as exc:
            return {"ok": False, "error": exc.message, "code": exc.code}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一 INTERNAL_ERROR 语义
            logger.exception("[RSI] %s failed: %s", method, exc)
            return {"ok": False, "error": str(exc), "code": "INTERNAL_ERROR"}

    # -- I1–I13 + Harness install --

    def _do_dataset_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.dataset_service.validate(
            params,
            adapter=self.context.adapter_for(
                params.get("scenario"), params.get("artifact_type")
            ),
        )

    def _do_task_create(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self.context.task_service.create(params)
        task_id = result["task_id"]
        self.context.ensure_root(task_id)
        return result

    def _do_task_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tasks": self.context.task_service.list(params)}

    def _do_task_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.get(
            params,
            projector=self.context.projector,
            usage_recorder=self.context.usage_recorder,
            artifact_service=self.context.artifact_service,
            adapter=self.context.adapter_for_task(params.get("task_id")),
        )

    def _do_task_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.delete(params)

    def _do_training_start(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.start(params, worker=self.context.worker)

    def _do_training_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.pause(params, worker=self.context.worker)

    def _do_training_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.resume(params, worker=self.context.worker)

    def _do_training_terminate(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.task_service.terminate(params, worker=self.context.worker)

    def _do_report_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.report_service.get(
            params,
            adapter=self.context.adapter_for_task(params.get("task_id")),
        )

    def _do_usage_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.usage_service.get(
            params,
            adapter=self.context.adapter_for_task(params.get("task_id")),
        )

    def _do_artifact_download(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.artifact_download_service.locate(
            params,
            adapter=self.context.adapter_for_task(params.get("task_id")),
        )

    def _do_artifact_files_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.artifact_files_service.list_files(params)

    def _do_artifact_files_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.artifact_files_service.read_file(params)

    def _do_tree_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.context.tree_service.get(
            params,
            adapter=self.context.adapter_for_task(params.get("task_id")),
        )

    def _do_harness_install(self, params: dict[str, Any]) -> Any:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        installer = getattr(self.context, "harness_installer", None)
        if installer is None:
            raise RsiNotReady("RSI Harness installer 未装配")
        return installer.install(task_id)

    def _do_harness_versions_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        installer = getattr(self.context, "harness_installer", None)
        if installer is None:
            raise RsiNotReady("RSI Harness installer 未装配")
        return installer.list_versions()

    def _do_harness_rollback(self, params: dict[str, Any]) -> Any:
        installation_id = str(params.get("installation_id") or "").strip()
        if not installation_id:
            raise RsiBadRequest("installation_id 必填")
        installer = getattr(self.context, "harness_installer", None)
        if installer is None:
            raise RsiNotReady("RSI Harness installer 未装配")
        return installer.rollback(installation_id)

    # -- 推送 --

    def _bind_status_push(self) -> None:
        def _on_status_changed(task_id: str, old: str, new: str) -> None:
            self._push(
                RSI_PUSH_STATUS_CHANGED,
                {
                    "task_id": task_id,
                    "old_status": old,
                    "new_status": new,
                    "status": new,
                },
            )

        self.context.store.set_status_changed_callback(_on_status_changed)

    def _bind_event_push(self) -> None:
        async def _on_progress(event_type: str, task_id: str, payload: dict[str, Any]) -> None:
            self._push(
                event_type,
                {
                    "task_id": task_id,
                    **payload,
                },
            )

        async def _on_tree_delta(event_type: str, task_id: str, payload: dict[str, Any]) -> None:
            self._push(
                event_type,
                {
                    "task_id": task_id,
                    **payload,
                },
            )

        self.context.register_worker_push(
            {
                "rsi.training.progress": _on_progress,
                "rsi.training.tree.delta": _on_tree_delta,
            }
        )

    def _push(self, event_type: str, payload: dict[str, Any]) -> None:
        """统一推送出口（复用 E2A server_push；零改动）。"""
        try:
            message: dict[str, Any] = {
                "channel_id": self.default_channel_id,
                "payload": {
                    "event_type": event_type,
                    **payload,
                },
            }
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                try:
                    task = self.context.store.get(task_id)
                    config = task.config if isinstance(task.config, dict) else {}
                    session_id = str(config.get("rsi_session_id") or "").strip()
                    if session_id:
                        message["session_id"] = session_id
                except Exception:  # noqa: BLE001 - a late push must not break worker
                    pass
            result = self.send_push(message)
            if not inspect.isawaitable(result):
                return
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                logger.warning("[RSI] push 无运行中事件循环，丢弃: %s", event_type)
        except Exception:  # noqa: BLE001
            logger.warning("[RSI] push failed: %s", event_type)
