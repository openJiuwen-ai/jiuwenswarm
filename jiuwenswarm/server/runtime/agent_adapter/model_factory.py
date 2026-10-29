# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model construction and Kernel leases, independent of session execution."""

from __future__ import annotations

import logging
import copy
from typing import Any, Callable, MutableMapping


logger = logging.getLogger(__name__)


class ModelFactory:
    """Materialize only selected recipes and retain their Kernel leases.

    Catalog mappings and leases belong here; compatibility properties expose
    them to the adapter. Request authorization remains an explicit adapter port:
    construction reuses the original request and never fabricates credentials.
    Failed clients are never cached.
    """

    def __init__(self, build_entry: Callable[[dict, dict], Any]) -> None:
        self._build_entry = build_entry
        self.leases: dict[Any, Any] = {}
        self.catalog: dict[str, dict] = {}
        self.configuration: dict[str, Any] = {}

    def acquire(
        self, identity: Any, client: dict, config: dict, *, resource_id: str = ""
    ) -> Any:
        from jiuwenswarm.runtime.kernel import (
            ResourceKey,
            identity_reference,
        )

        key = ResourceKey(
            identity,
            "model",
            resource_id or str(client.get("model_name") or "legacy"),
            identity_reference(
                (client.get("api_base"), client.get("endpoint_profile"))
            ),
            identity_reference(client),
            identity_reference((client, config)),
        )
        return self._acquire(key, lambda: self._build_entry(client, config))

    def _acquire(self, key: Any, build: Callable[[], Any]) -> Any:
        from jiuwenswarm.runtime.kernel import process_kernel
        from jiuwenswarm.server.runtime.agent_adapter.model_resources import (
            close_model_entry,
            register_model_connections,
        )

        lease = self.leases.get(key)
        if lease is None:
            kernel = process_kernel()

            def build_owned():
                model = build()
                register_model_connections(kernel, key.identity, model)
                return model

            lease = kernel.acquire(key, build_owned, close=close_model_entry)
            self.leases[key] = lease
        value = lease.value
        from jiuwenswarm.runtime.model_observation import record_model_use

        record_model_use(value)
        return value

    def resolve_selection(
        self, owner: Any, requested: str, can_access=None, *, catalog=None
    ) -> Any:
        """Authorize every lookup before borrowing a shared compiled selection.

        A warm model is never an authorization cache. The resolver checks the
        current catalog and every selected route before Kernel can return a
        previously prepared object. Its resolved content also keys credentials
        and route overrides, so edits cannot reuse an obsolete client.
        """
        from gateway.common.model_selection import ModelSelection
        from jiuwenswarm.runtime.kernel import (
            KernelIdentity,
            ResourceKey,
            identity_reference,
        )
        from jiuwenswarm.server.runtime.model_compiler_adapter import (
            build_model_from_selection,
        )
        from jiuwenswarm.server.runtime.model_routing_registry import (
            ModelExecutionContext,
            ModelSelectionResolver,
        )

        selection_type, selection_id, *route_parts = requested.split(":", 2)
        selection = ModelSelection(
            type=selection_type,
            id=selection_id,
            route_id=route_parts[0] if route_parts else None,
        )
        resolver = (
            ModelSelectionResolver(catalog)
            if catalog is not None
            else ModelSelectionResolver()
        )
        resolved = resolver.resolve(
            selection, ModelExecutionContext(can_access=can_access)
        )
        identity = getattr(owner, "_kernel_identity", None)
        if identity is None:
            identity = owner._kernel_identity = KernelIdentity.for_agent(
                project_dir=str(getattr(owner, "_project_dir", "") or ""),
                work_mode=str(getattr(owner, "_session_instance_mode", "agent")),
                definition=getattr(owner, "_session_instance_agent_definition", None),
                capabilities={},
                configuration=getattr(owner, "_config_base_cache", {}),
            )
        key = ResourceKey(
            identity,
            "model",
            requested,
            identity_reference((selection.type, selection.id, selection.route_id)),
            identity_reference(resolved.model_dump()),
            identity_reference(resolved.model_dump()),
        )
        return self._acquire(key, lambda: build_model_from_selection(resolved))

    @staticmethod
    def materialize(
        cache_key: str,
        *,
        recipes: dict[str, dict],
        aliases: dict[str, str],
        cache: MutableMapping[str, Any],
        build: Callable[..., Any],
    ) -> Any | None:
        # Do not recurse through the lazy mapping's get() implementation.
        cached = dict.get(cache, cache_key)
        if cached is not None:
            return cached
        entry = recipes.get(cache_key)
        if entry is None:
            return None
        client = entry.get("model_client_config") or {}
        try:
            model = build(
                client,
                entry.get("model_config_obj") or {},
                resource_id=str(entry.get("model_id") or client.get("model_name")),
            )
        except Exception as exc:
            logger.warning("model recipe %s failed: %s", cache_key, exc)
            return None
        cache[cache_key] = model
        for alias, target in aliases.items():
            if target == cache_key:
                cache[alias] = model
        return model

    def register_recipe(
        self,
        owner,
        entry: dict[str, Any],
        name_counter: dict[str, int],
    ) -> str | None:
        """Register one model entry into the request-selectable model cache.

        Register the recipe without constructing an unused provider client.
        Returns its cache key, or None when the entry has no model name.
        The caller pairs this return value with the entry's global list index
        to populate ``_global_index_to_cache_key``.
        """
        mcc = entry.get("model_client_config") or {}
        if not mcc.get("model_name"):
            return None
        model_name = mcc["model_name"]
        idx = name_counter.get(model_name, 0)
        name_counter[model_name] = idx + 1
        cache_key = f"{model_name}#{idx}"
        owner._model_recipes[cache_key] = copy.deepcopy(entry)
        if model_name not in owner._model_name_to_keys:
            owner._model_name_to_keys[model_name] = []
        owner._model_name_to_keys[model_name].append(cache_key)

        model_id = str(entry.get("model_id") or "").strip()
        if model_id:
            owner._model_id_to_key[model_id] = cache_key

        # 同时用纯 model_name 作为 key 指向 is_default=true 的条目
        if entry.get("is_default") is True:
            owner._model_alias_to_key[model_name] = cache_key

        alias = entry.get("alias") or ""
        if alias and alias != model_name:
            owner._model_alias_to_key.setdefault(alias, cache_key)

        return cache_key

    def register_catalog(
        self, owner, config: dict, *, available_models: Callable
    ) -> None:
        """从 models.defaults 列表构建模型缓存。

        key 使用 {model_name}#{index} 格式以支持同名模型共存。
        同时记录 _model_name_to_keys 映射以便按 model_name 查找，
        以及 _global_index_to_cache_key 映射以便在 _resolve_model_by_name
        回退分支把通道侧的全局 origin_index 换算成真实 cache_key。
        """
        owner._model_name_to_keys.clear()
        owner._global_index_to_cache_key.clear()
        owner._model_id_to_key.clear()
        owner._model_recipes = {}
        owner._model_alias_to_key = {}
        name_counter: dict[str, int] = {}

        # 含登录后自动获得的模型
        for global_idx, entry in enumerate(available_models(config)):
            cache_key = owner._register_model_cache_entry(entry, name_counter)
            if cache_key is not None:
                owner._global_index_to_cache_key[global_idx] = cache_key

    def build_legacy(self, owner, config: dict) -> None:
        """回退到旧格式（models.default / react 段）构建单条目缓存。"""
        default_model_config = config.get("models", {}).get("default", {})
        react_config = config.get("react", {})

        mcc = dict(
            default_model_config.get("model_client_config")
            or react_config.get("model_client_config")
            or {}
        )
        model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
        if "model_name" not in mcc:
            mcc["model_name"] = model_name

        mco = (
            default_model_config.get("model_config_obj")
            or react_config.get("model_config_obj")
            or {}
        )
        try:
            owner._model_cache[model_name] = owner._build_kernel_model(mcc, mco)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] 跳过无效模型条目(legacy) %s: %s",
                model_name,
                exc,
            )

    def configure(
        self, owner, config: dict, *, is_placeholder: Callable, client_view: Callable
    ) -> Any:
        from jiuwenswarm.runtime.kernel import KernelIdentity, process_kernel

        self.configuration = config
        identity = KernelIdentity.for_agent(
            project_dir=str(getattr(owner, "_project_dir", "") or ""),
            work_mode=str(getattr(owner, "_session_instance_mode", "agent")),
            definition=getattr(owner, "_session_instance_agent_definition", None),
            capabilities={
                name: config.get(name)
                for name in ("permissions", "tools", "skills", "mcp")
            },
            configuration=config,
        )
        previous = getattr(owner, "_kernel_identity", None)
        if previous is not None and previous != identity:
            process_kernel().retire(lambda key: key.identity == previous)
        owner._kernel_identity = identity
        # 指纹比对：模型配置未变且缓存非空时跳过重建，避免热更新时反复销毁/重建 Model 实例
        new_fp = owner._models_config_fingerprint(config)
        same_config = (
            previous == identity and new_fp == owner._last_models_config_fingerprint
        )
        if same_config and owner._model_cache and owner._model is not None:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] skip model cache rebuild: config unchanged"
            )
            return owner._model

        owner._model_cache.clear()
        owner._model_recipes = {}
        owner._model_alias_to_key = {}
        owner._model_name_to_keys.clear()
        owner._global_index_to_cache_key.clear()
        owner._model_id_to_key.clear()
        owner._model_group_cache.clear()
        owner._model = None
        owner._stable_selection_cache = {}
        owner._inject_attribution_to_config(config)
        owner._build_model_cache_from_defaults(config)
        recipes = getattr(owner, "_model_recipes", {})
        default_keys = []
        for name in owner._model_name_to_keys:
            if name not in owner._model_alias_to_key:
                continue
            key = owner._model_alias_to_key[name]
            if recipes[key].get("is_default") is True:
                default_keys.append(key)
        owner._configured_default_model_keys = list(
            dict.fromkeys([*default_keys, *recipes])
        )
        default_groups = [
            group
            for group in (config.get("models", {}).get("groups") or [])
            if isinstance(group, dict)
            and group.get("enabled", True)
            and group.get("is_default")
        ]
        owner._configured_default_group_id = (
            str(default_groups[0].get("model_group_id") or "") if default_groups else ""
        )
        requested = str(
            getattr(owner, "_initial_execution_model_name", "") or ""
        ).strip()
        selected = None
        execution_request = getattr(owner, "_initial_execution_request", None)
        # The bootstrap context may contain forwarded credentials. Keep it
        # only for this construction, rather than for the Session's lifetime.
        owner._initial_execution_request = None
        if execution_request is not None and requested.startswith(
            ("model:", "model_group:")
        ):
            # Reuse the real request's authorization and route resolution. A
            # fabricated bootstrap request would lose caller access metadata.
            selected = owner._resolve_model_for_request(execution_request)
            owner._model_cache[requested] = selected
        elif requested.startswith("model:"):
            key = owner._model_id_to_key.get(requested.removeprefix("model:"))
            if key is not None:
                selected = owner._materialize_model_recipe(key)
        elif requested and not requested.startswith("model_group:"):
            selected = owner._resolve_model_by_name(requested)
        if selected is None:
            selected = owner._configured_default_model()
        if not owner._model_cache:
            owner._build_model_cache_legacy(config)

        if not owner._model_cache:
            raise ValueError(
                "No valid model entries found in config — all entries failed validation. "
                "Check that api_key and api_base are set for at least one model."
            )

        if selected is None:
            selected = next(iter(owner._model_cache.values()))
        names = [
            name for name, model in owner._model_cache.items() if model is selected
        ]
        default_name = next((name for name in names if "#" not in name), names[0])

        # 首次启动兜底：config 默认条目仍为 .env 占位符时，改选 Zen 免费模型
        # （如 DeepSeek V4 Flash）作为默认，避免把占位模型发往厂商。
        # 仅内存态生效，不回写 config.yaml；Zen 不可达时保持原占位行为。
        if is_placeholder(
            client_view(owner._model_cache[default_name].model_client_config)
        ):
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_default_free_model_entry,
            )

            zen_default = get_zen_default_free_model_entry()
            if zen_default is not None:
                zmcc = zen_default["model_client_config"]
                zname = zmcc["model_name"]
                owner._model_cache[zname] = owner._build_kernel_model(
                    zmcc, zen_default.get("model_config_obj") or {}
                )
                default_name = zname

        owner._default_model_name = default_name
        owner._model = owner._model_cache[default_name]
        owner._model_client_config = owner._model.model_client_config
        owner._model_request_config = owner._model.model_config
        owner._selected_model_context_window_tokens = getattr(
            owner._model_request_config,
            "context_window",
            None,
        )
        owner._last_models_config_fingerprint = new_fp
        return owner._model


