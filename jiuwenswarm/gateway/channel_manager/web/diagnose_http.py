# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 诊断端点：POST /api/trajectory/sessions/{id}/diagnose → SSE。

薄端点：只做 HTTP 编排（解析请求、读 store 组装 ctx、证据收集、SSE 中继）。
分析阶段走 AgentServer 完整 Agent 流程（设计 v2）：组 E2A chat.send 信封
（channel=diagnosis、一次性 session、project_dir 指向被诊断会话的代码目录），
经 ``agent_client.send_request_stream`` 流式消费，chat.delta 映射为 SSE token。
分析逻辑在 ``jiuwenswarm/observability/diagnosis/``。
仿 ``trajectory_http.py`` 的 ``attach_*_routes`` 模式，挂在 FastAPI app 上。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.observability.config import load_diagnosis_settings
from jiuwenswarm.observability.diagnosis import DiagnosisContext
from jiuwenswarm.observability.diagnosis.agent_client_adapter import (
    DIAGNOSIS_CHANNEL_ID,
    make_diagnosis_session_id,
)
from jiuwenswarm.observability.diagnosis.analyzer import persist_report, run_diagnosis_agent
from jiuwenswarm.observability.diagnosis.budget import cleanup_expired_evidence
from jiuwenswarm.observability.diagnosis.evidence import (
    SpanRecord,
    decode_span_records,
    find_trace_json_files,
    load_trace_json,
)
from jiuwenswarm.observability.diagnosis.upload import UploadValidationError, save_uploaded_logs
from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id

from .trajectory_http import _validate_http_origin

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.trajectory_http import TrajectoryHttpService
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

# 诊断 SSE 空闲超时（秒）：整体诊断可能耗时很久，但连续该时长未收到任何事件
# （token/progress/meta/...）即视为 AgentServer 半开或卡死，主动取消本任务
# （进而中断服务端诊断任务并关闭流），避免前端「诊断中」状态永不收敛。
_DIAGNOSIS_STREAM_IDLE_TIMEOUT_S = 300.0

DIAGNOSE_API_PREFIX = "/api/trajectory"

# 报告 metadata 头（`> key: value`）最多 8 行：persist_report 固定写 6 行；
# 上限防正文本身以 blockquote 开头时被误吞进 header。
_REPORT_HEADER_MAX_LINES = 8


def _bind_diagnosis_request_id(diag_session_id: str) -> None:
    """诊断端点入口绑定 request_id ContextVar（A 类修复）。

    Gateway 进程无 ``bind_incoming_request``（仅 AgentServer handler 有），
    诊断链路日志的 request_id（``diagnosis-{diag_session_id}``）在此唯一绑定。
    与 ``telemetry.request_context._bind_current_request_id`` 同源原语，
    try/except 兜底——环境缺原语时 no-op，不阻断业务。
    """
    try:
        from openjiuwen.extensions.observability.span_context import (
            set_current_request_id,
        )

        set_current_request_id(f"diagnosis-{diag_session_id}")
    except Exception as e:
        logger.warning(f"set diagnosis request id failed: {e}.")
        pass


def _clear_diagnosis_request_id() -> None:
    """与 ``_bind_diagnosis_request_id`` 配对的清理（请求结束）。"""
    try:
        from openjiuwen.extensions.observability.span_context import (
            clear_current_request_id,
        )

        clear_current_request_id()
    except Exception as e:
        logger.warning(f"Clear diagnosis request id failed: {e}.")
        pass


