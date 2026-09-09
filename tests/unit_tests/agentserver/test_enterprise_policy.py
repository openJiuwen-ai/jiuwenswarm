"""企业级模型策略解析单元测试。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.enterprise_config.schemas import (
    normalize_template_ref,
)


def test_normalize_template_ref_accepts_list() -> None:
    assert normalize_template_ref(None) == {}
    assert normalize_template_ref(
        {
            "default_model": ["f2222222-2222-4222-8222-222222222202"],
            "vision_model": ["f2222222-2222-4222-8222-222222222202"],
            "skill_prebuilt": [
                "a1000001-0000-4000-8000-000000000001",
                "abc",
                "abc",
                "",
                None,
            ],
        }
    ) == {
        "default_model": ["f2222222-2222-4222-8222-222222222202"],
        "vision_model": ["f2222222-2222-4222-8222-222222222202"],
        "skill_prebuilt": [
            "a1000001-0000-4000-8000-000000000001",
            "abc",
        ],
    }


def test_normalize_template_ref_rejects_string_slot_value() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        normalize_template_ref(
            {"default_model": "f2222222-2222-4222-8222-222222222202"},
        )


@pytest.mark.asyncio
async def test_load_effective_config_by_instance_agent_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.enterprise_config import db_queries
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        DEFAULT_AGENT_LOAD_SLOTS,
        load_effective_enterprise_config,
    )
    from jiuwenswarm.common.schema.agent import AgentRequest

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-demo")

    m2 = "22222222-2222-4222-8222-222222222222"
    m1 = "11111111-1111-4111-8111-111111111111"
    w1, w2 = "w1", "w2"
    e1, e2 = "e1", "e2"
    jid = "sp-demo"
    resource_id = "bot_main"
    ref_template_id = "agent-tmpl-1"

    async def _list_records(
        table: str,
        *,
        filters: dict | None = None,
        order_by: str = "",
    ) -> list[dict]:
        scoped = dict(filters or {})
        if table == "instance_agent_resource":
            if scoped.get("resource_id") != resource_id:
                return []
            return [
                {
                    "jiuwenclaw_id": jid,
                    "resource_id": resource_id,
                    "resource_name": "main",
                    "ref_template_id": ref_template_id,
                    "grants": [{"match_expr": "", "enabled": True}],
                }
            ]
        if table == "agent_template":
            if scoped.get("template_id") != ref_template_id:
                return []
            return [
                {
                    "jiuwenclaw_id": jid,
                    "template_id": ref_template_id,
                    "enabled": True,
                    "template_ref": {
                        "default_model": [m2],
                        "vision_model": [m2],
                        "video_model": [m1],
                        "audio_model": [m1],
                        "skill_prebuilt": [w1, w2],
                        "extension_config": [e1, e2],
                    },
                    "data": None,
                }
            ]
        return []

    async def _fetch_templates_by_slot(
        slot: str, template_ids: list[str]
    ) -> list[dict]:
        return [
            {"template_id": tid, "model_id": tid, "slot": slot}
            for tid in template_ids
        ]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    monkeypatch.setattr(db_queries, "fetch_templates_by_slot", _fetch_templates_by_slot)

    request = AgentRequest(
        request_id="req-test-bob",
        params={},
        metadata={
            "routing": {
                "group_id": "g_demo_sales",
                "bot_id": resource_id,
                "user_id": "bob",
            }
        },
    )
    loaded = await load_effective_enterprise_config(
        request,
        DEFAULT_AGENT_LOAD_SLOTS,
    )
    assert loaded is not None
    assert loaded.resource_id == resource_id
    assert loaded.ref_template_id == ref_template_id
    assert loaded.template_ref["default_model"] == [m2]
    assert loaded.template_ref["vision_model"] == [m2]
    assert loaded.template_ref["video_model"] == [m1]
    assert loaded.template_ref["audio_model"] == [m1]
    assert loaded.template_ref["skill_prebuilt"] == [w1, w2]
    assert loaded.template_ref["extension_config"] == [e1, e2]


@pytest.mark.asyncio
async def test_load_effective_config_missing_template_id_skips_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """错误 / 不存在的 template_id 仍保留在 template_ref，只是查不到实体。"""
    from jiuwenswarm.server.runtime.enterprise_config import db_queries
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        DEFAULT_AGENT_LOAD_SLOTS,
        load_effective_enterprise_config,
    )
    from jiuwenswarm.common.schema.agent import AgentRequest

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-current")
    e4 = "44444444-4444-4444-8444-444444444444"
    missing_model_id = "missing-model-id"
    resource_id = "bot_main"
    ref_template_id = "agent-tmpl-ext"

    async def _list_records(
        table: str,
        *,
        filters: dict | None = None,
        order_by: str = "",
    ) -> list[dict]:
        scoped = dict(filters or {})
        if table == "instance_agent_resource":
            if scoped.get("resource_id") != resource_id:
                return []
            return [
                {
                    "jiuwenclaw_id": "sp-current",
                    "resource_id": resource_id,
                    "ref_template_id": ref_template_id,
                    "grants": [{"match_expr": "", "enabled": True}],
                }
            ]
        if table == "agent_template":
            if scoped.get("template_id") != ref_template_id:
                return []
            return [
                {
                    "jiuwenclaw_id": "sp-current",
                    "template_id": ref_template_id,
                    "enabled": True,
                    "template_ref": {
                        "default_model": [missing_model_id],
                        "extension_config": [e4],
                    },
                }
            ]
        return []

    async def _fetch_templates_by_slot(
        slot: str, template_ids: list[str]
    ) -> list[dict]:
        known = {e4}
        return [
            {
                "template_id": tid,
                "template_name": "Gateway 定时清理",
                "component": "gateway",
                "slot": slot,
            }
            for tid in template_ids
            if tid in known
        ]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    monkeypatch.setattr(db_queries, "fetch_templates_by_slot", _fetch_templates_by_slot)

    request = AgentRequest(
        request_id="req-test-unknown",
        params={},
        metadata={
            "routing": {
                "group_id": "g_unknown",
                "bot_id": resource_id,
                "user_id": "bob",
            }
        },
    )
    loaded = await load_effective_enterprise_config(
        request,
        DEFAULT_AGENT_LOAD_SLOTS,
    )
    assert loaded is not None
    assert loaded.template_ref["default_model"] == [missing_model_id]
    assert not loaded.models.get("default_model")
    assert loaded.template_ref["extension_config"] == [e4]
    assert loaded.extension_config is not None
    assert loaded.extension_config[0]["template_name"] == "Gateway 定时清理"


def test_embedding_slot_is_loaded_separately_from_model_slots() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.schemas import (
        DEFAULT_AGENT_LOAD_SLOTS,
        MODEL_SLOT_KEYS,
        SLOT_ENTITY_TABLE,
        TemplateRefSlot,
    )

    assert TemplateRefSlot.EMBEDDING_MODEL.value == "embedding_model"
    assert SLOT_ENTITY_TABLE[TemplateRefSlot.EMBEDDING_MODEL] == "embedding_template"
    assert TemplateRefSlot.EMBEDDING_MODEL in DEFAULT_AGENT_LOAD_SLOTS
    assert TemplateRefSlot.EMBEDDING_MODEL not in MODEL_SLOT_KEYS


@pytest.mark.asyncio
async def test_a2a_policy_slot_loads_by_policy_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.enterprise_config import db_queries

    captured: dict[str, object] = {}

    async def _list_records(
        table: str,
        *,
        filters: dict | None = None,
        order_by: str = "",
    ) -> list[dict]:
        captured.update(table=table, filters=filters, order_by=order_by)
        return [{"policy_id": "policy-1", "enabled": True}]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    result = await db_queries.fetch_template_by_slot(
        "a2a_access_policy", "policy-1"
    )

    assert result == {"policy_id": "policy-1", "enabled": True}
    assert captured["table"] == "a2a_access_policy_template"
    assert captured["filters"] == {"enabled": True, "policy_id": ["policy-1"]}


@pytest.mark.asyncio
async def test_a2a_policy_load_failure_keeps_other_enterprise_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.enterprise_config import db_queries
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        load_effective_enterprise_config,
    )
    from jiuwenswarm.server.runtime.enterprise_config.schemas import TemplateRefSlot

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _list_records(table, *, filters=None, order_by=""):
        del filters, order_by
        if table == "instance_agent_resource":
            return [{"resource_id": "resource-1", "ref_template_id": "agent-1"}]
        if table == "agent_template":
            return [
                {
                    "template_id": "agent-1",
                    "enabled": True,
                    "template_ref": {
                        "default_model": ["model-1"],
                        "a2a_access_policy": ["policy-1"],
                    },
                }
            ]
        return []

    async def _fetch_templates(slot, template_ids):
        if slot == TemplateRefSlot.A2A_ACCESS_POLICY:
            raise RuntimeError("policy table unavailable")
        return [{"template_id": template_ids[0], "model_id": template_ids[0]}]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    monkeypatch.setattr(db_queries, "fetch_templates_by_slot", _fetch_templates)
    request = AgentRequest(
        request_id="request-1",
        params={},
        metadata={"routing": {"bot_id": "resource-1"}},
    )

    loaded = await load_effective_enterprise_config(
        request,
        {TemplateRefSlot.DEFAULT_MODEL, TemplateRefSlot.A2A_ACCESS_POLICY},
    )

    assert loaded is not None
    assert loaded.models["default_model"][0]["model_id"] == "model-1"
    assert loaded.a2a_access_policy is None


def test_a2a_policy_slot_requires_one_literal_reference() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        _literal_slot_template_id_map,
    )

    assert _literal_slot_template_id_map(
        {"a2a_access_policy": ["policy-1"]}
    ) == {"a2a_access_policy": ["policy-1"]}
    assert _literal_slot_template_id_map(
        {"a2a_access_policy": ["policy-1", "policy-2"]}
    ) == {}
    assert _literal_slot_template_id_map(
        {"a2a_access_policy": ["${user::policy}"]}
    ) == {}


def test_image_gen_slot_is_registered_as_model_slot() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.apply_models import (
        SLOT_TO_CONFIG_KEY,
    )
    from jiuwenswarm.server.runtime.enterprise_config.schemas import (
        DEFAULT_AGENT_LOAD_SLOTS,
        MODEL_SLOT_KEYS,
        SLOT_ENTITY_TABLE,
        TemplateRefSlot,
    )

    assert TemplateRefSlot.IMAGE_GEN_MODEL.value == "image_gen_model"
    assert SLOT_ENTITY_TABLE[TemplateRefSlot.IMAGE_GEN_MODEL] == "model_template"
    assert TemplateRefSlot.IMAGE_GEN_MODEL in MODEL_SLOT_KEYS
    assert TemplateRefSlot.IMAGE_GEN_MODEL in DEFAULT_AGENT_LOAD_SLOTS
    assert SLOT_TO_CONFIG_KEY[TemplateRefSlot.IMAGE_GEN_MODEL] == "image_gen"


def test_enterprise_image_gen_maps_to_models_image_gen_section() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.apply_models import (
        apply_enterprise_models_to_config,
    )
    from jiuwenswarm.server.runtime.enterprise_config.schemas import (
        EffectiveEnterpriseConfig,
        RoutingContext,
        TemplateRefSlot,
    )

    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        models={
            TemplateRefSlot.IMAGE_GEN_MODEL.value: [
                {
                    "template_name": "image-gen",
                    "template_id": "img-1",
                    "api_base": "https://image.example.com/v1",
                    "api_key": "image-key",
                    "model_id": "wanx-v1",
                    "model_provider": "dashscope",
                    "parameters": {"size": "1024*1024"},
                }
            ]
        },
    )

    merged, applied = apply_enterprise_models_to_config({"models": {}}, enterprise)

    assert applied is True
    assert merged["models"]["image_gen"] == {
        "model_client_config": {
            "api_base": "https://image.example.com/v1",
            "api_key": "image-key",
            "model_name": "wanx-v1",
            "client_provider": "dashscope",
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"size": "1024*1024"},
    }


def test_normalize_model_types_accepts_image_gen() -> None:
    """Assert EE model_template allows image_gen without importing gateway DB deps."""
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "jiuwenclaw-ee"
        / "gateway"
        / "extensions"
        / "manager_config_receiver"
        / "core"
        / "template"
        / "model_template.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"_ALLOWED_MODEL_TYPES", "_normalize_model_types"}
    kept: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            kept.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    kept.append(node)
                    break
    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {"__name__": "_model_template_symbols"}
    exec("from typing import Any", namespace)
    exec(compile(module, str(path), "exec"), namespace)

    assert "image_gen" in namespace["_ALLOWED_MODEL_TYPES"]
    normalize = namespace["_normalize_model_types"]
    assert normalize(["image_gen"]) == ["image_gen"]
    assert normalize(["default", "image_gen"]) == ["default", "image_gen"]


def test_enterprise_embedding_maps_to_embed_config_section() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.apply_models import (
        apply_enterprise_models_to_config,
    )
    from jiuwenswarm.server.runtime.enterprise_config.schemas import (
        EffectiveEnterpriseConfig,
        RoutingContext,
    )

    enterprise = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g", bot_id="b", user_id="u"),
        embedding=[
            {
                "api_key": "policy-key",
                "api_base": "https://embedding.example.com/v1",
                "model_id": "text-embedding-3-large",
            }
        ],
    )

    merged, applied = apply_enterprise_models_to_config(
        {
            "embed": {
                "embed_api_key": "local-key",
                "embed_base_url": "https://local.example.com/v1",
                "embed_model": "local-model",
            }
        },
        enterprise,
    )

    assert applied is True
    assert merged["embed"] == {
        "embed_api_key": "policy-key",
        "embed_base_url": "https://embedding.example.com/v1",
        "embed_model": "text-embedding-3-large",
    }


def test_get_embed_config_prefers_db_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.memory import config as memory_config

    monkeypatch.setattr(
        memory_config,
        "_load_config",
        lambda: {
            "embed": {
                "embed_api_key": "local-key",
                "embed_base_url": "https://local.example.com/v1",
                "embed_model": "local-model",
            }
        },
    )
    memory_config.clear_embed_config_db_cache()
    assert memory_config.get_embed_config()["api_key"] == "local-key"

    memory_config.set_embed_config_db_cache(
        {
            "api_key": "policy-key",
            "api_base": "https://policy.example.com/v1",
            "model_id": "policy-model",
        }
    )
    assert memory_config.get_embed_config() == {
        "api_key": "policy-key",
        "base_url": "https://policy.example.com/v1",
        "model": "policy-model",
    }

    memory_config.clear_embed_config_db_cache()
    assert memory_config.get_embed_config()["api_key"] == "local-key"


@pytest.mark.asyncio
async def test_load_effective_config_loads_embedding_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.enterprise_config import db_queries
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        DEFAULT_AGENT_LOAD_SLOTS,
        load_effective_enterprise_config,
    )
    from jiuwenswarm.server.runtime.enterprise_config.schemas import TemplateRefSlot

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    jid = "embedding-demo"
    monkeypatch.setenv("JIUWENCLAW_ID", jid)
    embedding_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    resource_id = "bot_embed"
    ref_template_id = "agent-tmpl-embed"

    async def _list_records(
        table: str,
        *,
        filters: dict | None = None,
        order_by: str = "",
    ) -> list[dict]:
        scoped = dict(filters or {})
        if table == "instance_agent_resource":
            if scoped.get("resource_id") != resource_id:
                return []
            return [
                {
                    "jiuwenclaw_id": jid,
                    "resource_id": resource_id,
                    "ref_template_id": ref_template_id,
                    "grants": [{"match_expr": "", "enabled": True}],
                }
            ]
        if table == "agent_template":
            if scoped.get("template_id") != ref_template_id:
                return []
            return [
                {
                    "jiuwenclaw_id": jid,
                    "template_id": ref_template_id,
                    "enabled": True,
                    "template_ref": {"embedding_model": [embedding_id]},
                }
            ]
        return []

    async def _fetch_templates_by_slot(
        slot: str, template_ids: list[str]
    ) -> list[dict]:
        assert slot == TemplateRefSlot.EMBEDDING_MODEL
        assert template_ids == [embedding_id]
        return [
            {
                "template_id": embedding_id,
                "api_base": "https://embedding.example.com/v1",
                "api_key": "secret",
                "model_id": "text-embedding-3-large",
            }
        ]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    monkeypatch.setattr(db_queries, "fetch_templates_by_slot", _fetch_templates_by_slot)

    loaded = await load_effective_enterprise_config(
        AgentRequest(
            request_id="req-embedding",
            params={},
            metadata={"routing": {"bot_id": resource_id}},
        ),
        DEFAULT_AGENT_LOAD_SLOTS,
    )

    assert loaded is not None
    assert loaded.embedding == [
        {
            "template_id": embedding_id,
            "api_base": "https://embedding.example.com/v1",
            "api_key": "secret",
            "model_id": "text-embedding-3-large",
        }
    ]
