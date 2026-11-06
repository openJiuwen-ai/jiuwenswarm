"""Harness-only provider assembly; optional RSI engines are not imported."""
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter

def build_mock_rsi_adapters(tasks_root, *, model_resolver=None):
    from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import MockHarnessProvider
    return {"HARNESS": HarnessEngineAdapter(MockHarnessProvider(tasks_root))}

def build_rsi_adapters(tasks_root, *, mode="real", harness_provider=None, model_resolver=None, **kwargs):
    if mode == "mock":
        return build_mock_rsi_adapters(tasks_root, model_resolver=model_resolver)
    if harness_provider is None:
        from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider
        harness_provider = HarnessProvider(tasks_root, model_resolver=model_resolver)
    return {"HARNESS": HarnessEngineAdapter(harness_provider)}
