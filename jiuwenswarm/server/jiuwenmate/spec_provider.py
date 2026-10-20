"""Publish colleague configuration as fixed core Specs and assemble one invocation.

Only capabilities that both agent-core advertises and this host has opened are
published. Everything else is rejected here, and core still validates every
value again at its own entries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import Any, Literal, get_args, get_origin

from pydantic import ValidationError

from jiuwenswarm.server.jiuwenmate.execution_models import (
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
    build_execution_context,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import SessionWindowBackend
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import (
    WORKSPACE_READ,
    WORKSPACE_WRITE,
    HostWorkspaceAccess,
)
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.session.checkpointer import NoopCheckpointer
from openjiuwen.core.single_agent import create_agent_session
from openjiuwen.harness.persistent_agent import (
    PersistentAgentSpec,
    PersistentBuildContext,
    PersistentPorts,
)

HOST_OPEN_FEATURES: frozenset[str] = frozenset(
    {"construction", WORKSPACE_READ, WORKSPACE_WRITE, "window_management"}
)
_PUBLISHED_KNOWLEDGE_ROOT = re.compile(r"`(resources/knowledge/[a-f0-9]{16})/`")
# How many earlier Executions one continuation may read. A deployment constant, not a per-request choice.
CONTINUATION_CHAIN_LIMIT = 8

# Core opens single-round execution only; these values are fixed, not configurable.
_FIXED_FIELDS: dict[str, Any] = {
    "enable_task_loop": False,
    "auto_create_workspace": False,
    "continuation_chain_limit": CONTINUATION_CHAIN_LIMIT,
}
_UNOPENED_FLAGS = ("enable_async_subagent", "enable_subagent_runtime", "add_general_purpose_agent")
_DEFAULT_FEATURES = frozenset({"construction"})


def core_features() -> frozenset[str]:
    """Features the installed agent-core Spec accepts in ``enabled_features``."""
    pending = [PersistentAgentSpec.model_fields["enabled_features"].annotation]
    found: set[str] = set()
    while pending:
        annotation = pending.pop()
        if get_origin(annotation) is Literal:
            found.update(get_args(annotation))
        else:
            pending.extend(get_args(annotation))
    return frozenset(found)


@dataclass(frozen=True, kw_only=True)
class ConfigSnapshot:
    """A published configuration version; a Request keeps using the version it started with."""

    agent_id: str
    config_version: str
    spec_json: str
    fingerprint: str
    enabled_features: frozenset[str]
    knowledge_root: str | None = None

    def load_spec(self) -> PersistentAgentSpec:
        """Revalidate the stored Spec and refuse it if it no longer hashes to the published fingerprint."""
        try:
            spec = PersistentAgentSpec.model_validate_json(self.spec_json)
        except ValidationError as exc:
            raise PersistentContractError(f"stored Spec is invalid: {exc}") from exc
        if spec.card is None or spec.card.id != self.agent_id:
            raise PersistentContractError("stored Spec belongs to another Agent")
        if spec.fingerprint() != self.fingerprint:
            raise PersistentContractError("stored Spec does not match its fingerprint")
        if spec.enabled_features != self.enabled_features:
            raise PersistentContractError("stored Spec does not match its capability snapshot")
        if self.knowledge_root is not None and f"`{self.knowledge_root}/`" not in spec.system_prompt:
            raise PersistentContractError("stored Spec does not match its knowledge scope")
        return spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "config_version": self.config_version,
            "spec_json": self.spec_json,
            "fingerprint": self.fingerprint,
            "enabled_features": sorted(self.enabled_features),
            "knowledge_root": self.knowledge_root,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConfigSnapshot:
        fields = dict(data)
        fields["enabled_features"] = frozenset(fields["enabled_features"])
        if fields.get("knowledge_root") is None:
            try:
                prompt = json.loads(fields["spec_json"]).get("system_prompt", "")
            except (KeyError, TypeError, ValueError):
                prompt = ""
            roots = set(_PUBLISHED_KNOWLEDGE_ROOT.findall(prompt)) if isinstance(prompt, str) else set()
            if len(roots) == 1:
                fields["knowledge_root"] = roots.pop()
        return cls(**fields)


def publish_spec(
    agent_id: str,
    config_version: str,
    spec_fields: Mapping[str, Any],
    *,
    knowledge_root: str | None = None,
) -> ConfigSnapshot:
    """Validate colleague configuration against core and freeze it as one version."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise PersistentContractError("missing agent_id")
    if not isinstance(config_version, str) or not config_version.strip():
        raise PersistentContractError("missing config_version")
    fields = dict(spec_fields)
    for name, value in _FIXED_FIELDS.items():
        if name in fields and fields[name] != value:
            raise PersistentContractError(f"{name}={fields[name]!r} is not opened for persistent execution")
        fields[name] = value
    for flag in _UNOPENED_FLAGS:
        if fields.get(flag):
            raise PersistentContractError(f"{flag} is not opened for persistent execution")
    if fields.get("subagents"):
        raise PersistentContractError("subagents are not opened for persistent execution")

    features = frozenset(fields.get("enabled_features") or _DEFAULT_FEATURES)
    unknown = features - core_features()
    if unknown:
        raise PersistentContractError(f"agent-core does not support features: {sorted(unknown)}")
    unopened = features - HOST_OPEN_FEATURES
    if unopened:
        raise PersistentContractError(f"features are not opened by this host yet: {sorted(unopened)}")
    fields["enabled_features"] = features

    try:
        spec = PersistentAgentSpec.model_validate(fields)
    except ValidationError as exc:
        raise PersistentContractError(f"invalid PersistentAgentSpec: {exc}") from exc
    if spec.card is None or spec.card.id != agent_id:
        raise PersistentContractError("Spec card id must equal agent_id")
    try:
        fingerprint = spec.fingerprint()
    except BaseError as exc:
        raise PersistentContractError(f"Spec cannot be published: {exc}") from exc
    return ConfigSnapshot(
        agent_id=agent_id,
        config_version=config_version,
        spec_json=spec.model_dump_json(),
        fingerprint=fingerprint,
        enabled_features=spec.enabled_features,
        knowledge_root=knowledge_root,
    )