def attach_diagnose_routes(
    app: FastAPI,
    channel: "WebChannel",
    *,
    trajectory_service: "TrajectoryHttpService",
) -> None:
    """挂载诊断端点。复用 trajectory_service 的 reader（数据库路径已解析）。

    AgentServer 访问经 ``channel.agent_client``（``_register_web_handlers``
    装配阶段注入，app_web_handlers.py 同款口径），不在挂载时捕获——
    挂载早于装配，且热重启后引用会更换。
    """
    app.state.diagnose_web_channel = channel

    @app.post(f"{DIAGNOSE_API_PREFIX}/sessions/{{session_id}}/diagnose")
    async def diagnose_session(
        session_id: str,
        request: Request,
    ) -> Response:
        request.state.trajectory_route_handled = True

        # 浏览器 origin 门禁：与 trajectory 读端点同一道防线（跨站请求拒绝）
        origin_error = _validate_http_origin(request)
        if origin_error is not None:
            return origin_error

        # session_id 直接拼路径（诊断产物目录/上传目录），先过同一 path-component
        # 校验（trajectory 系同款），防 ".." 等越目录写。
        if not is_valid_session_id(session_id):
            return _error("invalid session_id", "BAD_REQUEST", 400)

        # 顶层开关：diagnosis.enabled=false 时端点直接拒，避免无谓读 store。
        settings = load_diagnosis_settings()
        if not settings.enabled:
            return _error(
                "diagnosis is disabled (config diagnosis.enabled=false)",
                "DIAGNOSIS_DISABLED",
                403,
            )

        # 解析请求：JSON（无上传）或 multipart（带 log_files[]）。两者互斥。
        fields, uploaded = await _parse_request(request)
        if isinstance(fields, Response):
            return fields

        offline = _truthy(fields.get("offline", False))

        # 会话级门禁（在线模式）：复用 trajectory 系的「会话存在性 + 模式白名单」
        # 校验（Agent/Team 会话）。避免对任意合法格式 session_id 触发 store 查询
        # 与 .trace/diagnosis 目录创建；离线模式仅依赖上传日志，不要求会话元数据，放行。
        if not offline:
            gate_error = trajectory_service.validate_access(session_id, settings)
            if gate_error is not None:
                return gate_error

        # 诊断产物目录与日志目录（在线/离线共用）
        data_dir = _resolve_data_dir(channel)
        diagnosis_dir = data_dir / ".trace" / "diagnosis"
        log_dir = data_dir / "agent" / ".logs"

        # 上传日志落盘（G5）。校验失败返回明确错误。
        uploaded_paths: list[Path] = []
        if uploaded:
            try:
                uploaded_paths = await save_uploaded_logs(
                    uploaded,
                    session_id,
                    diagnosis_dir,
                    max_mb=settings.max_upload_log_mb,
                    allow=settings.allow_log_upload,
                )
            except UploadValidationError as exc:
                return _error(str(exc), "UPLOAD_REJECTED", 400)

        # 上传文件中挑出 trace*.json（含 zip 内解出的），单独解析为 trace 证据；
        # 其余 .json 不视作日志，避免被当成普通日志文件误读。
        trace_files = find_trace_json_files(uploaded_paths)
        log_files = [p for p in uploaded_paths if p.suffix.lower() != ".json"]

        if offline:
            # 离线模式：默认纯日志诊断；若用户同时上传了 trace*.json
            # （含 zip 内解出的），解析为 records 自动升级为 trace 驱动诊断。
            # 仅读取、不抛错：无法解析的 trace 文件回落纯日志诊断。
            trace_id = ""
            records: list[SpanRecord] = []
            for tf in trace_files:
                recs = load_trace_json(tf)
                if recs:
                    records.extend(recs)
                    logger.info(
                        "offline diagnosis loaded trace %s: %d records", tf.name, len(recs)
                    )
            if records:
                records.sort(key=lambda r: r.start_time_unix_nano)
                trace_id = records[0].trace_id

            if not uploaded_paths and not _truthy(fields.get("include_logs", False)):
                return _error(
                    "离线诊断必须提供日志或 trace 文件（上传文件或目录导入）",
                    "OFFLINE_NO_LOGS",
                    400,
                )
        else:
            # 在线模式：读 trajectory store，取该 session 最近一条 trace 的全部 records
            reader = trajectory_service.reader
            try:
                traces, _ = await reader.list_traces(session_id, limit=1, cursor=None)
            except Exception:
                logger.exception("diagnose list_traces failed: session=%s", session_id)
                return _error("trajectory query failed", "TRAJECTORY_QUERY_FAILED", 500)

            if not traces:
                return _error("no trace found for session", "NO_TRACE", 404)

            trace_id = fields.get("trace_id") or str(traces[0].get("trace_id") or "")
            if not trace_id:
                return _error("trace_id missing", "BAD_REQUEST", 400)

            try:
                archive, _epoch, _rev = await reader.get_session_archive_records(session_id)
            except Exception:
                logger.exception("diagnose archive read failed: session=%s", session_id)
                return _error("trajectory archive read failed", "TRAJECTORY_QUERY_FAILED", 500)

            records = decode_span_records(
                r for r in archive if str(r.get("trace_id") or "") == trace_id
            )
            if not records:
                return _error("no span records for trace", "NO_TRACE", 404)

        ctx = DiagnosisContext(
            session_id=session_id,
            trace_id=trace_id,
            user_note=fields.get("user_note"),
            requested_mode=fields.get("mode"),
            records=records,
            log_dir=log_dir if _truthy(fields.get("include_logs", True)) else None,
            uploaded_logs=log_files,
        )

        # 诊断 Agent 工作目录 = 被诊断会话的 project_dir（代码检索落在出问题的
        # 代码仓）；读不到（纯聊天会话/分离部署不共享数据目录）回落默认工作区。
        project_dir, agent_mode = _resolve_session_workspace(session_id)

        # AgentServer 访问（唯一分析路径，无降级）：装配阶段未注入时直接拒。
        # 断连由 send_request_stream 抛错转 AGENT_UNREACHABLE（见 event_stream）。
        agent_client = getattr(channel, "agent_client", None)
        if agent_client is None:
            return _error(
                "AgentServer client unavailable (diagnosis requires AgentServer)",
                "AGENT_UNREACHABLE",
                503,
            )

        async def event_stream():
            report_chunks: list[str] = []
            final_text = ""
            window = (0, 0)
            evidence_path = ""
            # 提前生成 diag_session_id：供 set request_id ContextVar + 注入
            # session_id_factory，保证信封 request_id（diagnosis-{session_id}）
            # 与日志 request_id 完全一致。Gateway 进程无 bind_incoming_request，
            # 此处为诊断链路唯一的 request_id 绑定点（A 类修复）。
            diag_session_id = make_diagnosis_session_id()
            _bind_diagnosis_request_id(diag_session_id)
            # 空闲超时守卫：连续 _DIAGNOSIS_STREAM_IDLE_TIMEOUT_S 秒未收到任何事件，
            # 判定 AgentServer 半开/卡死，取消本任务（进而中断服务端诊断并关闭流）。
            loop = asyncio.get_running_loop()
            idle_task = asyncio.current_task()
            idle_handle = None

            def _on_idle() -> None:
                if idle_task is not None:
                    idle_task.cancel()

            def _schedule_idle() -> None:
                nonlocal idle_handle
                if idle_handle is not None:
                    idle_handle.cancel()
                idle_handle = loop.call_later(_DIAGNOSIS_STREAM_IDLE_TIMEOUT_S, _on_idle)

            _schedule_idle()
            try:
                try:
                    async for event in run_diagnosis_agent(
                        agent_client,
                        ctx,
                        diagnosis_dir=diagnosis_dir,
                        include_logs=settings.include_logs
                        and (bool(ctx.log_dir) or bool(ctx.uploaded_logs)),
                        history_summary_records=settings.history_summary_records,
                        project_dir=project_dir,
                        mode=agent_mode,
                        session_id_factory=lambda: diag_session_id,
                    ):
                        _schedule_idle()
                        etype = event.get("type")
                        if etype == "meta":
                            # 内部元事件：报告落盘/取消中断所需，不透传前端
                            evidence_path = str(event.get("evidence_path") or "")
                            window_hint = event.get("window")
                            if isinstance(window_hint, (list, tuple)) and len(window_hint) == 2:
                                window = (int(window_hint[0]), int(window_hint[1]))
                            diag_session_id = str(event.get("diag_session_id") or "")
                            continue
                        if etype == "token":
                            report_chunks.append(event.get("text") or "")
                        elif etype == "final":
                            final_text = str(event.get("text") or "")
                        yield {
                            "event": etype or "message",
                            "data": json.dumps(event, ensure_ascii=False),
                        }
                        if etype == "error":
                            return
                except asyncio.CancelledError:
                    # SSE 断开（用户取消/空闲超时）：向 AgentServer 发 chat.interrupt
                    # 终止诊断 Agent 任务。当前协程已在取消中，中断必须 fire-and-forget
                    # （新任务发送），不能 await——否则取消路径自身被挂起。
                    if idle_handle is not None:
                        idle_handle.cancel()
                    if diag_session_id:
                        _spawn_interrupt(agent_client, diag_session_id)
                    raise
                except Exception as exc:
                    # AgentServer 不可达/断连：无降级路径，显式报错。
                    logger.exception(
                        "diagnosis agent stream failed: session=%s", session_id
                    )
                    yield {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "type": "error",
                                "message": f"AgentServer 分析失败：{exc}（证据包已落盘 {evidence_path or '见 .trace/diagnosis'}）",
                            },
                            ensure_ascii=False,
                        ),
                    }
                    return
                finally:
                    if idle_handle is not None:
                        idle_handle.cancel()

                # chat.final 兜底：deep 适配器末轮可能只发 final 不发 delta，
                # report_chunks 为空时用 final 全文补一次 token（前端流式渲染依赖）。
                if not report_chunks and final_text:
                    report_chunks.append(final_text)
                    yield {
                        "event": "token",
                        "data": json.dumps(
                            {"type": "token", "text": final_text}, ensure_ascii=False
                        ),
                    }

                report_text = "".join(report_chunks)
                if not report_text.strip():
                    yield {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "type": "error",
                                "message": (
                                    "诊断 Agent 无输出（AgentServer 流式响应为空）。"
                                    "请检查 AgentServer 日志后重试。"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    }
                    return

                report_path = persist_report(
                    diagnosis_dir,
                    ctx.session_id,
                    report_text,
                    evidence_path=evidence_path,
                    trace_id=ctx.trace_id,
                    window=window,
                )
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "type": "done",
                            "report_path": report_path,
                            "evidence_path": evidence_path,
                        },
                        ensure_ascii=False,
                    ),
                }

                # 顺带清理过期诊断产物（best-effort，不抛、不影响报告）
                if settings.keep_evidence_days > 0:
                    try:
                        cleanup_expired_evidence(diagnosis_dir, settings.keep_evidence_days)
                    except Exception:
                        logger.debug("diagnosis evidence cleanup skipped", exc_info=True)
            finally:
                _clear_diagnosis_request_id()

        return EventSourceResponse(event_stream())

    @app.get(f"{DIAGNOSE_API_PREFIX}/sessions/{{session_id}}/diagnose/reports")
    async def list_diagnosis_reports(session_id: str, request: Request) -> Response:
        """列出该 session 的所有历史诊断报告，按生成时间倒序。"""
        request.state.trajectory_route_handled = True
        gate = _report_access_gate(request, session_id)
        if isinstance(gate, Response):
            return gate
        data_dir = _resolve_data_dir(channel)
        session_dir = data_dir / ".trace" / "diagnosis" / session_id
        reports: list[dict[str, Any]] = []
        if session_dir.is_dir():
            for path in sorted(session_dir.glob("report-*.md"), key=lambda p: p.name, reverse=True):
                report_id = path.stem.removeprefix("report-")
                meta = _parse_report_meta(path)
                reports.append({
                    "report_id": report_id,
                    "session_id": session_id,
                    "trace_id": meta["trace_id"],
                    "created_at": meta["generated_at"],
                    "summary": meta["summary"],
                    "report_path": str(path),
                    "evidence_path": meta["evidence_path"],
                })
        return JSONResponse({"reports": reports}, headers={"Cache-Control": "no-store"})

    @app.get(
        f"{DIAGNOSE_API_PREFIX}/sessions/{{session_id}}/diagnose/reports/{{report_id}}"
    )
    async def read_diagnosis_report(session_id: str, report_id: str, request: Request) -> Response:
        """读取单个历史诊断报告全文。"""
        request.state.trajectory_route_handled = True
        gate = _report_access_gate(request, session_id)
        if isinstance(gate, Response):
            return gate
        data_dir = _resolve_data_dir(channel)
        path = data_dir / ".trace" / "diagnosis" / session_id / f"report-{report_id}.md"
        if not path.is_file():
            return _error("report not found", "NOT_FOUND", 404)
        meta = _parse_report_meta(path)
        return JSONResponse(
            {
                "report_id": report_id,
                "session_id": session_id,
                "trace_id": meta["trace_id"],
                "created_at": meta["generated_at"],
                "window": meta["window"],
                "report_path": str(path),
                "evidence_path": meta["evidence_path"],
                "report_text": meta["body_text"],
            },
            headers={"Cache-Control": "no-store"},
        )


