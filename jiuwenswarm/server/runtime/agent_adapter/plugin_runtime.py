# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native plugin dependencies, per-plugin ownership and transactional mounting."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import shutil
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import yaml

from gateway.common.mode_matrix import resolve_request_mode
from jiuwenswarm.extensions.sdk.plugin_runtime import PluginRuntimeServices
from jiuwenswarm.runtime.request import resolve_agent_request_mode

logger = logging.getLogger(__name__)
_SERVICES = {"trajectory", "main_model", "agent_data_workspace", "embedding_factory"}


@dataclass(frozen=True, repr=False)
class PluginRuntimeHost:
    """Adapter-owned state and callbacks explicitly exposed to the runtime."""

    instance: Any
    loaded_plugins: dict[str, tuple[Any, str]]
    main_model: tuple[Any, str]
    agent_data_workspace: Path
    profile: str
    config: dict
    has_conflicting_turn: Callable[[str], bool]
    wants_attach_goal: Callable[[dict], bool]


@dataclass
class PluginCandidate:
    name: str
    version: str
    path: Path
    contract: dict
    runtime: PluginRuntimeServices
    fingerprint: str

    @property
    def plugin_id(self) -> str:
        return self.runtime.plugin_id


@dataclass
class PluginBinding:
    candidate: PluginCandidate
    runtime: PluginRuntimeServices
    snapshot: Any
    lease: Any = None
    record: Any = None
    state: str = "loading"
    released: bool = False
    close_tasks: dict[int, asyncio.Task] = field(default_factory=dict, repr=False)


def _needs_close_attempt(task: asyncio.Task | None) -> bool:
    if task is None:
        return True
    if not task.done() or task.cancelled():
        return False
    return isinstance(task.exception(), TimeoutError)


