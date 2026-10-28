# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A model connection scope cannot leak into unrelated Channels or raw writes."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common.runtime_model_config import (
    apply_runtime_model_config,
    runtime_model_config,
)


def _entry(name):
    return {
        "model_client_config": {"model_name": name, "api_key": "private-key"},
        "is_default": True,
    }


def test_parsed_and_raw_config_and_disk_remain_unchanged(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  defaults: []\n  groups: []\n", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    baseline = config.get_config()
    raw = config.get_config_raw()
    with runtime_model_config(_entry("temporary")):
        resolved = config.get_config()
        assert resolved["models"]["defaults"][0] == _entry("temporary")
        assert config.get_config_raw() == raw
        assert path.read_bytes() == before
        resolved["models"]["defaults"][0]["model_client_config"]["api_key"] = "changed"
        assert (
            config.get_config()["models"]["defaults"][0]["model_client_config"][
                "api_key"
            ]
            == "private-key"
        )
    assert config.get_config() == baseline and path.read_bytes() == before


def test_nested_scope_restores_on_exception():
    base = {
        "models": {
            "defaults": [],
            "groups": [{"is_default": True}],
            "agentos": [_entry("agentos")],
        }
    }
    before = deepcopy(base)
    with runtime_model_config(_entry("outer")):
        with pytest.raises(RuntimeError):
            with runtime_model_config(_entry("inner")):
                assert apply_runtime_model_config(base)["models"]["defaults"][
                    0
                ] == _entry("inner")
                raise RuntimeError("test")
        models = apply_runtime_model_config(base)["models"]
        assert models["defaults"][0] == _entry("outer")
        assert models["groups"] == [] and models["agentos"] == []
    assert apply_runtime_model_config(base) is base and base == before


@pytest.mark.parametrize(
    "modality", ["video", "audio", "vision", "image_gen", "video_gen"]
)
def test_chat_override_preserves_independent_models_without_mutating_config(
    monkeypatch, modality
):
    multimodal = {
        "enabled": True,
        "model_client_config": {
            "model_name": "independent-" + modality,
            "api_key": "multimodal-key",
        },
        "model_config_obj": {"temperature": 0.1},
    }
    base = {
        "models": {
            "defaults": [_entry("original")],
            "groups": [{"is_default": True}],
            "agentos": [_entry("agentos")],
            modality: multimodal,
            "future_model_setting": {"enabled": True},
        }
    }
    before = deepcopy(base)
    monkeypatch.setattr(config, "get_config_file", lambda: "unused-model-config.yaml")
    monkeypatch.setattr(config, "_read_with_retry", lambda path: base)
    with runtime_model_config(_entry("temporary")):
        models = config.get_config()["models"]
        assert models["defaults"][0] == _entry("temporary")
        assert models["groups"] == [] and models["agentos"] == []
        assert models[modality] == multimodal
        assert models["future_model_setting"] == {"enabled": True}
        models[modality]["model_client_config"]["api_key"] = "changed"
        assert config.get_config()["models"][modality] == multimodal
    assert base == before
    assert config.get_config()["models"][modality] == multimodal


@pytest.mark.asyncio
async def test_parallel_channels_are_isolated_and_child_tasks_inherit():
    base = {"models": {"defaults": [_entry("web-default")]}}
    bound = asyncio.Event()
    checked = asyncio.Event()

    async def process_cli():
        with runtime_model_config(_entry("cli-only")):
            bound.set()
            await checked.wait()
            result = await asyncio.to_thread(apply_runtime_model_config, base)
            assert result["models"]["defaults"][0] == _entry("cli-only")
        assert apply_runtime_model_config(base) is base

    async def web_or_tui():
        await bound.wait()
        assert apply_runtime_model_config(base) is base
        checked.set()

    await asyncio.gather(process_cli(), web_or_tui())
    assert apply_runtime_model_config(base) is base