def build_invocation(
    snapshot: ConfigSnapshot,
    request: RequestRecord,
    execution: ExecutionRecord,
    *,
    ports: PersistentPorts,
    system_prompt_addendum: str | None = None,
) -> tuple[PersistentAgentSpec, PersistentBuildContext]:
    """Create the Spec and the single-use build context for one Execution.

    The Session is new and empty for every call and never checkpoints by itself;
    window state is restored by core through the window backend.
    """
    if request.agent_id != snapshot.agent_id:
        raise PersistentContractError("Request belongs to another Agent")
    if request.config_version != snapshot.config_version or request.config_fingerprint != snapshot.fingerprint:
        raise PersistentContractError("Request was accepted under another configuration version")
    backend = ports.window_backend
    if ports.group_chat is not None:
        from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort

        if not isinstance(ports.group_chat, HostGroupChatPort) or ports.group_chat.scope != (
                request.namespace_id, request.agent_id):
            raise PersistentContractError("group_chat must be the host port bound to this colleague")
    if not isinstance(backend, SessionWindowBackend) or backend.scope != (
        request.namespace_id,
        request.agent_id,
        request.session_id,
    ):
        raise PersistentContractError("window_backend must be the host store bound to this Session")
    colleague = (request.namespace_id, request.agent_id)
    for feature, port_name in ((WORKSPACE_READ, "workspace_access"), (WORKSPACE_WRITE, "workspace_write_access")):
        if feature not in snapshot.enabled_features:
            continue
        access = getattr(ports, port_name)
        if not isinstance(access, HostWorkspaceAccess) or access.scope != colleague:
            raise PersistentContractError(f"{port_name} must be the host scope of this colleague")
    spec = snapshot.load_spec()
    context = build_execution_context(request, execution, capability_snapshot=snapshot.enabled_features)
    session = create_agent_session(session_id=request.session_id, card=spec.card, checkpointer=NoopCheckpointer())
    try:
        build_context = PersistentBuildContext(
            execution=context,
            session=session,
            ports=ports,
            system_prompt_addendum=system_prompt_addendum,
        )
        build_context.validate(spec)
    except BaseError as exc:
        raise PersistentContractError(f"invalid PersistentBuildContext: {exc}") from exc
    if spec.continuation_chain_limit is None:
        spec = spec.model_copy(update={"continuation_chain_limit": CONTINUATION_CHAIN_LIMIT})
    return spec, build_context
