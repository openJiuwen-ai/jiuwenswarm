"""Helpers for routing single-agent External Harness sessions."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.resources import RuntimeSpec, load_agent_template_package
from openjiuwen.harness_protocol import (
    HarnessCapability,
    HarnessCheckpoint,
    HarnessState,
    ResumePolicy,
    UnsupportedHarnessCapabilityError,
    json_value_to_builtin,
)
from openjiuwen.harness_providers import (
    HarnessIOAdapter,
    build_harness_context,
    create_harness,
)
from openjiuwen.harness_providers.io_adapter import TURN_LIFECYCLE

from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.utils import (
    get_default_project_session_workspace_dir,
    mask_sensitive,
)
from jiuwenswarm.server.runtime import extension_package_manager as equipment
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_metadata,
    update_session_metadata,
)

logger = logging.getLogger(__name__)
_BINDING_LOCK = threading.RLock()
_EXTERNAL_HARNESS_KIND = "external_harness"
_TERMINAL_KINDS = frozenset({"finished", "failed", "aborted"})
_PROVIDER_TO_CLI_AGENT = {
    "claudecode": "claude",
    "codex": "codex",
}
_CLI_AGENT_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
}
_ERROR_TEXT: dict[str, tuple[str, str]] = {
    "template_name_type": (
        "agent_template_name 必须是字符串",
        "agent_template_name must be a string",
    ),
    "binding_conflict": (
        "当前会话已绑定三方专家 {template!r}（provider={provider!r}）；请新建会话后切换专家",
        "This session is already bound to external expert {template!r} "
        "(provider={provider!r}); start a new session to switch experts",
    ),
    "cli_not_enabled": (
        "请先在配置界面启用 {label}，再使用该三方专家",
        "Enable {label} in settings before using this external expert",
    ),
    "invalid_runtime_config": (
        "runtime.config 无效: {detail}",
        "runtime.config is invalid: {detail}",
    ),
    "binding_missing": (
        "缺少 External Harness 绑定",
        "External Harness binding is missing",
    ),
    "no_pending_interaction": (
        "当前没有可恢复的交互请求",
        "No interaction is waiting for an answer",
    ),
    "waiting_for_interaction": (
        "任务正在等待交互输入，请先回答交互或取消当前任务",
        "The task is waiting for an answer; reply to the question or cancel it",
    ),
    "task_running": (
        "任务正在进行中，请先取消后再发送新消息",
        "A task is still running; cancel it before sending a new message",
    ),
    "startup_failed": (
        "External Harness 启动失败",
        "External Harness startup failed",
    ),
    "input_rejected": (
        "External Harness 已拒绝该输入",
        "External Harness input was rejected",
    ),
    "turn_not_accepted": (
        "External Harness 未受理新的一轮",
        "External Harness did not accept a new turn",
    ),
    "turn_failed": (
        "External Harness 本轮执行失败",
        "External Harness turn failed",
    ),
    "stream_failed": (
        "External Harness 输出流失败",
        "External Harness output stream failed",
    ),
    "stream_closed": (
        "External Harness 输出流在本轮完成前关闭",
        "External Harness output stream closed before turn completion",
    ),
    "unsupported_intent": (
        "不支持中断意图: {intent}",
        "Unsupported interrupt intent: {intent}",
    ),
    "nothing_to_cancel": (
        "当前没有可取消的 External Harness 任务",
        "There is no External Harness task to cancel",
    ),
    "cancel_unsupported": (
        "当前 External Harness 不支持取消",
        "This External Harness cannot be cancelled",
    ),
    "cancel_failed": (
        "External Harness 取消失败",
        "External Harness cancel failed",
    ),
    "cancel_requested": (
        "已请求取消 External Harness 任务",
        "External Harness cancel requested",
    ),
}


class ExternalHarnessBindingError(ValueError):
    """A request conflicts with the immutable runtime kind of its session."""


def _error_text(key: str, **values: object) -> str:
    zh_text, en_text = _ERROR_TEXT[key]
    use_en = False
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
        if isinstance(config, Mapping):
            use_en = str(config.get("preferred_language") or "").strip().lower() == "en"
    except Exception:
        logger.debug("read preferred_language failed, fallback to zh", exc_info=True)
    return (en_text if use_en else zh_text).format(**values)


def _requested_template_name(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    value = params.get("agent_template_name")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(_error_text("template_name_type"))
    return value.strip() or None


def _load_requested_runtime(
    agent_template_name: str,
) -> tuple[str, Path, RuntimeSpec | None]:
    runtime_name = equipment.resolve_equipment_runtime_id(
        "agent_templates",
        agent_template_name,
    )
    package_path = equipment.resolve_agent_template_dir(runtime_name)
    manifest = load_agent_template_package(package_path / "manifest.json")
    return runtime_name, package_path, equipment.agent_template_runtime(manifest)


def _binding_conflict_error(
    bound_template_name: str,
    bound_provider_name: str,
) -> ExternalHarnessBindingError:
    return ExternalHarnessBindingError(
        _error_text(
            "binding_conflict",
            template=bound_template_name,
            provider=bound_provider_name,
        )
    )


def resolve_external_harness_binding(
    session_id: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve and enforce one session's immutable External Harness binding."""
    requested_name = _requested_template_name(params)

    with _BINDING_LOCK:
        metadata = get_session_metadata(session_id, cache_bust=True)
        stored = metadata.get("session_runtime_binding")
        if isinstance(stored, dict) and stored.get("kind") == _EXTERNAL_HARNESS_KIND:
            bound_name = str(stored.get("agent_template_name") or "")
            bound_provider = str(stored.get("provider_name") or "")
            if not requested_name:
                raise _binding_conflict_error(bound_name, bound_provider)

            runtime_name, package_path, runtime = _load_requested_runtime(requested_name)
            if (
                runtime is None
                or runtime_name != bound_name
                or runtime.provider_name != bound_provider
            ):
                raise _binding_conflict_error(bound_name, bound_provider)
            return {
                "kind": _EXTERNAL_HARNESS_KIND,
                "agent_template_name": runtime_name,
                "provider_name": runtime.provider_name,
                "runtime": runtime,
                "package_path": package_path,
            }

        if not requested_name:
            return {"kind": "deep_agent"}

        runtime_name, package_path, runtime = _load_requested_runtime(requested_name)
        if runtime is None:
            return {"kind": "deep_agent"}

        binding = {
            "kind": _EXTERNAL_HARNESS_KIND,
            "agent_template_name": runtime_name,
            "provider_name": runtime.provider_name,
        }
        update_session_metadata(
            session_id=session_id,
            session_runtime_binding=binding,
            touch_last_message_at=False,
            cache_bust=True,
            sync_write=True,
        )
        return {
            **binding,
            "runtime": runtime,
            "package_path": package_path,
        }