async def _settle_thread_task(task: asyncio.Task) -> tuple[Any, bool]:
    """Retain ownership of thread results despite repeated caller cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _close_resources(binding: PluginBinding, timeout: float, grace: float) -> None:
    # Each package owns its drain logic. Never cancel an awaiter to fake a stopped writer.
    for resource in reversed(binding.runtime.resources):
        close = getattr(resource, "aclose", None)
        if callable(close):
            task = binding.close_tasks.get(id(resource))
            if _needs_close_attempt(task):
                binding.close_tasks[id(resource)] = asyncio.create_task(close(timeout=timeout, grace_timeout=grace))
    tasks = set(binding.close_tasks.values())
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=timeout + grace)
        if pending:
            raise TimeoutError("Plugin closing_timeout: resources are still closing")
        for task in tasks:
            task.result()


class _PluginSlot:
    """One plugin's owner; no assumptions about its algorithms, tools or storage."""

    def __init__(self, manager: PluginRuntimeManager, name: str) -> None:
        self.manager, self.name = manager, name
        self.binding: PluginBinding | None = None
        self.prepared: PluginBinding | None = None
        self.recovery: asyncio.Task | None = None
        self.recovery_restore: PluginBinding | None = None
        self.failed = False
        self.disposed = False
        self.generation = 0

    async def release(self, binding: PluginBinding) -> None:
        if binding.released:
            return
        if any(not task.done() for task in binding.close_tasks.values()):
            raise RuntimeError("Cannot release dependencies while plugin resources are closing")
        if binding.lease is not None:
            await asyncio.to_thread(binding.lease.release)
        binding.snapshot.cleanup()
        binding.released = True

    async def mount(self, binding: PluginBinding) -> Any:
        from openjiuwen.harness.schema.build_context import BuildContext

        host = self.manager.host
        instance = host.instance
        await self.manager.ensure_observer(binding)
        binding.runtime = replace(binding.runtime, resources=[])
        binding.close_tasks.clear()
        binding.record = None
        context = BuildContext(
            language=getattr(getattr(instance, "deep_config", None), "language", "cn") or "cn",
            workspace=getattr(getattr(instance, "deep_config", None), "workspace", None),
            member_card_id=binding.runtime.member_card_id,
            extras={"plugin_runtime": binding.runtime},
        )
        try:
            binding.record = await instance.load_plugin(binding.snapshot.name, context=context)
            if not binding.runtime.resources:
                raise RuntimeError("Runtime plugin did not register any constructed resources")
            for rail in binding.runtime.resources:
                if instance.find_rail_by_name(type(rail).__name__) is not rail:
                    raise RuntimeError("Plugin resource did not become active")
            binding.state = "active"
            self.failed = False
            self.binding = binding
            host.loaded_plugins[self.name] = (binding.record, binding.candidate.version)
            self.manager.refresh_tool_exposure()
            logger.info("Runtime plugin active: id=%s version=%s session=%s", binding.candidate.plugin_id,
                        binding.candidate.version, binding.runtime.session_id)
            return binding.record
        except BaseException:
            try:
                await self.detach(binding)
            except BaseException:
                self.binding = binding
                self.watch(binding, self.recovery_restore)
                raise
            raise

    async def detach(self, binding: PluginBinding) -> None:
        binding.state = "closing"
        await _close_resources(binding, self.manager.close_timeout, self.manager.close_grace)
        host = self.manager.host
        instance = host.instance
        if binding.record is not None:
            await instance.unload_extension(binding.record)
        else:
            for rail in reversed(binding.runtime.resources):
                if instance.find_rail_by_name(type(rail).__name__) is rail:
                    await instance.unregister_rail(rail)
        host.loaded_plugins.pop(self.name, None)
        if self.binding is binding:
            self.binding = None
        binding.state = "unmounted"

    def watch(self, binding: PluginBinding, restore: PluginBinding | None) -> None:
        if self.recovery is not None:
            return
        self.recovery_restore = restore

        async def recover() -> None:
            try:
                while True:
                    try:
                        await _close_resources(binding, self.manager.close_timeout, self.manager.close_grace)
                        async with self.manager.transition_lock:
                            await self.detach(binding)
                        break
                    except TimeoutError:
                        await asyncio.sleep(0.05)
                    except Exception:
                        self.failed = True
                        logger.exception("Runtime plugin detach retry: id=%s", binding.candidate.plugin_id)
                        await asyncio.sleep(1.0)
                async with self.manager.transition_lock:
                    self.failed = False
                    if binding is not restore or self.disposed:
                        await self.release(binding)
                    if restore is not None:
                        if self.disposed:
                            await self.release(restore)
                        else:
                            await self.mount(restore)
            except Exception:
                self.failed = True
                logger.exception("Runtime plugin close recovery failed: id=%s", binding.candidate.plugin_id)
                if binding.state == "unmounted":
                    await self.release(binding)
                    if restore is not None:
                        await self.release(restore)
            finally:
                self.recovery = None
                self.recovery_restore = None
                await self.manager.release_observer()

        self.recovery = asyncio.create_task(recover(), name=f"plugin-close-{self.name}")

    @asynccontextmanager
    async def transition(self, target: PluginCandidate | None):
        if self.disposed:
            raise RuntimeError("Plugin runtime is closed")
        if self.recovery is not None:
            raise RuntimeError("Plugin closing_timeout: previous writer is still closing")
        previous = self.binding
        old_model = previous.candidate.runtime.services.get("main_model") if previous else None
        new_model = target.runtime.services.get("main_model") if target else None
        same_model = old_model is new_model or (
            old_model is not None and new_model is not None
            and old_model[0] is new_model[0] and old_model[1] == new_model[1]
        )
        same = previous is not None and target is not None and (
            previous.candidate.fingerprint == target.fingerprint
            and same_model
            and previous.state == "active"
        )
        if same or (previous is None and target is None):
            yield
            return
        self.prepared = await self.manager.prepare(target) if target is not None else None
        self.recovery_restore = previous
        try:
            if previous is not None:
                try:
                    await self.detach(previous)
                except BaseException:
                    self.watch(previous, previous)
                    raise
            yield
            if target is not None and self.binding is not self.prepared:
                raise RuntimeError("Requested runtime plugin did not load")
            if previous is not None:
                await self.release(previous)
            self.generation += 1
        except BaseException as exc:
            if self.recovery is None:
                current = self.binding
                if current is not None and current is not previous:
                    try:
                        await self.detach(current)
                    except BaseException:
                        self.watch(current, previous)
                        raise
                if previous is not None and previous.state == "unmounted":
                    try:
                        await self.mount(previous)
                    except BaseException as restore_exc:
                        self.failed = True
                        if self.recovery is None:
                            await self.release(previous)
                        raise RuntimeError(
                            "Plugin candidate failed and the previous plugin could not be restored",
                        ) from restore_exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"Plugin equipment transition failed: {exc}") from exc
        finally:
            prepared, self.prepared = self.prepared, None
            if prepared is not None and self.binding is not prepared and self.recovery_restore is not prepared:
                await self.release(prepared)
            if self.recovery is None:
                self.recovery_restore = None


