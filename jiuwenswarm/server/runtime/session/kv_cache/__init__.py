"""Session-scoped KV cache lifecycle integration.

The package is deliberately split by responsibility:

* :mod:`kv_cache_application_runtime` owns the application-level Runtime instance;
* :mod:`kv_cache_model_provider` supplies only the historical-session fallback;
* :mod:`kv_cache_task_guard` converts product facts into logical lifecycle
  actions;
* :mod:`kv_cache_product_hooks` adapts Web/TUI/AgentServer events to those
  actions.

No symbols are re-exported here.  Callers import the owning module explicitly
so configuration, product state and Runtime ownership remain distinguishable.
"""