def _resolve_session_workspace(session_id: str) -> tuple[str, str]:
    """读被诊断会话 metadata，返回 (project_dir, mode) 供诊断 Agent 信封。

    读不到（会话不存在/分离部署不共享数据目录）返回默认 (\"\", \"agent\")——
    AgentServer 侧按默认工作区兜底。mode 非 agent/team 的值归一为 agent，
    诊断不需要复刻 code.* 等执行形态。
    """
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        metadata = get_session_metadata(session_id, enable_writeback=False) or {}
        project_dir = str(metadata.get("project_dir") or "").strip()
        mode = str(metadata.get("mode") or "").strip().lower()
        if mode not in ("agent", "team"):
            mode = "agent"
        return project_dir, mode
    except Exception:
        logger.debug(
            "diagnosis session metadata load failed: session=%s", session_id, exc_info=True
        )
        return "", "agent"


def _spawn_interrupt(agent_client: Any, diag_session_id: str) -> None:
    """SSE 断开后向 AgentServer 发 chat.interrupt（fire-and-forget，失败仅告警）。

    对齐 cron 的 ``_cancel_agent_session`` 口径：AgentServer 按 session_id
    定位 in-flight task，诊断 session 为一次性，中断后随 oneshot 回收。
    调用方处于 CancelledError 处理中，不能 await——开独立任务发送。
    """
    async def _send() -> None:
        try:
            interrupt_env = e2a_from_agent_fields(
                request_id=f"diagnosis-cancel-{diag_session_id}",
                channel_id=DIAGNOSIS_CHANNEL_ID,
                session_id=diag_session_id,
                req_method=ReqMethod.CHAT_CANCEL,
                params={"intent": "cancel", "session_id": diag_session_id},
                is_stream=False,
            )
            await agent_client.send_request(interrupt_env)
            logger.info(
                "[Diagnosis] AgentServer interrupt sent: session_id=%s", diag_session_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diagnosis] AgentServer interrupt failed (non-critical): "
                "session_id=%s error=%s",
                diag_session_id,
                exc,
            )

    try:
        task = asyncio.get_running_loop().create_task(_send())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    except RuntimeError:
        logger.debug("[Diagnosis] interrupt spawn skipped: no running loop")


