from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from jiuwenswarm.symphony.skill_retrieval.dispatch_imports import dispatch_import_path


class _FailingCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise RuntimeError("invalid api key")


class _StaticCompletions:
    def __init__(self, content: str, *, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(finish_reason=self.finish_reason, message=message)
        return SimpleNamespace(choices=[choice])


def _fake_builder(completions: object) -> SimpleNamespace:
    return SimpleNamespace(
        DEFAULT_CONTEXT_WINDOW=128000,
        DEFAULT_MAX_OUTPUT_TOKENS=32768,
        model="fake-model",
        _client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        _manager_config=SimpleNamespace(build=SimpleNamespace(num_retries=2, timeout=1.0)),
        _llm_seed=None,
        _max_output_tokens_cache=None,
        _counter_lock=threading.Lock(),
        _llm_calls=0,
        _retry_calls=0,
        _cache_hits=0,
        _cache_misses=0,
        _cache_unknown=0,
        _prompt_fingerprints=set(),
        _progress=None,
        _progress_task=None,
        _llm_semaphore=threading.Semaphore(1),
        _thread_local=threading.local(),
        _consecutive_failures=0,
        max_consecutive_failures=5,
    )


def test_tree_llm_runtime_raises_on_model_failure() -> None:
    with dispatch_import_path():
        from indexing.tree.llm_runtime import TreeLLMRuntime

        completions = _FailingCompletions()
        builder = _fake_builder(completions)

        with pytest.raises(RuntimeError, match="model call failed"):
            TreeLLMRuntime(builder).call_llm("build a skill tree")

        assert completions.calls == 1


def test_tree_llm_runtime_raises_after_invalid_json_retries() -> None:
    with dispatch_import_path():
        from indexing.tree.llm_runtime import TreeLLMRuntime

        completions = _StaticCompletions("not json")
        with pytest.raises(RuntimeError, match="valid JSON object"):
            TreeLLMRuntime(_fake_builder(completions)).call_llm_json("build a skill tree", max_retries=2)

        assert completions.calls == 2


def test_tree_llm_runtime_raises_on_truncated_json() -> None:
    with dispatch_import_path():
        from indexing.tree.llm_runtime import TreeLLMRuntime

        completions = _StaticCompletions('{"groups": {}', finish_reason="length")
        with pytest.raises(RuntimeError, match="truncated"):
            TreeLLMRuntime(_fake_builder(completions)).call_llm_json("build a skill tree")