def load_persisted_external_harness_binding(
    session_id: str,
) -> dict[str, Any] | None:
    """Return the stored External binding, including runtime and package path."""
    metadata = get_session_metadata(session_id, cache_bust=True)
    stored = metadata.get("session_runtime_binding")
    if not isinstance(stored, dict) or stored.get("kind") != _EXTERNAL_HARNESS_KIND:
        return None
    template_name = str(stored.get("agent_template_name") or "").strip()
    if not template_name:
        return None
    return resolve_external_harness_binding(
        session_id,
        {"agent_template_name": template_name},
    )


def _runtime_config(runtime: RuntimeSpec | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, RuntimeSpec):
        return dict(runtime.config)
    config = runtime.get("config")
    return dict(config) if isinstance(config, Mapping) else {}


def _host_external_cli_agents() -> list[Any]:
    from jiuwenswarm.common.config import EXTERNAL_CLI_AGENTS_CONFIG_PATH, get_config

    current: Any = get_config()
    for segment in EXTERNAL_CLI_AGENTS_CONFIG_PATH:
        if not isinstance(current, Mapping):
            return []
        current = current.get(segment)
    return list(current) if isinstance(current, list) else []


def _host_cli_agent_entry(cli_agent: str) -> Mapping[str, Any] | None:
    for item in _host_external_cli_agents():
        if item == cli_agent:
            return {"cli_agent": cli_agent}
        if isinstance(item, Mapping) and str(item.get("cli_agent") or "") == cli_agent:
            return item
    return None


