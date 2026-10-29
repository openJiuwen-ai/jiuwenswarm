"""Manage JiuwenMate colleagues: create, provision the default 小九, edit, publish and delete.

A colleague's persona lives in its own Template; every other setting lives in the
definition. Publishing freezes both, plus the resolved model, into a new
configuration version. A Request keeps the version it was accepted with, so a
later publication only affects new Requests. Every published version may read
the colleague's own office directory; the user puts files there.

A colleague is a configuration whose row is also its Web agent. Each IM app bound to
it gets its own agent, office directory and Sessions; one publication switches all of
them together.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import (
    CapabilitySelection,
    ColleagueDefinition,
    ColleagueSettings,
    ColleagueView,
    ConfigVersionView,
    ConnectionRecord,
    PersistentAgentError,
    require_identifier,
)
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ, WORKSPACE_WRITE, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog, ResourceCatalog, assemble_resources
from jiuwenswarm.common.context_window import DEFAULT_CONTEXT_WINDOW_TOKENS
from jiuwenswarm.server.jiuwenmate.spec_provider import (
    ConfigSnapshot,
    model_identity,
    model_identity_prompt,
    publish_spec,
)
from jiuwenswarm.server.jiuwenmate.bash_runtime import (
    BASH_PROMPT,
    FULL_ACCESS_BASH_PROMPT,
    WEB_FETCH_PROMPT,
    HOST_PROMPT_MARKER,
    PROVIDER as BASH_PROVIDER,
    refresh_builtin_tools,
    with_bash_tool,
)
from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime
from jiuwenswarm.server.jiuwenmate.resource_assembly import apply_feishu_skills
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

logger = logging.getLogger(__name__)

DEFAULT_COLLEAGUE = ColleagueSettings(
    name="小九",
    description="通用办公助理，帮助整理信息、撰写文档和总结讨论。",
    persona=(
        "你是小九，用户团队中的通用办公助理。\n\n"
        "你负责理解用户和同事提出的任务，梳理需求，查找与整理信息，撰写和修改文档，"
        "总结讨论要点，并给出清晰、可执行的结果。\n\n"
        "回答时先给结论，再给必要的依据和步骤。信息不足时直接说明缺少什么，不编造事实。"
    ),
    tags=("效率提升",),
    category="custom",
    capabilities=CapabilitySelection(
        starter_prompts=("帮我整理本周工作进展。", "把这段会议记录总结成要点。", "帮我起草一封工作邮件。")
    ),
)

# Write and edit parameter text is rewritten to these office-relative paths when the tool is registered.
WORKSPACE_PROMPT = (
    "\n\n# 办公目录\n\n"
    "你可以用 list_files 和 read_file 查看办公目录中的文件，用 write_file 和 edit_file 创建或修改文件。"
    "路径一律写成相对于办公目录的路径，不要使用绝对路径。"
    "新建文件默认放在办公目录根下，路径写文件名本身，例如 `test.txt`；"
    "只有对方指定了子目录时，才使用 `资料/周报.md` 这样的相对路径。"
    "修改已有文件前先读取它；如果提示版本冲突，说明文件已被改动，重新读取后再修改。"
    "`sessions/<session_id>/` 存放该会话的私有中间产物；只能使用本次执行指定的会话目录，普通中间文件直接放在其中，"
    "Lark Slides 专用计划和截图放在其中的 `.lark-slides/`。最终交付文件放入 Agent 共享的 `output/`，"
    "并通过 `share_file` 登记 `output/<文件名>`；不要把其他会话的草稿当作共享产物。"
    "具体可访问范围由本次请求授权决定；遇到拒绝时不要猜测文件内容或声称已经读取。"
    "需要把已完成的文件交给对方时，用 share_file 登记；回答结束后由系统发送。"
)

# Feishu IM accepts uploaded files up to 30 MB, so no shared file may exceed it.
SHARE_FILE_MAX_BYTES = 30_000_000
# Formal execution refuses a Spec that omits the capacity check. A model entry's
# own positive context window and output limit are kept; otherwise the host uses
# the fixed 256K window and reserves 8192 tokens for output plus a 1024 margin.
FORMAL_OUTPUT_RESERVE_TOKENS = 8192
FORMAL_CAPACITY_MARGIN_TOKENS = 1024

TemplateSource = Callable[[str], Mapping[str, Any] | None]
# Platforms whose apps are bound to a colleague as separate connections.
IM_CHANNELS = frozenset({"feishu", "dingtalk"})


class ModelProvider(Protocol):
    """Resolves a credential-free model selection into the core model configuration."""

    def default_selection(self) -> str | None:
        """Selection used when a colleague does not choose a model; None if no model is configured."""

    def resolve(self, selection: str, *, client_id: str) -> TeamModelConfig:
        """Return the full model configuration; raise PersistentAgentError if it cannot be resolved."""


class ConfiguredModelProvider:
    """Reads the models configured in Swarm through the existing model catalog."""

    def __init__(self, entries: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        if entries is None:
            from jiuwenswarm.common.config import get_default_models

            entries = get_default_models
        self._entries = entries

    def _catalog(self):
        from jiuwenswarm.runtime.model_catalog import build_model_catalog

        entries = list(self._entries())
        return entries, build_model_catalog(entries)

    def default_selection(self) -> str | None:
        _, catalog = self._catalog()
        return catalog.current_selection or None

    def resolve(self, selection: str, *, client_id: str) -> TeamModelConfig:
        from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
        from jiuwenswarm.runtime.model_catalog import ModelCatalogError, resolve_model_selection

        entries, catalog = self._catalog()
        try:
            descriptor = resolve_model_selection(catalog, selection)
        except ModelCatalogError as exc:
            raise PersistentAgentError(f"model is not available: {exc.message}", code="MODEL_NOT_FOUND") from exc
        entry = entries[int(descriptor.selection_key.rpartition("#")[2])]
        client = dict(entry["model_client_config"])
        client["client_id"] = client_id
        request = build_reasoning_model_request_kwargs(
            model_client_config=client,
            model_config_obj=entry.get("model_config_obj") or {},
            model_name=descriptor.model_name,
        )
        return TeamModelConfig(
            model_client_config=ModelClientConfig(**client),
            model_request_config=ModelRequestConfig(**request),
        )


def _positive_token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def formal_model_capacity(model: TeamModelConfig) -> tuple[TeamModelConfig, int, int]:
    """Fill the four values formal execution requires, keeping a configured output limit.

    Returns the model, the input limit, and the safety margin. The input limit is the
    context window minus the output reserve and the margin.
    """
    request = model.model_request_config or ModelRequestConfig()
    window = _positive_token_count(request.context_window) or DEFAULT_CONTEXT_WINDOW_TOKENS
    reserve = _positive_token_count(request.max_tokens)
    if reserve is None:
        reserve = min(FORMAL_OUTPUT_RESERVE_TOKENS, window - FORMAL_CAPACITY_MARGIN_TOKENS - 1)
    if reserve is None or reserve <= 0 or reserve + FORMAL_CAPACITY_MARGIN_TOKENS >= window:
        raise PersistentAgentError("model capacity leaves no room for input", code="INVALID_CONFIG")
    request = request.model_copy(update={"context_window": window, "max_tokens": reserve})
    return (
        model.model_copy(update={"model_request_config": request}),
        window - reserve - FORMAL_CAPACITY_MARGIN_TOKENS,
        FORMAL_CAPACITY_MARGIN_TOKENS,
    )


def formal_context_engine(model: TeamModelConfig) -> Any | None:
    """Describe optional local counting resources, without preparing or downloading them.

    Core can fall back to model usage and character estimates when a native
    tokenizer is unavailable. An unknown alias needs no tokenizer registry.
    """
    provider, model_name = model_identity(model)
    if not model_name:
        return None
    from openjiuwen.core.context_engine.schema.config import ContextEngineConfig

    from jiuwenswarm.server.runtime.tokenizer_service import (
        _default_tokenizer_spec,
        resolve_tokenizer_cache_dir,
    )

    spec = _default_tokenizer_spec(provider=provider, model=model_name)
    if spec is None:
        return None
    return ContextEngineConfig(
        model_name=model_name,
        model_provider=provider or None,
        tokenizer_registry=[spec],
        tokenizer_cache_dir=str(resolve_tokenizer_cache_dir()),
        enable_tokenizer_download=False,
        tokenizer_offline=True,
    )


def show_agent_template(template_id: str) -> Mapping[str, Any] | None:
    from jiuwenswarm.server.runtime.extension_package_manager import show_agent_template as show

    return show(template_id)


def default_data_dir() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir() / "jiuwenmate"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_agent_id() -> str:
    return "mate_" + uuid.uuid4().hex


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("zh", "en"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return ""


def _fallback_persona(settings: ColleagueSettings) -> str:
    persona = f"你是{settings.name}。"
    if settings.description:
        persona += f"\n\n{settings.description}"
    return persona


class PersistentAgentService:
    """All colleague management goes through this service; callers never see Specs or credentials."""

    def __init__(
        self,
        repository: PersistentAgentRepository,
        templates: TemplateStore,
        *,
        models: ModelProvider,
        template_source: TemplateSource = show_agent_template,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = _new_agent_id,
        windows: WindowBackendStore | None = None,
        workspaces: WorkspaceStore | None = None,
        resources: ResourceCatalog | None = None,
    ) -> None:
        self._repository = repository
        self._templates = templates
        self._windows = windows
        self._workspaces = workspaces or WorkspaceStore(repository.db_path.parent / "workspaces")
        self._resources = resources
        self._models = models
        self._template_source = template_source
        self._clock = clock
        self._id_factory = id_factory
        self.cli_runtime = FeishuCliRuntime(repository.db_path.parent)

    @classmethod
    def create_default(cls, data_dir: Path | None = None) -> PersistentAgentService:
        root = Path(data_dir) if data_dir is not None else default_data_dir()
        return cls(
            PersistentAgentRepository(root / "agents.sqlite3"),
            TemplateStore(root / "templates"),
            models=ConfiguredModelProvider(),
            windows=WindowBackendStore(root / "agents.sqlite3"),
            workspaces=WorkspaceStore(root / "workspaces"),
        )

    @property
    def workspaces(self) -> WorkspaceStore:
        return self._workspaces

    def _view(self, definition: ColleagueDefinition) -> ColleagueView:
        path = self._workspaces.path(definition.namespace_id, definition.agent_id)
        return replace(ColleagueView.from_definition(definition), workspace_dir=str(path))

    def _agent_view(self, agent: ColleagueDefinition, config: ColleagueDefinition) -> ColleagueView:
        """An IM agent as runtime callers see it: its own version and office, its configuration's settings."""
        return replace(
            self._view(config),
            agent_id=agent.agent_id,
            current_version=agent.current_version,
            workspace_dir=str(self._workspaces.path(agent.namespace_id, agent.agent_id)),
            created_at=agent.created_at,
        )

    def _config_of(self, definition: ColleagueDefinition) -> ColleagueDefinition:
        connection = self._repository.connection_of(definition.namespace_id, definition.agent_id)
        return definition if connection is None else self._require(definition.namespace_id, connection.config_id)

    def _require_config(
        self, namespace_id: str, agent_id: str, *, include_hidden: bool = False,
    ) -> ColleagueDefinition:
        """A colleague configuration, visible unless ``include_hidden``; an IM agent is managed through it."""
        definition = self._require(namespace_id, agent_id)
        if self._repository.connection_of(namespace_id, agent_id) is not None:
            raise PersistentAgentError("this agent serves one connection of a colleague; use the colleague instead")
        if definition.hidden and not include_hidden:
            raise PersistentAgentError("colleague not found", code="NOT_FOUND")
        return definition

    def _require(self, namespace_id: str, agent_id: str) -> ColleagueDefinition:
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        definition = self._repository.get(namespace_id, agent_id)
        if definition is None:
            raise PersistentAgentError("colleague not found", code="NOT_FOUND")
        return definition

    def _resource_catalog(self) -> ResourceCatalog:
        if self._resources is None:
            self._resources = InstalledResourceCatalog()
        return self._resources

    def _check_model(self, settings: ColleagueSettings, agent_id: str) -> None:
        if settings.capabilities.model is not None:
            self._models.resolve(settings.capabilities.model, client_id=f"jiuwenmate:{agent_id}")

    @staticmethod
    def _permission_snapshot(profile: str) -> dict[str, Any]:
        """Freeze the host permission policy for this employee and publication."""
        from jiuwenswarm.common.config import get_config

        config = get_config()
        raw = config.get("permissions", {}) if isinstance(config, dict) else {}
        permissions = deepcopy(raw) if isinstance(raw, dict) else {}
        permissions["enabled"] = profile != "full_access"
        permissions["mode"] = "auto" if profile == "automatic" else "manual"
        if profile == "automatic":
            from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
                normalize_permissions_for_runtime,
            )

            permissions = normalize_permissions_for_runtime(permissions)
            permissions["enabled"] = True
        elif profile == "full_access":
            permissions = {"enabled": False}
        return permissions

    def _insert(
        self,
        namespace_id: str,
        settings: ColleagueSettings,
        *,
        source_template_id: str | None,
        is_default: bool,
    ) -> tuple[ColleagueDefinition, bool]:
        agent_id = self._id_factory()
        now = self._clock()
        definition = ColleagueDefinition(
            namespace_id=namespace_id,
            agent_id=agent_id,
            settings=settings.with_persona(""),
            source_template_id=source_template_id,
            is_default=is_default,
            enabled=True,
            revision=1,
            published_revision=None,
            current_version=None,
            created_at=now,
            updated_at=now,
        )
        with self._templates.create(agent_id, settings, source_template_id):
            inserted = self._repository.insert(definition, claim_default=is_default)
        if not inserted:
            self._templates.remove(agent_id)
        return definition, inserted

    def ensure_default_colleague(self, namespace_id: str) -> ColleagueView | None:
        """Create 小九 the first time a namespace is used; never again, even after the user deletes it."""
        require_identifier("namespace_id", namespace_id)
        claimed = self._repository.default_agent_id(namespace_id)
        if claimed is None:
            settings = replace(
                DEFAULT_COLLEAGUE,
                capabilities=replace(DEFAULT_COLLEAGUE.capabilities, model=self._models.default_selection()),
            )
            definition, inserted = self._insert(namespace_id, settings, source_template_id=None, is_default=True)
            if inserted:
                if settings.capabilities.model is not None:
                    try:
                        self.publish(namespace_id, definition.agent_id)
                    except PersistentAgentError as exc:
                        logger.warning("[JiuwenMate] default colleague left unpublished: code=%s", exc.code)
                claimed = definition.agent_id
            else:
                claimed = self._repository.default_agent_id(namespace_id)
        definition = self._repository.get(namespace_id, claimed) if claimed else None
        return None if definition is None or definition.hidden else self._view(definition)

    def create_colleague(
        self,
        namespace_id: str,
        settings: ColleagueSettings,
        *,
        template_id: str | None = None,
        source_template_id: str | None = None,
        unique_source: bool = False,
    ) -> ColleagueView:
        """Create a colleague with its own Template and publish its first version."""
        require_identifier("namespace_id", namespace_id)
        recorded_source = source_template_id or template_id
        if recorded_source is not None:
            require_identifier("source_template_id", recorded_source)
            if unique_source and any(
                item.source_template_id == recorded_source and not item.hidden
                for item in self._repository.list(namespace_id)
            ):
                raise PersistentAgentError("this employee already exists", code="CONFLICT")
        if template_id is not None:
            require_identifier("template_id", template_id)
            card = self._template_source(template_id)
            if card is None:
                raise PersistentAgentError("Template not found", code="TEMPLATE_NOT_FOUND")
            settings = replace(
                settings,
                persona=settings.persona or str(card.get("persona") or "").strip(),
                description=settings.description or _localized(card.get("details") or card.get("displayDescription")),
            )
        if not settings.persona:
            settings = settings.with_persona(_fallback_persona(settings))
        if settings.capabilities.model is None:
            default_model = self._models.default_selection()
            if default_model is None:
                raise PersistentAgentError("no model is configured", code="MODEL_NOT_CONFIGURED")
            settings = replace(settings, capabilities=replace(settings.capabilities, model=default_model))
        self._check_model(settings, "probe")
        definition, inserted = self._insert(
            namespace_id, settings, source_template_id=recorded_source, is_default=False,
        )
        try:
            self.publish(namespace_id, definition.agent_id)
        except Exception:
            if inserted:
                try:
                    self.delete_colleague(namespace_id, definition.agent_id)
                except Exception:
                    logger.exception(
                        "[JiuwenMate] failed to roll back colleague creation: agent=%s",
                        definition.agent_id,
                    )
            raise
        return self.get_colleague(namespace_id, definition.agent_id)

    def refresh_published(self, namespace_id: str, *, models: ModelProvider | None = None) -> bool:
        """Republish colleagues without pending edits, so versions from an older host gain what it now opens.

        Unchanged settings keep their version. Return false if a published colleague
        has pending edits or could not be refreshed; its existing version stays intact.
        """
        if models is not None:
            self._models = models
        applied = True
        connected = {item.agent_id for item in self._repository.connections(namespace_id)}
        for definition in self._repository.list(namespace_id):
            # An IM agent is republished with its configuration.
            if definition.agent_id in connected or definition.hidden or definition.current_version is None:
                continue
            if definition.has_unpublished_changes:
                applied = False
                continue
            try:
                self.publish(namespace_id, definition.agent_id, expected_revision=definition.revision, models=models)
            except Exception as exc:
                applied = False
                code = exc.code if isinstance(exc, PersistentAgentError) else type(exc).__name__
                logger.warning(
                    "[JiuwenMate] colleague kept its version: agent=%s code=%s", definition.agent_id, code
                )
        return applied

    def list_colleagues(self, namespace_id: str) -> list[ColleagueView]:
        """The visible colleague configurations; their IM agents are not listed."""
        require_identifier("namespace_id", namespace_id)
        connected = {item.agent_id for item in self._repository.connections(namespace_id)}
        return [
            self._view(item) for item in self._repository.list(namespace_id)
            if item.agent_id not in connected and not item.hidden
        ]

    def list_agents(self, namespace_id: str) -> list[ColleagueView]:
        """Every runtime agent: each configuration's Web agent and every IM agent."""
        require_identifier("namespace_id", namespace_id)
        rows = self._repository.list(namespace_id)
        by_id = {item.agent_id: item for item in rows}
        connections = {item.agent_id: item for item in self._repository.connections(namespace_id)}
        views = []
        for item in rows:
            connection = connections.get(item.agent_id)
            if connection is None:
                views.append(self._view(item))
            elif connection.config_id in by_id:
                views.append(self._agent_view(item, by_id[connection.config_id]))
        return views

    def get_colleague(self, namespace_id: str, agent_id: str) -> ColleagueView:
        """The runtime view of any agent; an IM agent carries its configuration's settings."""
        definition = self._require(namespace_id, agent_id)
        config = self._config_of(definition)
        return self._view(definition) if config is definition else self._agent_view(definition, config)

    def get_config(self, namespace_id: str, agent_id: str) -> ColleagueView:
        """A visible colleague configuration, as management pages show it."""
        return self._view(self._require_config(namespace_id, agent_id))

    def read_settings(self, namespace_id: str, agent_id: str) -> ColleagueSettings:
        """The current settings; an IM agent reads its configuration's, so a saved reply mode applies at once."""
        config = self._config_of(self._require(namespace_id, agent_id))
        return config.settings.with_persona(self._templates.read_persona(config.agent_id))

    def connection_of(self, namespace_id: str, agent_id: str) -> ConnectionRecord | None:
        """The platform app an IM agent may serve; ``None`` for a configuration's Web agent."""
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        return self._repository.connection_of(namespace_id, agent_id)

    def connections(self, namespace_id: str, agent_id: str) -> list[ConnectionRecord]:
        """Every IM connection of a configuration, including apps no longer bound to it."""
        self._require_config(namespace_id, agent_id)
        return self._repository.connections(namespace_id, agent_id)

    def removal_connections(self, namespace_id: str, agent_id: str) -> list[ConnectionRecord] | None:
        """The IM connections a deletion must remove, or ``None`` if the colleague was already purged.

        A hidden colleague still lists its connections, so an unbinding or purge whose answer was
        lost can be repeated. An ID that never named a colleague is ``NOT_FOUND``.
        """
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        if self._repository.get(namespace_id, agent_id) is None and self._repository.was_removed(
            namespace_id, agent_id,
        ):
            return None
        self._require_config(namespace_id, agent_id, include_hidden=True)
        return self._repository.connections(namespace_id, agent_id)

    def attach_connection(self, namespace_id: str, agent_id: str, channel: str, connection_key: str) -> str:
        """Return the canonical Feishu agent or create a dedicated agent for another IM app.

        Web and Feishu share the colleague's identity and workspace. DingTalk keeps its
        per-connection agent. New dedicated agents are published from the colleague's last
        publication, never from unpublished settings.
        """
        config = self._require_config(namespace_id, agent_id)
        if channel not in IM_CHANNELS:
            raise PersistentAgentError(f"unsupported channel {channel!r}")
        require_identifier("connection_key", connection_key)
        if channel == "feishu":
            return config.agent_id
        now = self._clock()
        created = self._id_factory()
        record = self._repository.insert_connection_agent(
            ColleagueDefinition(
                namespace_id=namespace_id,
                agent_id=created,
                settings=config.settings.with_persona(""),
                source_template_id=None,
                is_default=False,
                enabled=True,
                revision=0,
                published_revision=None,
                current_version=None,
                created_at=now,
                updated_at=now,
            ),
            ConnectionRecord(
                namespace_id=namespace_id,
                agent_id=created,
                config_id=config.agent_id,
                channel=channel,
                connection_key=connection_key,
                created_at=now,
            ),
        )
        agent = self._require(namespace_id, record.agent_id)
        published = self._published_settings(config)
        if agent.current_version is None and published is not None:
            try:
                snapshot = self._prepare_version(namespace_id, agent, published, None)
                self._repository.publish_connection_agent(
                    namespace_id, snapshot, config_id=config.agent_id, config_revision=config.published_revision,
                    settings=published, published_at=now,
                )
            except Exception as exc:
                current = self._repository.get(namespace_id, agent.agent_id)
                if current is None:
                    # The colleague was deleted meanwhile; preparing the version recreated the office.
                    self._workspaces.remove(namespace_id, agent.agent_id)
                    raise PersistentAgentError("colleague not found", code="NOT_FOUND") from exc
                # A publication that ran meanwhile has already given the new agent its version.
                if getattr(exc, "code", None) != "CONFLICT" or current.current_version is None:
                    raise
        return record.agent_id

    def hide_colleague(self, namespace_id: str, agent_id: str) -> None:
        """Take a colleague off the list and stop all its agents; every agent keeps its data. Repeating is harmless."""
        self._require_config(namespace_id, agent_id, include_hidden=True)
        self._repository.hide(namespace_id, agent_id, updated_at=self._clock())

    def _published_settings(self, config: ColleagueDefinition) -> ColleagueSettings | None:
        if config.published_settings is not None:
            return config.published_settings
        # Colleagues published before connections were recorded: without pending edits the settings are the
        # published ones; with pending edits the published persona is unknown.
        if config.current_version is not None and not config.has_unpublished_changes:
            return config.settings.with_persona(self._templates.read_persona(config.agent_id))
        return None

    def update_settings(self, namespace_id: str, agent_id: str, settings: ColleagueSettings) -> ColleagueView:
        """Save settings and the Template; nothing changes for Requests until ``publish``."""
        definition = self._require_config(namespace_id, agent_id)
        if not settings.persona:
            settings = settings.with_persona(self._templates.read_persona(agent_id))
        self._check_model(settings, agent_id)
        with self._templates.replace(agent_id, settings, definition.source_template_id):
            updated = self._repository.update_settings(
                namespace_id,
                agent_id,
                settings.with_persona(""),
                expected_revision=definition.revision,
                updated_at=self._clock(),
            )
        return self._view(updated)

    def set_enabled(self, namespace_id: str, agent_id: str, *, enabled: bool) -> ColleagueView:
        """Persist office availability for every connection. Private sessions stay open; IM and schedules stop."""
        self._require_config(namespace_id, agent_id)
        updated = self._repository.set_enabled(namespace_id, agent_id, enabled=enabled, updated_at=self._clock())
        return self._view(updated)

    def publish(
        self, namespace_id: str, agent_id: str, *,
        expected_revision: int | None = None, models: ModelProvider | None = None,
    ) -> ConfigVersionView:
        """Freeze the current settings as the version new Requests use; unchanged settings keep the version.

        Every agent of the colleague is switched in one transaction. If any of them cannot be
        published, or the colleague changed meanwhile, all of them keep their versions.
        """
        definition = self._require_config(namespace_id, agent_id)
        if expected_revision is not None and definition.revision != expected_revision:
            raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
        settings = definition.settings
        if settings.capabilities.model is None:
            raise PersistentAgentError("choose a model before publishing", code="MODEL_NOT_CONFIGURED")
        settings = settings.with_persona(self._templates.read_persona(agent_id))
        agents = [definition] + [
            self._require(namespace_id, item.agent_id) for item in self._repository.connections(namespace_id, agent_id)
        ]
        changes = [
            (agent.agent_id, agent.current_version, self._prepare_version(namespace_id, agent, settings, models))
            for agent in agents
        ]
        self._repository.publish_configuration(
            namespace_id, agent_id, revision=definition.revision, published_settings=settings, changes=changes,
            published_at=self._clock(),
        )
        web = changes[0][2]
        version = definition.current_version if web is None else web.config_version
        return self._version_view(namespace_id, agent_id, version)

    def _prepare_version(
        self, namespace_id: str, agent: ColleagueDefinition, settings: ColleagueSettings, models: ModelProvider | None,
    ) -> ConfigSnapshot | None:
        """The next version of one agent for ``settings`` (persona included), or ``None`` if it would not change."""
        agent_id = agent.agent_id
        office = self._workspaces.ensure(namespace_id, agent_id)
        assembled = assemble_resources(agent_id, settings.capabilities, office, self._resource_catalog())
        model, input_limit, margin = formal_model_capacity(
            (models or self._models).resolve(settings.capabilities.model, client_id=f"jiuwenmate:{agent_id}")
        )
        context_engine = formal_context_engine(model)
        fields = with_bash_tool({
            "card": {"id": agent_id, "name": settings.name, "description": settings.description},
            "system_prompt": (
                settings.persona
                + HOST_PROMPT_MARKER
                + WORKSPACE_PROMPT
                + assembled.prompt
                + model_identity_prompt(model)
                + (FULL_ACCESS_BASH_PROMPT if settings.permission_profile == "full_access" else BASH_PROMPT)
                + WEB_FETCH_PROMPT
            ),
            "model": model,
            "model_input_limit_tokens": input_limit,
            "capacity_safety_margin_tokens": margin,
            "enabled_features": [
                "construction",
                WORKSPACE_READ,
                WORKSPACE_WRITE,
                "window_management",
            ],
            "share_file_max_bytes": SHARE_FILE_MAX_BYTES,
            # Guarded file tools and the Invocation-local Bash adapter reuse core operations.
            "enable_sys_operation": True,
            "permission_profile": settings.permission_profile,
            "permissions": self._permission_snapshot(settings.permission_profile),
            # Otherwise core probes the model for image input outside the budget port.
            "enable_read_image_multimodal": False,
            **assembled.spec_fields,
        })
        if context_engine is not None:
            fields["context_engine_config"] = context_engine
        fields = apply_feishu_skills(fields, office, self.cli_runtime.skill_directories(namespace_id, agent_id))
        try:
            if agent.current_version is not None:
                current = self.snapshot(namespace_id, agent_id, agent.current_version)
                if publish_spec(agent_id, current.config_version, fields).fingerprint == current.fingerprint:
                    return None
            return publish_spec(
                agent_id,
                self._repository.next_version(namespace_id, agent_id),
                fields,
                knowledge_root=assembled.knowledge_root,
            )
        except PersistentContractError as exc:
            raise PersistentAgentError(f"configuration cannot be published: {exc}", code="INVALID_CONFIG") from exc

    def refresh_feishu_capability(
        self, namespace_id: str, agent_id: str, *, skill_directories: tuple[Path, ...],
    ) -> ConfigVersionView | None:
        """Derive automatic CLI assets from the live version, never from unpublished settings."""
        import json

        definition = self._require(namespace_id, agent_id)
        if definition.current_version is None:
            return None
        current = self.current_snapshot(namespace_id, agent_id)
        fields = json.loads(current.spec_json)
        if not any(tool.get("type") == BASH_PROVIDER for tool in (fields.get("tools") or [])):
            fields = with_bash_tool(fields)
            fields["system_prompt"] += (
                FULL_ACCESS_BASH_PROMPT if fields.get("permission_profile") == "full_access" else BASH_PROMPT
            )
        fields = refresh_builtin_tools(fields)
        fields = apply_feishu_skills(fields, self._workspaces.path(namespace_id, agent_id), skill_directories)
        if publish_spec(agent_id, current.config_version, fields).fingerprint == current.fingerprint:
            return self._version_view(namespace_id, agent_id, current.config_version)
        snapshot = publish_spec(agent_id, self._repository.next_version(namespace_id, agent_id), fields,
                                knowledge_root=current.knowledge_root)
        self._repository.save_capability_version(namespace_id, snapshot, previous_version=current.config_version,
                                                 published_at=self._clock())
        return self._version_view(namespace_id, agent_id, snapshot.config_version)

    def _version_view(self, namespace_id: str, agent_id: str, config_version: str) -> ConfigVersionView:
        stored = self._repository.get_version(namespace_id, agent_id, config_version)
        if stored is None:
            raise PersistentAgentError("configuration version not found", code="NOT_FOUND")
        snapshot, published_at = stored
        return ConfigVersionView(
            agent_id=agent_id,
            config_version=config_version,
            fingerprint=snapshot.fingerprint,
            enabled_features=snapshot.enabled_features,
            published_at=published_at,
        )

    def current_snapshot(self, namespace_id: str, agent_id: str) -> ConfigSnapshot:
        """The version a newly accepted Request must be pinned to."""
        definition = self._require(namespace_id, agent_id)
        if definition.current_version is None:
            raise PersistentAgentError("colleague has no published configuration", code="NOT_PUBLISHED")
        return self.snapshot(namespace_id, agent_id, definition.current_version)

    def snapshot(self, namespace_id: str, agent_id: str, config_version: str) -> ConfigSnapshot:
        """The exact version an accepted Request was pinned to."""
        require_identifier("config_version", config_version)
        stored = self._repository.get_version(namespace_id, agent_id, config_version)
        if stored is None:
            raise PersistentAgentError("configuration version not found", code="NOT_FOUND")
        return stored[0]

    def delete_colleague(self, namespace_id: str, agent_id: str, *, record_removal: bool = False) -> None:
        """Remove the colleague, its versions, Template, office directory and window data.

        Files and windows go first and the rows last, so a failed deletion leaves the colleague
        to be found and deleted again. A deleted 小九 is not provisioned again.
        """
        self._require(namespace_id, agent_id)
        # Its IM agents go first; the repository refuses again if one was added meanwhile.
        if self._repository.connections(namespace_id, agent_id):
            raise PersistentAgentError("colleague still has connections", code="CONFLICT")
        connection = self._repository.connection_of(namespace_id, agent_id)
        if connection is not None and connection.channel == "welink":
            import shutil
            from jiuwenswarm.server.jiuwenmate.welink_inbound import credential_dir_for

            from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_binding import (
                load_welink_section, normalize_welink_section,
            )

            credentials = credential_dir_for(agent_id)
            section = normalize_welink_section(load_welink_section())
            reader_dir = str(section.get("reader_credential_dir") or "").strip()
            # Legacy installs share this directory with the reader. Removing the
            # colleague must not sign every remaining assistant out.
            shared_reader = Path(reader_dir).expanduser().resolve() if reader_dir else None
            if shared_reader is None or (credentials.resolve() != shared_reader and credentials.resolve() not in shared_reader.parents):
                shutil.rmtree(credentials, ignore_errors=True)
                if credentials.exists():
                    raise PersistentAgentError("WeLink credentials could not be removed", code="CLEANUP_FAILED")
        self._templates.remove(agent_id)
        self._workspaces.remove(namespace_id, agent_id)
        if self._windows is not None:
            self._windows.purge_agent(namespace_id, agent_id)
        # Both removals ignore errors, so a directory left behind is reported here.
        if self._templates.path(agent_id).exists() or self._workspaces.path(namespace_id, agent_id).exists():
            raise PersistentAgentError("colleague files could not be removed", code="CLEANUP_FAILED")
        self._repository.delete(namespace_id, agent_id, record_removal=record_removal)
