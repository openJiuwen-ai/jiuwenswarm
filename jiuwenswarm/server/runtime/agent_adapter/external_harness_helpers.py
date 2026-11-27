"""Helpers for routing single-agent External Harness sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.interrupt.response import InterruptKind
from openjiuwen.harness.resources import RuntimeSpec, read_agent_template_card
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessCapability,
    HarnessCheckpoint,
    HarnessInput,
    HarnessState,
    HarnessStateError,
    ResumePolicy,
    UnsupportedHarnessCapabilityError,
    json_value_to_builtin,
)
from openjiuwen.harness_providers import (
    HarnessIOAdapter,
    build_harness_context,
    create_harness,
)
from openjiuwen.harness_providers.io_adapter import TURN_LIFECYCLE, to_harness_input

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
from jiuwenswarm.server.runtime.session.history_io import run_stream_parser

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
# Bounds starting the short-lived Codex App Server plus thread/fork, below the
# web client's 60 s session.fork timeout so the server answers first.
_CODEX_FORK_TIMEOUT_SECONDS = 45.0
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
    "fork_at_message_unsupported": (
        "三方专家会话不支持从某条消息处复制；请复制整个会话",
        "A copy of an External Harness session cannot start at a message; copy the whole session",
    ),
    "fork_unsupported": (
        "该三方专家 (provider={provider!r}) 不支持复制其 CLI 会话",
        "This external expert (provider={provider!r}) cannot fork its CLI session",
    ),
    "fork_expert_changed": (
        "三方专家会话的副本只能沿用原专家 {template!r}；请先恢复原专家选择再复制，或新建会话后切换专家",
        "A copy of an External Harness session keeps its expert {template!r}; "
        "restore that expert before copying, or start a new session to switch experts",
    ),
    "fork_failed": (
        "复制 CLI 会话失败: {detail}",
        "Forking the CLI session failed: {detail}",
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
    use_en = True
    try:
        from jiuwenswarm.enterprise.config.preferred_language import (
            resolve_preferred_language,
        )

        use_en = resolve_preferred_language() == "en"
    except Exception:
        logger.debug("read preferred_language failed, fallback to en", exc_info=True)
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


async def _load_requested_runtime(
    agent_template_name: str,
) -> tuple[str, Path, RuntimeSpec | None]:
    """Locate the expert and read its runtime from openjiuwen's manifest card.

    Only ``manifest.json`` is read here; the package is parsed in full once,
    inside ``create_harness``.
    """
    runtime_name = equipment.resolve_equipment_runtime_id(
        "agent_templates",
        agent_template_name,
    )
    package_path = equipment.resolve_agent_template_dir(runtime_name)
    card = await read_agent_template_card(package_path / "manifest.json")
    return runtime_name, package_path, equipment.agent_template_runtime(card)


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


async def resolve_external_harness_binding(
    session_id: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve and enforce one session's immutable External Harness binding."""
    requested_name = _requested_template_name(params)
    # Read the card before taking the (thread) lock: the lock guards only the
    # session metadata check below and must not be held across an await.
    requested = await _load_requested_runtime(requested_name) if requested_name else None

    with _BINDING_LOCK:
        metadata = get_session_metadata(session_id, cache_bust=True)
        stored = metadata.get("session_runtime_binding")
        if isinstance(stored, dict) and stored.get("kind") == _EXTERNAL_HARNESS_KIND:
            bound_name = str(stored.get("agent_template_name") or "")
            bound_provider = str(stored.get("provider_name") or "")
            if requested is None:
                raise _binding_conflict_error(bound_name, bound_provider)

            runtime_name, package_path, runtime = requested
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

        if requested is None:
            return {"kind": "deep_agent"}

        runtime_name, package_path, runtime = requested
        if runtime is None:
            return {"kind": "deep_agent"}

        binding = {
            "kind": _EXTERNAL_HARNESS_KIND,
            "agent_template_name": runtime_name,
            "provider_name": runtime.provider_name,
        }
        return {
            **binding,
            "runtime": runtime,
            "package_path": package_path,
        }