def _report_access_gate(request: Request, session_id: str) -> Response | None:
    """历史报告 GET 端点共用门禁：origin + session_id 格式 + 顶层开关。

    与 POST /diagnose 同一套防线——历史报告与诊断同敏感级（含日志证据摘要），
    关闭诊断后也不应继续可读。
    """
    origin_error = _validate_http_origin(request)
    if origin_error is not None:
        return origin_error
    if not is_valid_session_id(session_id):
        return _error("invalid session_id", "BAD_REQUEST", 400)
    settings = load_diagnosis_settings()
    if not settings.enabled:
        return _error(
            "diagnosis is disabled (config diagnosis.enabled=false)",
            "DIAGNOSIS_DISABLED",
            403,
        )
    return None


def _parse_report_meta(path: Path) -> dict[str, Any]:
    """从落盘报告的 metadata 头解析 trace_id/window/evidence/generated_at/summary，
    并剥离 metadata 头返回正文 body_text（供前端直接渲染，不暴露 `> key: value` 头）。

    头部格式见 analyzer.persist_report：`> key: value` 行，空行结束。
    """
    meta: dict[str, Any] = {
        "trace_id": "",
        "window": None,
        "evidence_path": "",
        "generated_at": "",
        "summary": "",
        "body_text": "",
    }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return meta
    header_lines: list[str] = []
    body_start = 0
    for idx, line in enumerate(text.splitlines()):
        if line.startswith("> ") and len(header_lines) < _REPORT_HEADER_MAX_LINES:
            header_lines.append(line[2:])
            body_start = idx + 1
        elif line.strip() == "" and header_lines:
            body_start = idx + 1
            break
        elif header_lines:
            break
    for line in header_lines:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "trace":
                meta["trace_id"] = value
            elif key == "window":
                meta["window"] = value
            elif key == "evidence":
                meta["evidence_path"] = value
            elif key == "generated_at":
                meta["generated_at"] = value
    # 正文：剥离 metadata 头后的全部内容（去掉头部空行）
    body_text = "\n".join(text.splitlines()[body_start:]).lstrip("\n")
    meta["body_text"] = body_text
    # summary：正文第一行非空文本，截 80 字
    for line in body_text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            meta["summary"] = stripped[:80]
            break
    return meta