def _merge_host_external_cli_config(
    provider_name: str,
    package_config: dict[str, Any],
) -> dict[str, Any]:
    cli_agent = _PROVIDER_TO_CLI_AGENT.get(provider_name)
    if cli_agent is None:
        return package_config
    entry = _host_cli_agent_entry(cli_agent)
    if entry is None:
        raise ValueError(
            _error_text("cli_not_enabled", label=_CLI_AGENT_LABELS[cli_agent])
        )
    from jiuwenswarm.common.external_cli_runtime import activate_external_cli_runtime_paths

    activate_external_cli_runtime_paths()
    merged = dict(package_config)
    # Manifest keys win, including an explicit false or null.
    if provider_name == "codex":
        merged.setdefault("bypass_approvals_and_sandbox", True)
        merged.setdefault("system_prompt_mode", "replace")
    elif provider_name == "claudecode":
        from openjiuwen.harness_providers.claudecode.config import (
            DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
        )

        merged.setdefault("permission_mode", "bypassPermissions")
        merged.setdefault("system_prompt_mode", "append")
        merged.setdefault("max_buffer_size", DEFAULT_CLAUDE_MAX_BUFFER_SIZE)
    cli_path = str(entry.get("cli_path") or "").strip()
    if cli_path:
        merged.setdefault("cli_path", cli_path)
    return merged


def _supports_checkpoint(harness: Any) -> bool:
    card = getattr(harness, "card", None)
    supports = getattr(card, "supports", None)
    return callable(supports) and bool(supports(HarnessCapability.CHECKPOINT))


def _load_external_harness_checkpoint(
    *,
    session_id: str,
    agent_template_name: str,
    provider_name: str,
) -> HarnessCheckpoint | None:
    stored = get_session_metadata(session_id, cache_bust=True).get(
        "session_harness_checkpoint"
    )
    if not isinstance(stored, dict) or (
        stored.get("agent_template_name") != agent_template_name
        or stored.get("provider_name") != provider_name
    ):
        return None
    payload = stored.get("checkpoint")
    if not isinstance(payload, dict):
        return None
    try:
        checkpoint = HarnessCheckpoint(
            provider=str(payload["provider"]),
            schema_version=str(payload["schema_version"]),
            agent_id=str(payload["agent_id"]),
            host_session_id=str(payload["host_session_id"]),
            checkpoint_id=str(payload["checkpoint_id"]),
            sequence=int(payload["sequence"]),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            provider_session_id=payload.get("provider_session_id"),
            revision=payload.get("revision"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Ignoring invalid External Harness checkpoint for session %s: %s",
            session_id,
            _visible_error(exc, "invalid checkpoint"),
        )
        return None
    expected_agent_id = (
        f"external_harness:{provider_name}:{agent_template_name}"
    )
    if (
        checkpoint.agent_id != expected_agent_id
        or checkpoint.host_session_id != session_id
    ):
        logger.warning(
            "Ignoring out-of-scope External Harness checkpoint for session %s",
            session_id,
        )
        return None
    return checkpoint


def _checkpoint_metadata(
    checkpoint: HarnessCheckpoint,
    *,
    agent_template_name: str,
    provider_name: str,
) -> dict[str, Any]:
    return {
        "agent_template_name": agent_template_name,
        "provider_name": provider_name,
        "checkpoint": {
            "provider": checkpoint.provider,
            "schema_version": checkpoint.schema_version,
            "agent_id": checkpoint.agent_id,
            "host_session_id": checkpoint.host_session_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "sequence": checkpoint.sequence,
            "data": json_value_to_builtin(checkpoint.data),
            "provider_session_id": checkpoint.provider_session_id,
            "revision": checkpoint.revision,
        },
    }


async def _persist_external_harness_checkpoint(
    adapter: Any,
    io: HarnessIOAdapter,
    session_id: str,
) -> None:
    binding = getattr(adapter, "_external_harness_binding", None)
    harness = getattr(io, "harness", None)
    if not isinstance(binding, dict) or not _supports_checkpoint(harness):
        return
    try:
        checkpoint = await harness.export_checkpoint()
        if checkpoint is None:
            return
        agent_template_name = str(binding["agent_template_name"])
        provider_name = str(binding["provider_name"])
        expected_agent_id = (
            f"external_harness:{provider_name}:{agent_template_name}"
        )
        if (
            checkpoint.agent_id != expected_agent_id
            or checkpoint.host_session_id != session_id
        ):
            logger.warning(
                "Skipping out-of-scope External Harness checkpoint for session %s",
                session_id,
            )
            return
        update_session_metadata(
            session_id=session_id,
            session_harness_checkpoint=_checkpoint_metadata(
                checkpoint,
                agent_template_name=agent_template_name,
                provider_name=provider_name,
            ),
            touch_last_message_at=False,
            cache_bust=True,
            sync_write=True,
        )
    except Exception as exc:
        logger.warning(
            "External Harness checkpoint export failed for session %s: %s",
            session_id,
            _visible_error(exc, "checkpoint export failed"),
        )


async def start_external_harness(
    *,
    session_id: str,
    agent_template_name: str,
    provider_name: str,
    runtime: RuntimeSpec | Mapping[str, Any],
    package_path: str | Path,
    language: str,
    cwd: str | Path,
    env: Mapping[str, str],
) -> HarnessIOAdapter:
    """Create and start one session-owned External Harness adapter."""
    config = _merge_host_external_cli_config(provider_name, _runtime_config(runtime))
    try:
        harness = create_harness(
            manifest=package_path,
            provider=provider_name,
            config=config,
            language=language,
        )
    except ValueError as exc:
        raise ValueError(_error_text("invalid_runtime_config", detail=exc)) from exc
    checkpoint = (
        _load_external_harness_checkpoint(
            session_id=session_id,
            agent_template_name=agent_template_name,
            provider_name=provider_name,
        )
        if _supports_checkpoint(harness)
        else None
    )
    context = build_harness_context(
        manifest=package_path,
        provider=provider_name,
        host_session_id=session_id,
        agent_id=f"external_harness:{provider_name}:{agent_template_name}",
        agent_name=agent_template_name,
        language=language,
        cwd=str(cwd),
        env=dict(env),
        resume_policy=(
            ResumePolicy.RESUME_IF_AVAILABLE
            if checkpoint is not None
            else ResumePolicy.NEW
        ),
        checkpoint=checkpoint,
    )
    io = HarnessIOAdapter(harness, emit_turn_lifecycle=True)
    await io.start(context)
    return io


async def stop_external_harness(adapter: Any) -> None:
    """Stop and detach one session-owned External Harness adapter."""
    io = getattr(adapter, "_external_harness", None)
    if io is None:
        return
    session_id = str(getattr(adapter, "_parent_session_id", None) or "")
    if session_id:
        await _persist_external_harness_checkpoint(adapter, io, session_id)
    await io.stop()
    adapter._external_harness = None  # pylint: disable=protected-access


async def stream_turn_output(
    io: HarnessIOAdapter,
    receipt_turn_id: str | None,
    *,
    resume: bool,
) -> AsyncIterator[OutputSchema]:
    """Yield only the current request's ordered output through its boundary."""
    own = resume
    async for chunk in io.outputs():
        if chunk.type == TURN_LIFECYCLE:
            payload = chunk.payload if isinstance(chunk.payload, dict) else {}
            kind = payload.get("kind")
            if kind == "started":
                own = payload.get("turn_id") == receipt_turn_id
                continue
            if own and kind in _TERMINAL_KINDS:
                yield chunk
                return
            logger.debug(
                "Skipping External Harness lifecycle marker outside current turn"
            )
            continue
        if not own:
            logger.debug("Skipping External Harness output outside current turn")
            continue
        yield chunk
        if chunk.type == INTERACTION:
            return


def _response_chunk(
    request: AgentRequest,
    payload: dict[str, Any],
    *,
    complete: bool,
    runtime_completion: str | None = None,
) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request.request_id,
        channel_id=request.channel_id,
        payload=payload,
        is_complete=complete,
        metadata=request.metadata if isinstance(request.metadata, dict) else {},
        runtime_completion=runtime_completion,
    )