def build_model_from_entry(mcc: dict, mco: dict) -> Any:
    """根据单个模型条目的 model_client_config / model_config_obj 构建 Model 实例。

    模块级公开函数：除本适配器外，模型缓存构建（``agent_ws_server`` /
    ``auto_harness.scheduler``）与图像模态探测预热（``image_modality_warmup``）
    都要按同一规则从 ``models.defaults`` 条目造 Model，共享这一份实现。

    Args:
        mcc: 条目的 ``model_client_config`` 段，``model_name`` 单独取出。
        mco: 条目的 ``model_config_obj`` 段。

    Returns:
        按该条目配置构建的 Model 实例。
    """
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )
    from jiuwenswarm.common.reasoning_config import resolve_endpoint_profile_override
    from jiuwenswarm.common.reasoning_injector import (
        build_reasoning_model_request_kwargs,
    )

    name = mcc.get("model_name", "")
    mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
    if not mcc_fields.get("client_provider"):
        mcc_fields["client_provider"] = "OpenAI"
    # 已知自建网关（如 DashScope 风格端点）按 api_base host 补全 endpoint_profile，
    # 覆盖尚未重新保存过的存量条目；显式配置的方言优先。core 依赖该字段
    # 选择线格式（enable_thinking + thinking_budget），档位仍按模型名。
    if not mcc_fields.get("endpoint_profile"):
        _inferred_profile = resolve_endpoint_profile_override(
            mcc_fields.get("api_base") or mcc_fields.get("base_url")
        )
        if _inferred_profile:
            mcc_fields["endpoint_profile"] = _inferred_profile

    # ``context_window``（模型支持的上下文总长度）可配在任意模型条目的 mco 里
    # （defaults / agentos / video / audio / vision / image_gen 均可），经
    # ``build_reasoning_model_request_kwargs`` 摊开进入 kwargs，进 core 的
    # ``ModelRequestConfig`` 供 core 人员取值。
    # 是否在出口 pop 取决于 core 是否已把 context_window 加为 ModelRequestConfig
    # 正式字段（见 ``reasoning_injector.core_has_context_window_field``，自动适配）：
    # - core 未加字段（过渡期）：context_window 进 extra 会被
    #   ``base_model_client._build_request_params`` 经 ``model_dump`` 透传给厂商
    #   SDK 报 unexpected keyword argument -> reasoning_injector 公共出口 pop 防发厂商。
    # - core 已加字段：context_window 作正式字段，core 自行 exclude 不发厂商、
    #   ``self.model_config.context_window`` 可读 -> 不 pop，留给 core。
    # 出口 pop 不再守 _source=="agentos"：所有条目一视同仁，defaults 配了
    # context_window 同样过渡期 pop 防发厂商。agentos 条目的 mco 含内部标记
    # ``_source == "agentos"``（由 ``get_default_models`` 注入），仅用于前端
    # is_agentos 置灰只读展示，不再参与 context_window 出口判断。
    # 不再挂 ``_agentos_ctx_window`` 普通属性：旧机制是把 context_window 喂给
    # ``ContextEngineConfig.context_window_tokens``（压缩阈值）的桥接，已拆除
    # （见 ``_deep_agent_context_engine_config``）。
    request_kwargs = build_reasoning_model_request_kwargs(
        model_client_config=mcc_fields,
        model_config_obj=mco,
        model_name=name,
    )
    m_config = ModelRequestConfig(**request_kwargs)
    model = Model(
        model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config
    )
    return model
