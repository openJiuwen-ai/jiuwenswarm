# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the installed core counter with real local tokenizer artifacts."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from jiuwenswarm.common.native_tokenizer_cache import install_native_tokenizer_cache


@pytest.fixture
def cached_factory(monkeypatch):
    from openjiuwen.core.context_engine.token import tokenizer_selector

    original = tokenizer_selector.NativeTokenizerCounter
    monkeypatch.setattr(tokenizer_selector, "NativeTokenizerCounter", original)
    with patch.object(tokenizer_selector, "NativeTokenizerCounter", wraps=original) as loader:
        # MagicMock's dynamic attributes must not look like an installed patch.
        loader.jiuwen_instance_cache = False
        install_native_tokenizer_cache()
        yield tokenizer_selector.NativeTokenizerCounter, loader


def _artifact(path, extra=False):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel

    vocab = {"[UNK]": 0, "hello": 1}
    if extra:
        vocab["world"] = 2
    Tokenizer(WordLevel(vocab, unk_token="[UNK]")).save(str(path))


def test_native_counter_reused_across_concurrent_selectors(tmp_path, cached_factory):
    factory, loader = cached_factory
    artifact = tmp_path / "tokenizer.json"
    _artifact(artifact)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counters = list(pool.map(lambda _: factory(artifact, model="test"), range(64)))
    assert loader.call_count == 1
    assert all(counter is counters[0] for counter in counters)
    assert counters[0].count("hello") == 1
    assert counters[0].measurement_source == "native_tokenizer"


@pytest.mark.asyncio
async def test_context_creation_and_rebinding_reuse_the_same_loaded_artifact(tmp_path, cached_factory):
    from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
    from openjiuwen.core.foundation.llm import UserMessage

    _factory, loader = cached_factory
    artifact = tmp_path / "tokenizer.json"
    _artifact(artifact)
    config = ContextEngineConfig(
        model_name="test", model_provider="test", tokenizer_offline=True,
        tokenizer_cache_dir=str(tmp_path),
        tokenizer_spec={"model": "test", "provider": "test", "source": "local", "id": str(artifact)},
    )
    engine = ContextEngine(config)
    context = await engine.create_context(history_messages=[UserMessage(content="hello")])
    for _ in range(32):
        assert engine.rebind_context_model(config, session_id="default_session_id") == 0
    assert engine.get_context() is context
    assert loader.call_count == 1


def test_replaced_artifact_and_model_metadata_do_not_reuse_stale_counter(tmp_path, cached_factory):
    factory, loader = cached_factory
    artifact = tmp_path / "tokenizer.json"
    _artifact(artifact)
    exact = factory(artifact, model="test")
    family = factory(
        artifact, model="alias", measurement_source="family_tokenizer_fallback",
        fallback_reason="model_variant_tokenizer_family", fallback_tokenizer_model="test",
    )
    assert exact is not family
    assert family.measurement_estimated
    assert family.measurement_fallback_tokenizer_model == "test"
    _artifact(artifact, extra=True)
    replaced = factory(artifact, model="test")
    assert replaced is not exact
    assert loader.call_count == 3


def test_failed_load_is_retryable(tmp_path, cached_factory):
    factory, loader = cached_factory
    artifact = tmp_path / "tokenizer.json"
    artifact.write_text("broken", encoding="utf-8")
    with pytest.raises(Exception):
        factory(artifact)
    _artifact(artifact)
    assert factory(artifact).count("hello") == 1
    assert loader.call_count == 2


def test_cache_capacity_and_installation_are_bounded(tmp_path, cached_factory):
    from openjiuwen.core.context_engine.token import tokenizer_selector

    factory, loader = cached_factory
    artifact = tmp_path / "tokenizer.json"
    _artifact(artifact)
    install_native_tokenizer_cache()
    assert tokenizer_selector.NativeTokenizerCounter is factory
    first = factory(artifact, model="0")
    for index in range(1, 34):
        factory(artifact, model=str(index))
    assert factory(artifact, model="0") is not first
    assert loader.call_count == 35