def _visible_error(error: object, fallback: str) -> str:
    message = mask_sensitive(str(error)).strip()
    return message or fallback


def _bind_external_workspace(adapter: Any, inputs: dict[str, Any]) -> None:
    """Adopt the conversation project once; an existing session root stays put."""
    if str(getattr(adapter, "_project_dir", "") or "").strip():
        return
    project_dir = inputs.get("project_dir")
    if not isinstance(project_dir, str):
        return
    selected = project_dir.strip()
    if selected:
        adapter._project_dir = selected  # pylint: disable=protected-access


def _external_workspace(adapter: Any, session_id: str) -> Path:
    project_dir = str(getattr(adapter, "_project_dir", "") or "").strip()
    if project_dir:
        return Path(project_dir).expanduser().resolve(strict=False)
    return get_default_project_session_workspace_dir(session_id).resolve(strict=False)


async def _ensure_external_harness(
    adapter: Any,
    request: AgentRequest,
) -> HarnessIOAdapter:
    existing = getattr(adapter, "_external_harness", None)
    if existing is not None:
        return existing

    binding = getattr(adapter, "_external_harness_binding", None)
    if not isinstance(binding, dict):
        raise ValueError(_error_text("binding_missing"))
    session_id = str(
        getattr(adapter, "_parent_session_id", None)
        or request.session_id
        or "default"
    )
    language_resolver = getattr(adapter, "_resolve_runtime_language", None)
    language = language_resolver() if callable(language_resolver) else "zh"
    io = await start_external_harness(
        session_id=session_id,
        agent_template_name=str(binding["agent_template_name"]),
        provider_name=str(binding["provider_name"]),
        runtime=binding["runtime"],
        package_path=binding["package_path"],
        language=str(language),
        cwd=_external_workspace(adapter, session_id),
        env=dict(os.environ),
    )
    adapter._external_harness = io  # pylint: disable=protected-access
    return io