class PluginRuntimeManager:
    """Coordinate declarative native plugins and a fixed set of host services."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.slots: dict[str, _PluginSlot] = {}
        self._lock = asyncio.Lock()
        self._observer: Any = None
        self.close_timeout, self.close_grace = 10.0, 5.0
        self._disposed = False

    @property
    def bindings(self) -> dict[str, PluginBinding]:
        return {name: slot.binding for name, slot in self.slots.items() if slot.binding is not None}

    @property
    def host(self) -> PluginRuntimeHost:
        return self.adapter.get_plugin_runtime_host()

    @property
    def transition_lock(self) -> asyncio.Lock:
        return self._lock

    def _candidate(self, name: str, path: Path, version: str, params: dict, session_id: str) -> PluginCandidate | None:
        from openjiuwen.harness.resources.extension_loader import load_plugin_package
        from openjiuwen.harness.manifest.meta_elements import _load_class_from_file

        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        metadata = manifest.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Plugin metadata must be a mapping")
        if "coding_runtime" in metadata:
            raise ValueError("Legacy coding runtime is unsupported; upgrade the plugin to plugin_runtime contract v1")
        contract = metadata.get("plugin_runtime")
        if contract is None:
            return None
        if (not isinstance(contract, dict) or type(contract.get("contract_version")) is not int
                or contract.get("contract_version") != 1):
            raise ValueError("Unsupported plugin_runtime contract; expected v1")
        requirements = contract.get("requires", [])
        capabilities = contract.get("capabilities", [])
        profiles = contract.get("profiles", [])
        direct_tools = contract.get("direct_tools", [])
        for key, values in (("requires", requirements), ("capabilities", capabilities),
                            ("profiles", profiles), ("direct_tools", direct_tools)):
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                raise ValueError(f"plugin_runtime.{key} must be a string list")
            if len(values) != len(set(values)):
                raise ValueError(f"plugin_runtime.{key} contains duplicates")
        if set(requirements) - _SERVICES:
            raise ValueError("Plugin requested an unsupported host service")
        if not profiles or set(profiles) - {"code", "work"} or contract.get("role_scope") != "main":
            raise ValueError("Runtime plugins support declared code/work main profiles only")
        mode_params = dict(params)
        host = self.host
        default_profile = host.profile
        if mode_params.get("mode") is None:
            mode_params["mode"] = "code" if default_profile == "code" else "agent"
        mode = resolve_request_mode(mode_params, resolve_agent_request_mode)
        profile = "code" if mode.is_code_profile else "work"
        if profile not in profiles or profile != default_profile:
            raise ValueError("Plugin is supported only on a declared main agent profile")
        if mode.is_team or params.get("team"):
            raise ValueError("Plugin is supported only on a declared main agent profile")
        if params.get("sub_mode") == "team" or params.get("submode") == "team":
            raise ValueError("Plugin is supported only on a declared main agent profile")
        if getattr(host.instance, "_jiuwenswarm_code_team_member", False):
            raise ValueError("Plugin is supported only on a declared main agent profile")
        spec = load_plugin_package(manifest_path)
        if not spec.rails:
            raise ValueError("Runtime plugin must declare at least one Rail")
        config_path = path / "config.yaml"
        package_config = {}
        if config_path.is_file():
            if not config_path.resolve().is_relative_to(path.resolve()):
                raise ValueError("Plugin config.yaml must stay inside the package")
            package_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = host.config
        plugin_config = config.get("plugins", {})
        if not isinstance(plugin_config, dict):
            raise ValueError("Host plugins configuration must be a mapping")
        overrides = plugin_config.get(spec.id, {})
        if not isinstance(package_config, dict) or not isinstance(overrides, dict):
            raise ValueError("Plugin configuration must be a mapping")
        services: dict[str, Any] = {}
        if "main_model" in requirements:
            model, model_name = host.main_model
            if model is None or not model_name:
                raise ValueError("Plugin requires the current main model")
            services["main_model"] = (model, model_name)
        if "agent_data_workspace" in requirements:
            services["agent_data_workspace"] = host.agent_data_workspace.resolve()
        if "embedding_factory" in requirements:
            from jiuwenswarm.agents.harness.common.memory.embeddings import OpenAICompatibleEmbeddingProvider
            services["embedding_factory"] = OpenAICompatibleEmbeddingProvider
        runtime = PluginRuntimeServices(
            plugin_id=spec.id, session_id=session_id, member_card_id=str(host.instance.card.id),
            profile=profile, services=services, host_config=config, package_config=package_config, options=overrides,
        )
        # Business preflight runs before any old owner is detached or any lease acquired.
        for item in manifest.get("rails", []):
            if not isinstance(item, dict) or not item.get("file"):
                raise ValueError("Runtime plugin Rails must use file/class declarations")
            rail_path = (path / item["file"]).resolve()
            if not rail_path.is_relative_to(path.resolve()):
                raise ValueError("Plugin Rail must stay inside the package")
            cls = _load_class_from_file(rail_path, item.get("class"), package_root=path)
            prepare = getattr(cls, "prepare_plugin_context", None)
            if callable(prepare):
                options = prepare(runtime)
                if inspect.isawaitable(options):
                    if inspect.iscoroutine(options):
                        options.close()
                    raise TypeError("Plugin preflight must be synchronous")
                if not isinstance(options, dict):
                    raise ValueError("Plugin preflight must return an options mapping")
                runtime = runtime.with_options(options)
        # Include business configuration changes without logging credentials.
        digest = hashlib.sha256(json.dumps({"manifest": manifest, "config": config,
                                           "package_config": package_config}, sort_keys=True).encode()).hexdigest()
        return PluginCandidate(name, version, path, contract, runtime, digest)

    async def prepare(self, candidate: PluginCandidate) -> PluginBinding:
        from jiuwenswarm.agents.harness.agent_observability import acquire_plugin_trajectory_demand

        lease, snapshot = None, None
        try:
            services = dict(candidate.runtime.services)
            if "trajectory" in candidate.contract.get("requires", []):
                from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
                acquisition = asyncio.create_task(asyncio.to_thread(
                    acquire_plugin_trajectory_demand, candidate.runtime.session_id, candidate.plugin_id,
                ))
                lease, cancelled = await _settle_thread_task(acquisition)
                if cancelled:
                    raise asyncio.CancelledError
                services["trajectory"] = get_trajectory_span_processor()
                if services["trajectory"] is None:
                    raise RuntimeError("Plugin trajectory processor is unavailable")
            snapshot = tempfile.TemporaryDirectory(prefix="jiuwenswarm-plugin-", ignore_cleanup_errors=True)
            copying = asyncio.create_task(asyncio.to_thread(shutil.copytree, candidate.path,
                                         Path(snapshot.name), dirs_exist_ok=True, symlinks=True))
            _, cancelled = await _settle_thread_task(copying)
            if cancelled:
                raise asyncio.CancelledError
            manifest = json.loads((Path(snapshot.name) / "manifest.json").read_text(encoding="utf-8-sig"))
            if (
                manifest.get("id") != candidate.plugin_id
                or str(manifest.get("version", "")).strip() != candidate.version
            ):
                raise ValueError("Plugin package changed while preparing runtime; retry")
            return PluginBinding(
                candidate,
                replace(candidate.runtime, services=services, resources=[]),
                snapshot,
                lease,
            )
        except BaseException:
            if snapshot is not None:
                snapshot.cleanup()
            if lease is not None:
                await _settle_thread_task(asyncio.create_task(asyncio.to_thread(lease.release)))
            raise

    async def ensure_observer(self, binding: PluginBinding) -> None:
        from openjiuwen.harness.observability.rail import AgentObservabilityRail
        instance = self.host.instance
        if binding.lease is not None and instance.find_rail_by_name("AgentObservabilityRail") is None:
            self._observer = AgentObservabilityRail()
            await instance.register_rail(self._observer)

    async def release_observer(self) -> None:
        if (self._observer is not None and not any(b.lease is not None for b in self.bindings.values())
                and not any(s.recovery for s in self.slots.values())):
            await self.host.instance.unregister_rail(self._observer)
            self._observer = None

    def refresh_tool_exposure(self) -> None:
        from openjiuwen.core.foundation.tool import ToolExposure
        for binding in self.bindings.values():
            for name in binding.candidate.contract.get("direct_tools", []):
                card = self.host.instance.ability_manager.get(name)
                if card is not None:
                    card.exposure = ToolExposure.DIRECT
                    declared = getattr(card, "set_exposure_declared", None)
                    if callable(declared):
                        declared(True)

    @asynccontextmanager
    async def transition(self, params: dict, session_id: str):
        from jiuwenswarm.server.runtime import extension_package_manager as equipment
        async with self._lock:
            if self._disposed:
                raise RuntimeError("Plugin runtime manager is closed")
            targets, owners = {}, {}
            names = params.get("plugin_names", [])
            if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
                raise ValueError("plugin_names must be a list of non-empty strings")
            for name in names:
                path = equipment.resolve_plugin_dir(name)
                version = equipment.read_manifest_version(path)
                try:
                    candidate = await asyncio.to_thread(self._candidate, name, path, version, params, session_id)
                except Exception as exc:
                    raise ValueError(f"Invalid runtime plugin {name}: {exc}") from exc
                if candidate is None:
                    continue
                for capability in candidate.contract.get("capabilities", []):
                    if capability in owners:
                        raise ValueError(f"Exclusive plugin capability already requested: {capability}")
                    owners[capability] = name
                if any(other.plugin_id == candidate.plugin_id for other in targets.values()):
                    raise ValueError("A plugin id may be mounted only once per agent")
                targets[name] = candidate
            for name, slot in self.slots.items():
                if slot.recovery is not None:
                    raise RuntimeError(f"Plugin closing_timeout: {name} still has a closing writer")
            changed = set(targets) != set(self.bindings)
            for name, candidate in targets.items():
                previous = self.bindings.get(name)
                if previous is None or previous.candidate.fingerprint != candidate.fingerprint:
                    changed = True
                elif "main_model" in candidate.runtime.services:
                    old = previous.candidate.runtime.services["main_model"]
                    new = candidate.runtime.services["main_model"]
                    changed = changed or old[0] is not new[0] or old[1] != new[1]
            host = self.host
            if changed and (host.has_conflicting_turn(session_id) or host.wants_attach_goal(params)):
                raise RuntimeError("Plugin runtime change rejected at non-fresh turn")
            try:
                async with AsyncExitStack() as stack:
                    for name in dict.fromkeys([*self.slots, *targets]):
                        slot = self.slots.setdefault(name, _PluginSlot(self, name))
                        await stack.enter_async_context(slot.transition(targets.get(name)))
                    yield
                    for name in targets:
                        slot = self.slots[name]
                        if slot.prepared is not None and slot.binding is not slot.prepared:
                            raise RuntimeError(f"Requested runtime plugin did not load: {name}")
            finally:
                await self.release_observer()

    async def load(self, name: str) -> Any | None:
        slot = self.slots.get(name)
        return await slot.mount(slot.prepared) if slot is not None and slot.prepared is not None else None

    async def remove(self, name: str) -> bool:
        async with self._lock:
            slot = self.slots.get(name)
            if slot is None or slot.binding is None:
                return False
            binding = slot.binding
            try:
                await slot.detach(binding)
            except BaseException:
                slot.watch(binding, None if slot.disposed else binding)
                raise
            await slot.release(binding)
            await self.release_observer()
            return True

    async def close(self) -> None:
        self._disposed = True
        for slot in self.slots.values():
            slot.disposed = True
        recoveries = {slot.recovery for slot in self.slots.values() if slot.recovery is not None}
        if recoveries:
            _, pending = await asyncio.wait(recoveries, timeout=self.close_timeout + self.close_grace)
            if pending:
                raise TimeoutError("Plugin closing_timeout: session dependencies are still in use")
        for name in list(self.bindings):
            await self.remove(name)
        await self.release_observer()

    def status(self, name: str) -> dict[str, Any]:
        slot = self.slots.get(name)
        binding = slot.binding if slot else None
        return {"owner": "plugin" if binding else "none",
                "state": binding.state if binding else ("failed" if slot and slot.failed else "unmounted"),
                "generation": slot.generation if slot else 0,
                "plugin_id": binding.candidate.plugin_id if binding else None,
                "version": binding.candidate.version if binding else None}


def get_plugin_runtime_manager(adapter: Any) -> PluginRuntimeManager:
    return adapter.get_plugin_runtime_manager()