def persist_external_harness_binding(
    session_id: str,
    binding: Mapping[str, Any],
) -> None:
    """Persist immutable External binding after the session child accepts it."""
    if binding.get("kind") != _EXTERNAL_HARNESS_KIND:
        return
    agent_template_name = str(binding.get("agent_template_name") or "").strip()
    provider_name = str(binding.get("provider_name") or "").strip()
    if not agent_template_name or not provider_name:
        return
    stored_binding = {
        "kind": _EXTERNAL_HARNESS_KIND,
        "agent_template_name": agent_template_name,
        "provider_name": provider_name,
    }
    with _BINDING_LOCK:
        metadata = get_session_metadata(session_id, cache_bust=True)
        stored = metadata.get("session_runtime_binding")
        if isinstance(stored, dict) and stored.get("kind") == _EXTERNAL_HARNESS_KIND:
            bound_name = str(stored.get("agent_template_name") or "")
            bound_provider = str(stored.get("provider_name") or "")
            if bound_name == agent_template_name and bound_provider == provider_name:
                return
            raise _binding_conflict_error(bound_name, bound_provider)
        update_session_metadata(
            session_id=session_id,
            session_runtime_binding=stored_binding,
            touch_last_message_at=False,
            cache_bust=True,
            sync_write=True,
        )


