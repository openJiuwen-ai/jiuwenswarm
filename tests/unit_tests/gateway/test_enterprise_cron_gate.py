# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise cron gate / sticky identity unit tests."""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.cron_job_mutations import apply_cron_job_patch, build_new_cron_job
from jiuwenswarm.gateway.cron.enterprise_gate import (
    enterprise_cron_enabled,
    extract_routing_triple,
    job_matches_routing,
    routing_triple_complete,
    strip_sticky_identity_fields,
)
from jiuwenswarm.gateway.cron.models import CronJob


def test_build_new_cron_job_persists_routing_triple() -> None:
    job = build_new_cron_job(
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="desc",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="u1",
    )
    assert job.group_id == "g1"
    assert job.bot_id == "b1"
    assert job.user_id == "u1"
    d = job.to_dict()
    assert d["group_id"] == "g1"
    restored = CronJob.from_dict(d)
    assert restored.user_id == "u1"


def test_apply_cron_job_patch_identity_sticky() -> None:
    job = build_new_cron_job(
        name="n",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="u1",
    )
    updated = apply_cron_job_patch(
        job,
        {"name": "n2", "group_id": "hack", "bot_id": "hack", "user_id": "hack"},
    )
    assert updated.name == "n2"
    assert updated.group_id == "g1"
    assert updated.bot_id == "b1"
    assert updated.user_id == "u1"


def test_strip_sticky_identity_fields() -> None:
    out = strip_sticky_identity_fields(
        {"name": "x", "group_id": "g", "bot_id": "b", "user_id": "u", "job_id": "j"}
    )
    assert out == {"name": "x"}


def test_extract_routing_triple_prefers_metadata_routing() -> None:
    class _Req:
        params = {"group_id": "from_params", "bot_id": "bp"}
        metadata = {
            "user_id": "ur",
            "routing": {
                "group_id": "from_routing",
                "bot_id": "br",
            },
            "query": {"bot_id": ["bq"]},
        }

    g, b, u = extract_routing_triple(_Req())
    assert g == "from_routing"
    assert b == "br"
    assert u == "ur"


def test_extract_routing_triple_priority() -> None:
    # 无 routing 的 dict 视为本地 handler params 副本
    g, b, u = extract_routing_triple(
        {"group_id": "from_params", "bot_id": "bp"},
        {"group_id": "from_meta", "user_id": "um"},
    )
    assert g == "from_params"
    assert b == "bp"
    assert u == "um"


def test_extract_routing_triple_ignores_top_level_routing_fields_when_routing_key_present() -> None:
    g, b, u = extract_routing_triple(
        {
            "routing": {},
            "group_id": "top-g",
            "bot_id": "top-b",
            "user_id": "top-u",
        },
    )
    # user_id 顶层权威；group/bot 有 routing 键时不读顶层
    assert g is None
    assert b is None
    assert u == "top-u"


def test_extract_routing_triple_ignores_query_when_routing_absent() -> None:
    g, b, u = extract_routing_triple(
        {"query": {"bot_id": ["bq"], "user_id": ["uq"], "group_id": ["gq"]}},
    )
    assert g is None
    assert b is None
    assert u is None


def test_job_matches_routing_and() -> None:
    job = CronJob(
        id="j1",
        name="n",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="u1",
    )
    assert job_matches_routing(job, group_id="g1", bot_id="b1", user_id="u1")
    assert not job_matches_routing(job, group_id="g1", bot_id="b1", user_id="other")
    assert job_matches_routing(
        CronJob(
            id="j2",
            name="n",
            enabled=True,
            cron_expr="0 9 * * *",
            timezone="Asia/Shanghai",
            targets="web",
        ),
        group_id="g1",
        bot_id="b1",
        user_id="u1",
        include_unbound=True,
    )


def test_enterprise_cron_enabled_by_deployment_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    assert enterprise_cron_enabled(deployment_mode="standalone") is True
    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    assert enterprise_cron_enabled(deployment_mode="distributed") is False
    monkeypatch.setenv("GATEWAY_DB_HOST", "mysql-headless.default")
    assert enterprise_cron_enabled(deployment_mode="distributed") is True


def test_enterprise_cron_disabled_for_personal_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    assert enterprise_cron_enabled(deployment_mode="standalone") is False


def test_routing_triple_complete() -> None:
    assert routing_triple_complete("g", "b", "u")
    assert not routing_triple_complete("g", "b", None)
    assert not routing_triple_complete("", "b", "u")
