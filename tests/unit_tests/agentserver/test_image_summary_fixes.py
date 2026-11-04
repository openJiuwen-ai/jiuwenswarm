# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""回归测试：本 PR 修复的评审建议项（atomgit-bot 4 条）。

覆盖：
- image_summary_rail._model_credential_from_agent：api_key 为空但 base_url/model_name
  齐全时仍可用（桌面形态令牌经密钥包回退），缺失 base_url/model_name 时返回 None；
- session_history._replace_image_data_urls：无路径图片保留 data_url，有路径改写为
  image_ref 并保留 width/height；
- tools.multimodal_config.build_vision_model_config：优先从 config 读取（识别 base_url
  键），不依赖进程级 env 反读、不污染 env。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine.image_refs import set_image_summary_provider


@pytest.fixture(autouse=True)
def _reset_image_summary_provider():
    """ImageSummaryRail 构造时注册进程级 provider，前后复位避免污染其他测试。"""
    set_image_summary_provider(None)
    yield
    set_image_summary_provider(None)


# ---------------------------------------------------------------------------
# Fix1: image_summary_rail._model_credential_from_agent 凭证判定
# ---------------------------------------------------------------------------


def _model(credential):
    return SimpleNamespace(
        model_client_config=credential,
        model_config=SimpleNamespace(model_name=credential.model),
    )


def _deep_agent(*, api_key="k", api_base="http://x", model="m"):
    credential = SimpleNamespace(api_key=api_key, api_base=api_base, model=model)
    return SimpleNamespace(
        deep_config=SimpleNamespace(model=_model(credential)),
        _deep_config=SimpleNamespace(model=_model(credential)),
    )


def test_credential_allows_empty_api_key():
    """api_key 为空但 base_url/model 齐全 → 仍返回凭证（桌面形态由密钥包回退）。"""
    from jiuwenswarm.agents.harness.common.rails.image_summary_rail import ImageSummaryRail

    agent = _deep_agent(api_key="", api_base="np://claw-model/v1", model="m1")
    credential = ImageSummaryRail._model_credential_from_agent(agent)
    assert credential is not None
    assert credential.api_key == ""
    assert credential.base_url == "np://claw-model/v1"
    assert credential.model == "m1"


def test_credential_requires_base_url_and_model():
    """base_url 或 model_name 缺失 → 返回 None。"""
    from jiuwenswarm.agents.harness.common.rails.image_summary_rail import ImageSummaryRail

    # base_url 缺失
    no_base = _deep_agent(api_base="")
    assert ImageSummaryRail._model_credential_from_agent(no_base) is None
    # model 缺失
    no_model = _deep_agent(model="")
    assert ImageSummaryRail._model_credential_from_agent(no_model) is None


# ---------------------------------------------------------------------------
# Fix2: session_history 无路径图片保留 data_url
# ---------------------------------------------------------------------------


def test_replace_image_data_urls_keeps_pathless_image_payload():
    from jiuwenswarm.server.runtime.session.session_history import (
        _replace_image_data_urls,
    )

    item = {
        "type": "image",
        "data_url": "data:image/png;base64,AAAA",
        "mime_type": "image/png",
        "width": 100,
        "height": 50,
    }
    out = _replace_image_data_urls(item)
    assert out["type"] == "image_ref"
    assert out["data_url"] == "data:image/png;base64,AAAA"  # 无路径 → 保留原始数据
    assert out["mime_type"] == "image/png"
    assert out["width"] == 100
    assert out["height"] == 50


def test_replace_image_data_urls_extracts_path_and_keeps_meta():
    from jiuwenswarm.server.runtime.session.session_history import (
        _replace_image_data_urls,
    )

    item = {
        "type": "image",
        "data_url": "data:image/png;base64,AAAA",
        "source_path": "/a/b.png",
        "width": 100,
        "height": 50,
    }
    out = _replace_image_data_urls(item)
    assert out["type"] == "image_ref"
    assert out["path"] == "/a/b.png"
    assert "data_url" not in out  # 有路径 → 不再携带 base64
    assert out["width"] == 100
    assert out["height"] == 50
    assert out["mime_type"] == "image/png"


# ---------------------------------------------------------------------------
# Fix4: build_vision_model_config 从 config 优先读取
# ---------------------------------------------------------------------------


def _clean_vision_env(monkeypatch):
    for key in (
        "VISION_API_KEY",
        "VISION_BASE_URL",
        "VISION_API_BASE",
        "VISION_MODEL",
        "VISION_MODEL_NAME",
        "API_BASE",
        "API_KEY",
        "MODEL_NAME",
    ):
        monkeypatch.delenv(key, raising=False)


def test_build_vision_config_resolves_base_url_keyword(monkeypatch):
    """config 用 base_url 键（apply_vision_* 不识别）仍能正确解析，且不污染 env。"""
    from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
        build_vision_model_config,
    )

    _clean_vision_env(monkeypatch)
    cfg = {
        "models": {
            "vision": {
                "model_config": {
                    "api_key": "vkey",
                    "base_url": "https://vision.example/v1",
                    "model_name": "vision-m",
                }
            }
        }
    }
    config = build_vision_model_config(cfg)
    assert config is not None
    assert config.base_url == "https://vision.example/v1"
    assert config.model == "vision-m"
    assert config.api_key == "vkey"
    # 不应写入视觉 env，避免跨请求串用
    assert os.environ.get("VISION_API_BASE") is None


def test_build_vision_config_env_only_as_fallback(monkeypatch):
    """config 缺 base_url 时回退独立视觉 env，而非主会话 API_BASE。"""
    from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
        build_vision_model_config,
    )

    _clean_vision_env(monkeypatch)
    monkeypatch.setenv("VISION_API_KEY", "vkey")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision-env/v1")
    monkeypatch.setenv("VISION_MODEL", "vision-env-m")
    # config 提供了 api_key，但无 base_url/model_name → 应走 vision env 兜底
    cfg = {"models": {"vision": {"model_config": {"api_key": "vkey"}}}}
    config = build_vision_model_config(cfg)
    assert config is not None
    assert config.base_url == "https://vision-env/v1"
    assert config.model == "vision-env-m"
