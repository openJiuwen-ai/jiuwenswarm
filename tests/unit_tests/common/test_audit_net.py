# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.audit_net import (
    clear_local_ip_cache,
    enrich_extra_with_hop_ips,
    get_local_ip,
    resolve_peer_ip,
)


def setup_function() -> None:
    clear_local_ip_cache()


def teardown_function() -> None:
    clear_local_ip_cache()


def test_get_local_ip_prefers_pod_ip(monkeypatch):
    monkeypatch.setenv("POD_IP", "10.1.2.3")
    assert get_local_ip() == "10.1.2.3"
    monkeypatch.setenv("POD_IP", "10.9.9.9")
    assert get_local_ip() == "10.1.2.3"  # cached


def test_resolve_peer_ip_literal_and_url():
    assert resolve_peer_ip("10.0.0.5") == "10.0.0.5"
    assert resolve_peer_ip("http://10.0.0.5:8080/v1") == "10.0.0.5"
    assert resolve_peer_ip("") == "-"
    assert resolve_peer_ip(None) == "-"


def test_enrich_fills_srcip_when_dstip_passed(monkeypatch):
    monkeypatch.setenv("POD_IP", "10.1.1.1")
    clear_local_ip_cache()
    extra: dict = {"DSTIP": "10.2.2.2"}
    enrich_extra_with_hop_ips(extra)
    assert extra["SRCIP"] == "10.1.1.1"
    assert extra["DSTIP"] == "10.2.2.2"


def test_enrich_skips_without_dst(monkeypatch):
    monkeypatch.setenv("POD_IP", "10.1.1.1")
    clear_local_ip_cache()
    extra: dict = {}
    enrich_extra_with_hop_ips(extra)
    assert "SRCIP" not in extra
    assert "DSTIP" not in extra


def test_enrich_does_not_override_explicit_src(monkeypatch):
    monkeypatch.setenv("POD_IP", "10.1.1.1")
    clear_local_ip_cache()
    extra = {"SRCIP": "9.9.9.9", "DSTIP": "8.8.8.8"}
    enrich_extra_with_hop_ips(extra)
    assert extra["SRCIP"] == "9.9.9.9"
    assert extra["DSTIP"] == "8.8.8.8"