async def load_persisted_external_harness_binding(
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
    return await resolve_external_harness_binding(
        session_id,
        {"agent_template_name": template_name},
    )


def _session_workspace(session_id: str) -> Path:
    """Return the working folder an External Harness of the session runs in."""
    project_dir = get_session_metadata(session_id, cache_bust=True).get("project_dir")
    if isinstance(project_dir, str) and project_dir.strip():
        return Path(project_dir.strip()).expanduser().resolve(strict=False)
    return get_default_project_session_workspace_dir(session_id).resolve(strict=False)


def _claude_projects_dir() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / "projects" if config_dir else Path.home() / ".claude" / "projects"


def _fork_claude_session(source_session: str, source_cwd: Path, target_cwd: Path) -> str:
    """Fork a Claude Code session and place the fork where the target's CLI resumes it."""
    from jiuwenswarm.common.external_cli_runtime import activate_external_cli_runtime_paths

    # A frozen app installs claude_agent_sdk outside the bundle; this process
    # must expose its site-packages before importing it.
    activate_external_cli_runtime_paths()
    from claude_agent_sdk import fork_session, project_key_for_directory

    forked = fork_session(source_session, directory=str(source_cwd)).session_id
    source_key = project_key_for_directory(source_cwd)
    target_key = project_key_for_directory(target_cwd)
    if source_key != target_key:
        projects = _claude_projects_dir()
        written = projects / source_key / f"{forked}.jsonl"
        if not written.is_file():
            # A StopIteration here would surface as a meaningless RuntimeError
            # once it crosses asyncio.to_thread, so look up with a default.
            found = next(iter(projects.glob(f"*/{forked}.jsonl")), None)
            if found is None:
                raise FileNotFoundError(
                    f"forked Claude Code transcript {forked}.jsonl not found under {projects}"
                )
            written = found
        target_dir = projects / target_key
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(written, target_dir / written.name)
    return forked


async def _fork_codex_thread(source_thread: str, codex_bin: str | None, target_cwd: Path) -> str:
    """Fork a Codex thread through a short-lived App Server.

    Raises:
        TimeoutError: The App Server did not start and fork the thread within
            ``_CODEX_FORK_TIMEOUT_SECONDS``.
    """
    from openai_codex import AsyncCodex, CodexConfig

    config = CodexConfig(codex_bin=codex_bin, cwd=str(target_cwd), env=dict(os.environ))

    async def fork() -> str:
        async with AsyncCodex(config=config) as codex:
            thread = await codex.thread_fork(source_thread, cwd=str(target_cwd))
        return str(thread.id)

    try:
        return await asyncio.wait_for(fork(), timeout=_CODEX_FORK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise TimeoutError(
            f"codex thread/fork did not finish within {_CODEX_FORK_TIMEOUT_SECONDS:g}s"
        ) from exc


def _check_fork_keeps_expert(
    bound_template_name: str,
    session_equipment_override: Mapping[str, object] | None,
) -> None:
    """Refuse a copy whose equipment override names an expert other than the bound one.

    The copy is bound to the source's expert and the binding cannot change, so
    an override switching (or clearing) the expert would leave a copy whose
    every message fails with ``binding_conflict``.

    Raises:
        ValueError: The override selects a different expert or clears it.
    """
    if not session_equipment_override or "agent_template_name" not in session_equipment_override:
        return
    requested = session_equipment_override.get("agent_template_name")
    requested_name = requested.strip() if isinstance(requested, str) else ""
    keeps_expert = False
    if requested_name:
        try:
            keeps_expert = (
                equipment.resolve_equipment_runtime_id("agent_templates", requested_name)
                == bound_template_name
            )
        except Exception:
            logger.debug("resolve requested expert %r failed", requested_name, exc_info=True)
    if not keeps_expert:
        raise ValueError(_error_text("fork_expert_changed", template=bound_template_name))


async def fork_external_harness_session(
    source_session_id: str,
    target_session_id: str,
    *,
    at_message: bool = False,
    session_equipment_override: Mapping[str, object] | None = None,
) -> bool:
    """Bind a copy of an External Harness session to a fork of its CLI session.

    The target is bound to the source's expert, and its harness resumes from a
    fork of the source's CLI session (Claude Code ``fork_session``, Codex
    ``thread/fork``), placed for the target's working folder; the source's own
    CLI session is unchanged. A source that has run no turn has no CLI session
    yet, and the target starts one of its own. Return False when the source is
    not an External Harness session.

    Args:
        source_session_id: Session being copied.
        target_session_id: The copy.
        at_message: Whether the copy starts at a message; refused for an
            External Harness session.
        session_equipment_override: Equipment the copy was given in place of
            the source's; refused when it switches or clears the expert.

    Raises:
        ValueError: The copy is refused, or forking the CLI session failed.
    """
    stored = get_session_metadata(source_session_id, cache_bust=True).get("session_runtime_binding")
    if not isinstance(stored, dict) or stored.get("kind") != _EXTERNAL_HARNESS_KIND:
        return False
    if at_message:
        raise ValueError(_error_text("fork_at_message_unsupported"))
    agent_template_name = str(stored.get("agent_template_name") or "")
    provider_name = str(stored.get("provider_name") or "")
    _check_fork_keeps_expert(agent_template_name, session_equipment_override)
    binding: dict[str, Any] = {
        "kind": _EXTERNAL_HARNESS_KIND,
        "agent_template_name": agent_template_name,
        "provider_name": provider_name,
    }
    source_checkpoint = _load_external_harness_checkpoint(
        session_id=source_session_id,
        agent_template_name=agent_template_name,
        provider_name=provider_name,
    )
    target_checkpoint: HarnessCheckpoint | None = None
    if source_checkpoint is not None:
        data = dict(source_checkpoint.data)
        target_cwd = _session_workspace(target_session_id)
        try:
            if provider_name == "claudecode" and isinstance(data.get("session_id"), str):
                forked = await asyncio.to_thread(
                    _fork_claude_session,
                    data["session_id"],
                    _session_workspace(source_session_id),
                    target_cwd,
                )
                data["session_id"] = forked
            elif provider_name == "codex" and isinstance(data.get("thread_id"), str):
                runtime = await load_persisted_external_harness_binding(source_session_id) or {}
                config = _merge_host_external_cli_config(
                    provider_name, _runtime_config(runtime.get("runtime") or {}),
                )
                codex_bin = config.get("codex_bin")
                forked = await _fork_codex_thread(
                    data["thread_id"],
                    str(codex_bin) if codex_bin else None,
                    target_cwd,
                )
                data["thread_id"] = forked
            else:
                raise ValueError(_error_text("fork_unsupported", provider=provider_name))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                _error_text("fork_failed", detail=_visible_error(exc, type(exc).__name__))
            ) from exc
        data["resumed"] = False
        target_checkpoint = HarnessCheckpoint(
            provider=source_checkpoint.provider,
            schema_version=source_checkpoint.schema_version,
            agent_id=source_checkpoint.agent_id,
            host_session_id=target_session_id,
            checkpoint_id=f"fork-{uuid.uuid4().hex}",
            sequence=0,
            data=data,
            provider_session_id=forked,
        )
    with _BINDING_LOCK:
        update_session_metadata(
            session_id=target_session_id,
            session_runtime_binding=binding,
            **(
                {
                    "session_harness_checkpoint": _checkpoint_metadata(
                        target_checkpoint,
                        agent_template_name=agent_template_name,
                        provider_name=provider_name,
                    )
                }
                if target_checkpoint is not None
                else {}
            ),
            touch_last_message_at=False,
            cache_bust=True,
            sync_write=True,
        )
    return True


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
        turn_idle_timeout_s = 300.0
        raw_idle = entry.get("codex_turn_idle_timeout_s")
        if raw_idle is not None:
            try:
                parsed_idle = float(raw_idle)
                if parsed_idle > 0:
                    turn_idle_timeout_s = parsed_idle
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid codex_turn_idle_timeout_s=%r in external_cli_agents; using 300s",
                    raw_idle,
                )
        merged.setdefault("turn_idle_timeout_s", turn_idle_timeout_s)
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
    from jiuwenswarm.common.windows_cli_paths import resolve_preserved_cli_path

    for field in ("cli_path", "codex_bin") if provider_name == "codex" else ("cli_path",):
        if merged.get(field):
            merged[field] = resolve_preserved_cli_path(merged[field])
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


