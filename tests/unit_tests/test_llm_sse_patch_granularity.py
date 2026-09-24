# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LLM 客户端补丁粒度拆分与渠道门控的回归测试。

背景（code review P0 / P1）：

* P0 —— ``apply_openai_sse_invoke_patch`` 原先把 4 个互相独立的补丁捆绑成一个整体，
  并被 ``channels.xiaoyi.mode == "xiaoyi_claw"`` 整体门控。OfficeClaw + Huawei MaaS
  生产链路只依赖其中与渠道无关的 3 个（GLM XML 清洗 / Authorization 保留 /
  MaaS x-span-id 注入），门控为 False 时 Authorization 补丁随之失效，网关报
  APIG.0303。本文件第一条用例即为该回归的防护点。
* P1 —— 门控读取的是会被 Web UI 保存流程清理掉的顶层 ``channels.xiaoyi.mode``，
  而 ``mode`` 的规范位置是 ``channels.xiaoyi.apps[].mode``。
"""

from __future__ import annotations

from typing import Any

import pytest

import jiuwenswarm.llm_sse_patch as patch_mod
from jiuwenswarm.server import app_agentserver

from openjiuwen.core.foundation.llm import headers_helper
from openjiuwen.core.foundation.llm.model_clients import openai_model_client as oai_mod

_MODULE_PATCH_FLAGS = (
    "_PATCH_APPLIED",
    "_AUTH_HEADER_PATCH_APPLIED",
    "_RESPONSE_ASSEMBLY_PATCH_APPLIED",
    "_GLM_XML_SANITIZE_PATCH_APPLIED",
    "_MAAS_SPAN_ID_PATCH_APPLIED",
)

_CLIENT_PATCH_FLAGS = (
    "_response_assembly_patch_applied",
    "_glm_xml_sanitize_patch_applied",
    "_maas_span_id_patch_applied",
    "_auth_default_headers_patch_applied",
)


@pytest.fixture()
def fresh_patch_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空所有补丁幂等标志，模拟「服务启动早期尚未打补丁」的进程状态。"""
    for name in _MODULE_PATCH_FLAGS:
        monkeypatch.setattr(patch_mod, name, False, raising=False)
    for name in _CLIENT_PATCH_FLAGS:
        monkeypatch.setattr(oai_mod.OpenAIModelClient, name, False, raising=False)
    monkeypatch.setattr(headers_helper, "_auth_header_patch_applied", False, raising=False)


# ── P0：补丁粒度与幂等 ────────────────────────────────────────────────


def test_channel_independent_patches_apply_without_sse_assembly(
    fresh_patch_state: None,
) -> None:
    """P0 回归：即使不启用 SSE 响应组装补丁，其余 3 个补丁仍必须全部生效。

    这正是 OfficeClaw 部署下的场景——配置中没有 xiaoyi_claw 渠道，
    但 Authorization 保留 / GLM 清洗 / MaaS x-span-id 一个都不能少。
    """
    patch_mod.apply_openai_auth_header_patch()
    patch_mod.apply_glm_tool_xml_sanitize_patch()
    patch_mod.apply_huawei_maas_span_id_patch()

    # ③ Authorization 保留必须生效
    assert headers_helper._auth_header_patch_applied is True  # pylint: disable=protected-access
    headers = headers_helper.build_base_headers(
        custom_headers={"Authorization": "Basic dGVzdA=="}
    )
    assert headers.get("Authorization") == "Basic dGVzdA=="

    # ② GLM XML 清洗 与 ④ MaaS x-span-id 注入必须生效
    assert oai_mod.OpenAIModelClient._glm_xml_sanitize_patch_applied is True  # pylint: disable=protected-access
    assert oai_mod.OpenAIModelClient._maas_span_id_patch_applied is True  # pylint: disable=protected-access

    # ① SSE 响应组装补丁必须保持未启用（它才需要按渠道门控）
    assert (
        getattr(oai_mod.OpenAIModelClient, "_response_assembly_patch_applied", False) is False
    )
    assert patch_mod._RESPONSE_ASSEMBLY_PATCH_APPLIED is False  # pylint: disable=protected-access