async def _parse_request(request: Request) -> tuple[dict, list] | Response:
    """解析请求体。返回 (fields, uploaded_files)。

    JSON body → (fields, [])；multipart/form-data → (form fields, log_files[])。
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception:
            return _error("invalid multipart form", "BAD_REQUEST", 400)
        fields: dict[str, Any] = {}
        for key in ("trace_id", "user_note", "mode", "include_logs", "model", "offline"):
            value = form.get(key)
            if value is not None:
                fields[key] = value
        uploaded = form.getlist("log_files")
        return fields, list(uploaded)
    # 默认 JSON
    try:
        raw = await request.body()
    except Exception:
        return _error("invalid request body", "BAD_REQUEST", 400)
    if not raw:
        return {}, []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("request body must be valid JSON", "BAD_REQUEST", 400)
    if not isinstance(data, dict):
        return _error("request body must be a JSON object", "BAD_REQUEST", 400)
    return data, []


def _resolve_data_dir(channel: "WebChannel") -> Path:
    """解析运行时数据根（~/.jiuwenswarm/，可被 JIUWENSWARM_DATA_DIR 覆盖）。"""
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return Path(get_user_workspace_dir())


def _truthy(value: Any) -> bool:
    """布尔解析：兼容 multipart 表单的字符串 'true'/'false' 与 JSON 布尔值。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