async def _with_input_boundaries(
    outputs: AsyncIterator[OutputSchema],
    boundaries: asyncio.Queue[dict[str, Any]],
) -> AsyncIterator[tuple[str, Any]]:
    """Interleave the turn's output with the input steered into it.

    Yields ``("output", chunk)`` for the turn's own output and
    ``("input", boundary)`` for each steered input, in the order they arrive;
    a boundary that is ready is yielded before the output that follows it.
    """
    iterator = outputs.__aiter__()
    next_output: asyncio.Future[Any] | None = None
    next_boundary: asyncio.Future[Any] | None = None
    try:
        while True:
            if next_output is None:
                next_output = asyncio.ensure_future(iterator.__anext__())
            if next_boundary is None:
                next_boundary = asyncio.ensure_future(boundaries.get())
            done, _ = await asyncio.wait(
                {next_output, next_boundary},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_boundary in done:
                boundary = next_boundary.result()
                next_boundary = None
                yield "input", boundary
                continue
            try:
                chunk = next_output.result()
            except StopAsyncIteration:
                return
            next_output = None
            yield "output", chunk
    finally:
        taken = None
        if next_boundary is not None and next_boundary.done() and not next_boundary.cancelled():
            taken = next_boundary.result()
        for pending in (next_output, next_boundary):
            if pending is not None and not pending.done():
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if taken is not None:
            # A steered input taken from the queue but not yielded stays first in it.
            rest = [boundaries.get_nowait() for _ in range(boundaries.qsize())]
            for boundary in (taken, *rest):
                boundaries.put_nowait(boundary)
        closer = getattr(iterator, "aclose", None)
        if callable(closer):
            await closer()


# How long a steer that finds its turn ended waits for the provider to leave
# RUNNING, so that its request starts the next turn.
_TURN_RELEASE_WAIT_S = 1.0
_TURN_RELEASE_POLL_S = 0.01


def _steer_lock(adapter: Any) -> asyncio.Lock:
    """The lock that orders a steer's send with the end of the turn's stream.

    A steer holds it from its last check of the turn's queue until its
    boundary is on it; the stream takes it to close the queue.
    """
    lock = getattr(adapter, "_external_harness_steer_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        adapter._external_harness_steer_lock = lock  # pylint: disable=protected-access
    return lock


async def _declined_at_turn_end(io: Any) -> bool:
    """Decline a steer whose turn has ended, once the provider has released it.

    The request then takes the path of a new message, which starts the next
    turn only when the provider is no longer RUNNING.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_RELEASE_WAIT_S
    while io.state is HarnessState.RUNNING and loop.time() < deadline:
        await asyncio.sleep(_TURN_RELEASE_POLL_S)
    return False


async def _close_steer_intake(
    adapter: Any,
    boundaries: asyncio.Queue[dict[str, Any]],
    merged: AsyncIterator[tuple[str, Any]],
) -> list[dict[str, Any]]:
    """Stop the turn taking steers; return the steered inputs it has not shown.

    A steer that was sending while the turn ended has its boundary on the
    queue once the lock is free. A later steer finds the queue closed and
    becomes the next turn.
    """
    async with _steer_lock(adapter):
        if getattr(adapter, "_external_harness_boundaries", None) is boundaries:
            adapter._external_harness_boundaries = None  # pylint: disable=protected-access
    await merged.aclose()
    return [boundaries.get_nowait() for _ in range(boundaries.qsize())]


_STEER_IMAGES_LEAD = (
    "The user attached these images to this message. "
    "They are files on this machine; read them from their paths."
)


async def _steer_images(request: AgentRequest, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The steer's uploaded images, checked against the session's uploads off the event loop.

    An image that is not an upload of this session, or cannot be read as an
    image, raises ``ValueError``; the steer is then not sent.
    """
    if not params.get("media_items"):
        return []
    from jiuwenswarm.server.runtime.attachments.steer_images import (
        resolve_steer_image_references,
    )

    return await asyncio.to_thread(resolve_steer_image_references, params, request.session_id)


def _steer_content(query: Any, images: list[dict[str, Any]]) -> HarnessInput:
    """The provider input of a steer: its prompt and, after it, the paths of its images.

    A new message gives the provider its images as upload paths in the
    prompt (``files_updated_by_user``); a steer names them in a text block of
    the same input, with the same records.
    """
    if not images:
        return to_harness_input(query)
    records = [
        {key: image.get(key) for key in ("filename", "path", "mime_type", "size_bytes")}
        for image in images
    ]
    listing = json.dumps({"uploaded_images": records}, ensure_ascii=False)
    return to_harness_input([
        {"type": "text", "text": str(query or "")},
        {"type": "text", "text": f"{_STEER_IMAGES_LEAD}\n{listing}"},
    ])


async def steer_external_harness(
    adapter: Any,
    request: AgentRequest,
    inputs: dict[str, Any],
) -> bool:
    """Steer the session's running External Harness turn with new input.

    The input goes to the provider as a steer of its active turn, and its
    boundary, ``chat.input_received``, enters the stream of that turn as it
    does for a turn of Swarm's own agent. Return False, before sending, when
    there is no running turn whose stream this adapter projects or the
    provider does not steer; the input then takes the path of a new message.
    A steer that finds the turn ended waits until the provider has released
    it first, so that this path starts the next turn.
    """
    from jiuwenswarm.runtime.session_input import SessionInputRejectedError

    io = getattr(adapter, "_external_harness", None)
    boundaries = getattr(adapter, "_external_harness_boundaries", None)
    if io is None or io.state is not HarnessState.RUNNING:
        return False
    if io.has_pending_interrupt():
        raise SessionInputRejectedError(_error_text("waiting_for_interaction"))
    if io.delivery_mode(immediate=True) is not DeliveryMode.STEER:
        return False
    if boundaries is None:
        return await _declined_at_turn_end(io)
    params = request.params if isinstance(request.params, dict) else {}
    query = inputs.get("query")
    images = await _steer_images(request, params)
    async with _steer_lock(adapter):
        if io.state is not HarnessState.RUNNING or (
            getattr(adapter, "_external_harness_boundaries", None) is not boundaries
        ):
            # The turn ended while the images were checked.
            ended = True
        else:
            try:
                await io.harness.send(_steer_content(query, images), mode=DeliveryMode.STEER)
            except HarnessStateError:
                # The turn ended before the provider took the input.
                ended = True
            else:
                boundaries.put_nowait(_steer_boundary(request, params, query, images))
                ended = False
    if ended:
        return await _declined_at_turn_end(io)
    return True


def _steer_boundary(
    request: AgentRequest,
    params: Mapping[str, Any],
    query: Any,
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    """The input boundary of a steer the provider took."""
    from agent_gateway.common.session_message import (
        SESSION_MESSAGE_INTERNAL_KEY,
        SESSION_MESSAGE_ORIGIN,
    )

    if images:
        # An image-only steer has no text of its own; the prompt is not the person's words.
        content = str(params.get("content") or params.get("query") or "")
    else:
        content = str(params.get("content") or params.get("query") or query or "")
    boundary: dict[str, Any] = {
        "input_request_id": request.request_id,
        "content": content,
        "timestamp": time.time() * 1000,
    }
    if images:
        boundary["media_items"] = images
    cross_session = params.get(SESSION_MESSAGE_INTERNAL_KEY)
    if isinstance(cross_session, dict):
        boundary.update({
            "message_origin": SESSION_MESSAGE_ORIGIN,
            "session_message_id": cross_session.get("message_id"),
            "cross_session": {**cross_session, "content": boundary["content"]},
        })
    return boundary



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
    if callable(language_resolver):
        language = language_resolver()
    else:
        from jiuwenswarm.enterprise.config.preferred_language import (
            resolve_preferred_language,
        )

        # Prefer memo / yaml; map zh→cn for harness codes that expect cn/en.
        from jiuwenswarm.common.i18n import to_runtime_language

        language = to_runtime_language(resolve_preferred_language()) or "cn"
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
        # The harness IO adapter accepts InteractiveInput.answer() under the request id.
        "interrupt_id": request_id,
        "kind": InterruptKind.ASK_USER,
    }


async def _parse_external_chunk(chunk: OutputSchema, *, has_streamed_answer: bool) -> dict[str, Any] | None:
    projected = _project_external_user_input(chunk)
    if projected is not None:
        return projected
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import project_stream_event

    parsed = await run_stream_parser(
        JiuWenSwarmDeepAdapter.decode_stream_chunk,
        chunk,
        has_streamed_content=has_streamed_answer,
    )
    return await project_stream_event(parsed)


async def process_external_harness_stream(
    adapter: Any,
    request: AgentRequest,
    inputs: dict[str, Any],
) -> AsyncIterator[AgentResponseChunk]:
    """Run one External Harness request and project its ordered chat stream."""
    input_timestamp_ms = time.time() * 1000
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
    # Input steered into this turn (steer_external_harness) and not yet answered.
    boundaries: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pending_input_ids: list[str] = []
    adapter._external_harness_boundaries = boundaries  # pylint: disable=protected-access

    def output_phase() -> AgentResponseChunk:
        applied = list(pending_input_ids)
        pending_input_ids.clear()
        return _response_chunk(
            request,
            {
                "event_type": "chat.output_phase",
                "output_phase_id": f"phase-{uuid.uuid4().hex}",
                "applied_input_ids": applied,
                "input_timestamp": input_timestamp_ms,
                # The stream clock in milliseconds, which history takes from
                # every record of an output phase.
                "timestamp": time.time() * 1000,
            },
            complete=False,
        )

    def input_received(boundary: dict[str, Any]) -> AgentResponseChunk:
        return _response_chunk(
            request,
            {"event_type": "chat.input_received", **boundary},
            complete=False,
        )

    merged = _with_input_boundaries(
        stream_turn_output(
            io,
            receipt_turn_id,
            resume=resume,
        ),
        boundaries,
    )
    try:
        async for source, chunk in merged:
            if source == "input":
                pending_input_ids.append(chunk["input_request_id"])
                yield input_received(chunk)
                continue
            if chunk.type in (TURN_LIFECYCLE, INTERACTION):
                # The provider took these steers as the turn ended or asked;
                # they are shown, not answered by this turn's output.
                for boundary in await _close_steer_intake(adapter, boundaries, merged):
                    yield input_received(boundary)
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
                    if pending_input_ids:
                        yield output_phase()
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

            parsed = await _parse_external_chunk(
                chunk,
                has_streamed_answer=has_streamed_answer,
            )
            if parsed is None:
                continue
            if pending_input_ids:
                # The first output after steered input answers it.
                yield output_phase()
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
    finally:
        if getattr(adapter, "_external_harness_boundaries", None) is boundaries:
            adapter._external_harness_boundaries = None  # pylint: disable=protected-access

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