def test_granular_patches_are_individually_idempotent(fresh_patch_state: None) -> None:
    """P0 回归：每个补丁拥有独立幂等标志，重复调用不得二次包装。"""
    patch_mod.apply_openai_auth_header_patch()
    patch_mod.apply_glm_tool_xml_sanitize_patch()
    wrapped_stream_chunk = oai_mod.OpenAIModelClient._parse_stream_chunk  # pylint: disable=protected-access
    patch_mod.apply_glm_tool_xml_sanitize_patch()
    assert oai_mod.OpenAIModelClient._parse_stream_chunk is wrapped_stream_chunk  # pylint: disable=protected-access

    patch_mod.apply_huawei_maas_span_id_patch()
    wrapped_invoke = oai_mod.OpenAIModelClient.invoke
    patch_mod.apply_huawei_maas_span_id_patch()
    assert oai_mod.OpenAIModelClient.invoke is wrapped_invoke

    patch_mod.apply_openai_response_assembly_patch()
    wrapped_parse_response = oai_mod.OpenAIModelClient._parse_response  # pylint: disable=protected-access
    patch_mod.apply_openai_response_assembly_patch()
    assert oai_mod.OpenAIModelClient._parse_response is wrapped_parse_response  # pylint: disable=protected-access


def test_aggregate_entry_still_applies_all_patches(fresh_patch_state: None) -> None:
    """兼容入口 ``apply_openai_sse_invoke_patch`` 仍应一次性应用全部 4 个补丁。"""
    patch_mod.apply_openai_sse_invoke_patch()

    assert headers_helper._auth_header_patch_applied is True  # pylint: disable=protected-access
    assert oai_mod.OpenAIModelClient._glm_xml_sanitize_patch_applied is True  # pylint: disable=protected-access
    assert oai_mod.OpenAIModelClient._maas_span_id_patch_applied is True  # pylint: disable=protected-access
    assert oai_mod.OpenAIModelClient._response_assembly_patch_applied is True  # pylint: disable=protected-access


# ── P1：渠道门控读取路径 ──────────────────────────────────────────────


def _stub_config(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)


def test_gate_matches_mode_inside_xiaoyi_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """规范路径 ``channels.xiaoyi.apps[].mode`` 必须能命中 xiaoyi_claw。"""
    _stub_config(
        monkeypatch,
        {
            "channels": {
                "xiaoyi": {
                    "apps": [{"mode": "chat"}, {"mode": "xiaoyi_claw"}],
                    "send_file_allowed": True,
                }
            }
        },
    )
    assert app_agentserver._should_apply_sse_invoke_patch() is True  # pylint: disable=protected-access


def test_gate_returns_false_for_officeclaw_only_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OfficeClaw 部署（无 xiaoyi_claw）下门控应为 False，且不影响其他补丁。"""
    _stub_config(
        monkeypatch,
        {
            "channels": {
                "officeclaw": {"send_file_allowed": True},
                "xiaoyi": {"apps": [{"mode": "chat"}], "send_file_allowed": True},
            }
        },
    )
    assert app_agentserver._should_apply_sse_invoke_patch() is False  # pylint: disable=protected-access


def test_gate_supports_legacy_flat_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧平铺格式 ``channels.xiaoyi.mode`` 仍应被兼容。"""
    _stub_config(monkeypatch, {"channels": {"xiaoyi": {"mode": "xiaoyi_claw"}}})
    assert app_agentserver._should_apply_sse_invoke_patch() is True  # pylint: disable=protected-access


def test_gate_defaults_to_true_when_config_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动早期读配置失败时保守兜底为 True（保持原有行为）。"""

    def _raise() -> dict[str, Any]:
        raise RuntimeError("config not ready")

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", _raise)
    assert app_agentserver._should_apply_sse_invoke_patch() is True  # pylint: disable=protected-access
