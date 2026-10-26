# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""installed_skill 决策 / HMAC / ID 解析单测."""

from __future__ import annotations

import hashlib
import hmac

from jiuwenswarm.agents.harness.common.installed_skill import (
    DECISION_ALREADY_INSTALLED,
    DECISION_BLOCKED,
    DECISION_INSTALL,
    DECISION_PREBUILT,
    DECISION_UPGRADE,
    SOURCE_PREBUILT,
    SOURCE_USER,
    decide_user_reinstall,
    format_user_skill_source,
    resolve_final_tenant_ids,
    row_public_view,
    skill_versions_equal,
    verify_skill_download_hmac,
)
from jiuwenswarm.agents.harness.common.installed_skill_ops import _reject_for_decision


def test_skill_versions_equal_strip_only() -> None:
    assert skill_versions_equal("1.0", "1.0")
    assert skill_versions_equal(" 1.0 ", "1.0")
    assert not skill_versions_equal("1.0", "1.0.0")
    assert not skill_versions_equal("v1.0", "1.0")
    assert skill_versions_equal("", None)
    assert skill_versions_equal(None, "  ")


def test_decide_user_reinstall_matrix() -> None:
    assert decide_user_reinstall(None, new_version="1.0") == DECISION_INSTALL
    assert (
        decide_user_reinstall(
            {"source_type": SOURCE_PREBUILT, "skill_version": "1.0"},
            new_version="2.0",
        )
        == DECISION_PREBUILT
    )
    assert (
        decide_user_reinstall(
            {"source_type": SOURCE_USER, "skill_version": "1.0"},
            new_version="1.0",
        )
        == DECISION_ALREADY_INSTALLED
    )
    assert (
        decide_user_reinstall(
            {"source_type": SOURCE_USER, "skill_version": "1.0"},
            new_version="2.0",
        )
        == DECISION_UPGRADE
    )
    assert (
        decide_user_reinstall(
            {"source_type": "other", "skill_version": "1.0"},
            new_version="1.0",
        )
        == DECISION_BLOCKED
    )


def test_reject_for_decision() -> None:
    assert _reject_for_decision(DECISION_INSTALL, "foo") is None
    assert _reject_for_decision(DECISION_UPGRADE, "foo") is None

    prebuilt = _reject_for_decision(DECISION_PREBUILT, "foo", channel="web")
    assert prebuilt is not None
    assert prebuilt["error_code"] == "conflict_prebuilt"
    assert prebuilt["source"] == "web"

    already = _reject_for_decision(DECISION_ALREADY_INSTALLED, "bar")
    assert already is not None
    assert already["error_code"] == "already_installed"
    assert already.get("already_installed") is True

    blocked = _reject_for_decision(DECISION_BLOCKED, "baz")
    assert blocked is not None
    assert blocked["error_code"] == "blocked"


def test_verify_skill_download_hmac() -> None:
    secret = "test-secret"
    content = b"skill-package-bytes"
    sig = hmac.new(secret.encode("utf-8"), content, hashlib.sha256).hexdigest()
    assert verify_skill_download_hmac(content, sig, secret=secret)
    assert not verify_skill_download_hmac(content, "deadbeef", secret=secret)
    assert not verify_skill_download_hmac(b"", sig, secret=secret)
    assert not verify_skill_download_hmac(content, sig, secret="")


def test_resolve_final_tenant_ids_hex_passthrough() -> None:
    sid = "a" * 32
    aid = "b" * 32
    assert resolve_final_tenant_ids(service_id=sid, agent_id=aid) == (sid, aid)


def test_resolve_final_tenant_ids_md5_logical() -> None:
    svc, ag = resolve_final_tenant_ids(service_id="my-svc", agent_id="my-agent")
    assert svc == hashlib.md5(b"my-svc").hexdigest()
    assert ag == hashlib.md5(b"my-agent").hexdigest()


def test_format_user_skill_source_and_row_public_view() -> None:
    from datetime import datetime, timezone

    assert format_user_skill_source("web", "https://x/y.zip") == "web:https://x/y.zip"
    assert format_user_skill_source("clawhub", "slug") == "clawhub:slug"
    assert format_user_skill_source("skillnet", "https://a") == "skillnet:https://a"
    view = row_public_view(
        {
            "skill_name": "demo",
            "source_type": SOURCE_USER,
            "skill_source": "web:https://x",
            "skill_version": "1",
        }
    )
    assert view["removable"] is True
    assert row_public_view({"skill_name": "p", "source_type": SOURCE_PREBUILT})["removable"] is False
    ts = datetime(2026, 8, 4, 12, 8, 24, tzinfo=timezone.utc)
    dt_view = row_public_view(
        {
            "skill_name": "demo",
            "source_type": SOURCE_USER,
            "installed_at": ts,
            "updated_at": ts.replace(tzinfo=None),
        }
    )
    assert dt_view["installed_at"] == "2026-08-04T12:08:24+00:00"
    assert dt_view["updated_at"].endswith("+00:00")
    import json

    json.dumps(dt_view)
