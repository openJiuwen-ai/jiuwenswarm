# Native plugin runtime

Ordinary zero-argument Rails retain their existing loading path. Plugins needing host services declare metadata.plugin_runtime with contract_version=1, profiles=["code"] and/or ["work"], role_scope="main", requires, capabilities and optional direct_tools.

Available services are main_model (model/name pair), trajectory (shared processor with a per-mount lease), agent_data_workspace (absolute data-root Path) and embedding_factory (provider construction). Call runtime.require(name); undeclared or unavailable services fail explicitly. New plugins using these services need only a ZIP, not another host manager. New fundamental services still require host support.

Capabilities are package-defined exclusive names; conflicting target plugins are rejected before acquisition. Multiple independent plugins may coexist. direct_tools marks existing tools DIRECT and does not create missing tools. Team, child-agent and external-Harness runtimes are not supported.

The optional synchronous prepare_plugin_context(runtime) validates business configuration and returns a dict of effective options before any old binding is detached. It may read data needed for validation but must not start background work or write business data. runtime.package_config is config.yaml; runtime.options initially contains host plugins[manifest.id]; runtime.host_config contains the host snapshot. These mappings are read-only.

The file Rail's from_plugin_context factory obtains context.extras["plugin_runtime"], constructs its own configuration and instance, and calls runtime.register_resource(rail) before returning. That registration enables cleanup when init fails before a complete load record exists. Plugins own their algorithms, configuration schema, storage validation and asynchronous aclose(*, timeout, grace_timeout).

The trajectory service becomes available only after lease acquisition: request it in from_plugin_context, never in prepare_plugin_context. Other declared services may be used during preflight.

The host manages per-plugin bindings, service leases, package snapshots, fresh-turn changes, rollback and asynchronous drain. Default closing budgets are 10 seconds plus 5 seconds grace. Pending closers are retained, never cancelled to pretend a thread stopped. Deselect failures restore the previous binding after drain; session disposal releases it. Rollback does not undo arbitrary ordinary-plugin Python side effects or business-file writes.

TTSE 2.0.0 uses this generic contract. Its ZIP owns option validation, legacy configuration compatibility, bank validation, TTSEConfig, induction policy and Rail construction. Upgrade the host and ZIP together; legacy coding_runtime declarations from TTSE 1.x are rejected. Core still supplies synchronous file Rail factories, partial-binding cleanup and the TTSE algorithms/close implementation; this host refactor adds no core API.