def _external_question_options(raw_options: Any) -> list[dict[str, str]]:
    if not isinstance(raw_options, list):
        return []
    options: list[dict[str, str]] = []
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "").strip()
        if not label:
            continue
        normalized = {"label": label}
        description = str(option.get("description") or "").strip()
        if description:
            normalized["description"] = description
        options.append(normalized)
    if options:
        options.append({"label": "Other", "description": "Custom input"})
    return options


def _external_ask_questions(value: dict[str, Any]) -> list[dict[str, Any]]:
    provider_data = value.get("provider_data")
    raw_questions: Any = None
    if isinstance(provider_data, dict):
        raw_questions = provider_data.get("questions")
        tool_input = provider_data.get("input")
        if not isinstance(raw_questions, list) and isinstance(tool_input, dict):
            raw_questions = tool_input.get("questions")
    questions: list[dict[str, Any]] = []
    if isinstance(raw_questions, list):
        for item in raw_questions:
            if not isinstance(item, dict):
                continue
            text = str(item.get("question") or "").strip()
            if not text:
                continue
            multi_select = item.get("multi_select")
            if multi_select is None:
                multi_select = item.get("multiSelect", False)
            questions.append(
                {
                    "question": text,
                    "header": str(item.get("header") or "").strip() or "Question",
                    "options": _external_question_options(item.get("options")),
                    "multi_select": bool(multi_select),
                }
            )
    if questions:
        return questions
    prompt = str(value.get("prompt") or "").strip() or "需要你的回答"
    options: list[dict[str, str]] = []
    choices = value.get("choices")
    if isinstance(choices, (list, tuple)):
        for choice in choices:
            label = str(choice or "").strip()
            if label:
                options.append({"label": label})
    return [
        {
            "question": prompt,
            "header": "需要你的回答",
            "options": options,
            "multi_select": False,
        }
    ]


def _project_external_user_input(chunk: OutputSchema) -> dict[str, Any] | None:
    """Project a harness user-input interrupt onto one ask-user card per question."""
    if chunk.type != INTERACTION:
        return None
    payload = chunk.payload
    if hasattr(payload, "id") and hasattr(payload, "value"):
        request_id = str(getattr(payload, "id", "") or "").strip()
        value = payload.value
    elif isinstance(payload, dict):
        request_id = str(payload.get("id", "") or "").strip()
        value = payload.get("value")
    else:
        return None
    if not isinstance(value, dict) or value.get("kind") != "user_input":
        return None
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": _external_ask_questions(value),
        "source": "ask_user_interrupt",
    }


def _parse_external_chunk(chunk: OutputSchema, *, has_streamed_answer: bool) -> dict[str, Any] | None:
    projected = _project_external_user_input(chunk)
    if projected is not None:
        return projected
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    return JiuWenSwarmDeepAdapter.parse_stream_chunk(
        chunk,
        has_streamed_content=has_streamed_answer,
    )


async def process_external_harness_stream(
    adapter: Any,
    request: AgentRequest,
    inputs: dict[str, Any],
) -> AsyncIterator[AgentResponseChunk]:
    """Run one External Harness request and project its ordered chat stream."""
    _bind_external_workspace(adapter, inputs)
    try:
        io = await _ensure_external_harness(adapter, request)
    except Exception as exc:
        logger.warning(
            "External Harness startup failed for session %s: %s",
            request.session_id,
            _visible_error(exc, "startup failed"),
        )
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": _visible_error(exc, _error_text("startup_failed")),
            },
            complete=True,
        )
        return

    query = inputs.get("query")
    resume = isinstance(query, InteractiveInput)
    if resume and not io.is_pending_interrupt_resume_valid(query):
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": _error_text("no_pending_interaction"),
            },
            complete=True,
        )
        return
    if not resume and io.state is HarnessState.RUNNING:
        pending_interaction = io.has_pending_interrupt()
        message = _error_text(
            "waiting_for_interaction" if pending_interaction else "task_running"
        )
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": message,
            },
            complete=True,
        )
        return

    try:
        receipt = await io.send(query)
    except Exception as exc:
        logger.warning(
            "External Harness input was rejected for session %s: %s",
            request.session_id,
            _visible_error(exc, "input rejected"),
        )
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": _visible_error(exc, _error_text("input_rejected")),
            },
            complete=True,
        )
        return

    if not resume and receipt is None:
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": _error_text("turn_not_accepted"),
            },
            complete=True,
        )
        return

    has_streamed_answer = False
    receipt_turn_id = None if receipt is None else receipt.turn_id
    try:
        async for chunk in stream_turn_output(
            io,
            receipt_turn_id,
            resume=resume,
        ):
            if chunk.type == TURN_LIFECYCLE:
                payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                kind = str(payload.get("kind") or "")
                await _persist_external_harness_checkpoint(
                    adapter,
                    io,
                    request.session_id,
                )
                if kind == "finished":
                    final_output = payload.get("final_output")
                    content = final_output if isinstance(final_output, str) else ""
                    yield _response_chunk(
                        request,
                        {"event_type": "chat.final", "content": content},
                        complete=True,
                        runtime_completion="completed",
                    )
                elif kind == "failed":
                    yield _response_chunk(
                        request,
                        {
                            "event_type": "chat.error",
                            "error": _visible_error(
                                payload.get("error_message"),
                                _error_text("turn_failed"),
                            ),
                        },
                        complete=True,
                        runtime_completion="completed",
                    )
                else:
                    yield _response_chunk(
                        request,
                        {"event_type": "chat.final", "content": ""},
                        complete=True,
                        runtime_completion="completed",
                    )
                return

            parsed = _parse_external_chunk(
                chunk,
                has_streamed_answer=has_streamed_answer,
            )
            if parsed is None:
                continue
            if chunk.type == "llm_output" and parsed.get("event_type") == "chat.delta":
                has_streamed_answer = True
            is_interaction = chunk.type == INTERACTION
            yield _response_chunk(
                request,
                parsed,
                complete=is_interaction,
                runtime_completion="suspended" if is_interaction else None,
            )
            if is_interaction:
                return
    except Exception as exc:
        logger.warning(
            "External Harness output stream failed for session %s: %s",
            request.session_id,
            _visible_error(exc, "stream failed"),
        )
        yield _response_chunk(
            request,
            {
                "event_type": "chat.error",
                "error": _visible_error(exc, _error_text("stream_failed")),
            },
            complete=True,
        )
        return

    yield _response_chunk(
        request,
        {
            "event_type": "chat.error",
            "error": _error_text("stream_closed"),
        },
        complete=True,
    )


def _interrupt_response(
    request: AgentRequest,
    *,
    success: bool,
    message: str,
) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={
            "event_type": "chat.interrupt_result",
            "intent": "cancel",
            "success": success,
            "message": message,
        },
        metadata=request.metadata,
    )


async def process_external_harness_interrupt(
    adapter: Any,
    request: AgentRequest,
) -> AgentResponse:
    """Map one External Harness cancel request to graceful abort."""
    params = request.params if isinstance(request.params, dict) else {}
    intent = str(params.get("intent") or "cancel")
    if intent != "cancel":
        return _interrupt_response(
            request,
            success=False,
            message=_error_text("unsupported_intent", intent=intent),
        )

    io = getattr(adapter, "_external_harness", None)
    if io is None or io.state is not HarnessState.RUNNING:
        return _interrupt_response(
            request,
            success=True,
            message=_error_text("nothing_to_cancel"),
        )

    try:
        await io.abort(immediate=False)
    except UnsupportedHarnessCapabilityError:
        return _interrupt_response(
            request,
            success=False,
            message=_error_text("cancel_unsupported"),
        )
    except Exception as exc:
        logger.warning(
            "External Harness graceful abort failed for session %s: %s",
            request.session_id,
            _visible_error(exc, "abort failed"),
        )
        return _interrupt_response(
            request,
            success=False,
            message=_visible_error(exc, _error_text("cancel_failed")),
        )
    return _interrupt_response(
        request,
        success=True,
        message=_error_text("cancel_requested"),
    )
