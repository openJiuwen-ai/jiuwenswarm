# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""计费 trace 核心段构造（billing_trace）+ xiaoyi 渠道计费客户端（billing_client）单测。

2026-09-02 正式方案：task/status/update NEW/FINISH/FAILED 经 np://claw-billing
管道上报（桌面 BillingProxy 拼接鉴权）；x-hag-trace-id 全链路裸核心段
（临时标记方案的 begin/end/failed 前缀与 NO_REPLY 虚拟调用已删除）。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.invocation_context import billing_trace
from jiuwenswarm.common.invocation_context.billing_trace import (
    MAX_CORE_LEN,
    SHORT_INTERACTION_ID_MAX_LEN,
    build_billing_core,
)
from jiuwenswarm.common import billing_client


class TestBuildBillingCore:
    """core = sessionId&interactionId 短码（上限 45，超长先截 session 段保 interaction 短码）。"""

    def test_short_interaction_id_kept(self) -> None:
        assert build_billing_core("sess-1", "cron-run-1") == "sess-1&cron-run-1"

    def test_long_interaction_id_truncated_to_8(self) -> None:
        assert (
            build_billing_core("sess-1", "31675eb3-6199-4022-a58f-9ed63bf8f489")
            == "sess-1&31675eb3"
        )

    def test_desktop_typical_form_within_limit(self) -> None:
        core = build_billing_core(
            "desktop_1a03cf9f826_9d2ce4dcd3d1",
            "31675eb3-6199-4022-a58f-9ed63bf8f489",
        )
        assert core == "desktop_1a03cf9f826_9d2ce4dcd3d1&31675eb3"
        assert len(core) <= MAX_CORE_LEN

    def test_overlong_session_truncated_keeping_interaction(self) -> None:
        core = build_billing_core("s" * 100, "31675eb3-6199-4022-a58f-9ed63bf8f489")
        assert core == f"{'s' * 36}&31675eb3"
        assert len(core) == MAX_CORE_LEN

    def test_interaction_short_id_threshold(self) -> None:
        # ≤ SHORT_INTERACTION_ID_MAX_LEN 原样；超出取前 8
        assert build_billing_core("s", "x" * SHORT_INTERACTION_ID_MAX_LEN) == f"s&{'x' * 12}"
        assert build_billing_core("s", "x" * 13) == f"s&{'x' * 8}"


# ---------------------------------------------------------------- billing_client

_SECRETS = {
    "pipes.billing": "\\\\.\\pipe\\claw-billing",
    "billingToken": "blt_test",
    "uid": "uid-1",
    "deviceId": "dev-1",
}


def _fake_get_secret(path: str, default=None):
    return _SECRETS.get(path, default)


@pytest.fixture(autouse=True)
def _billing_env(monkeypatch):
    """默认启用计费 + fake 密钥包；_post_once 替换为记录伪件（不打真实管道）。"""
    monkeypatch.setattr(billing_client, "get_secret", _fake_get_secret)
    monkeypatch.setenv("JIUWEN_XIAOYI_BILLING", "on")
    billing_client.reset_xiaoyi_billing_registry()
    yield
    billing_client.reset_xiaoyi_billing_registry()


class _RecordingPost:
    """_post_once 伪件：按序记录调用；fail_times 控制前 N 次失败。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict] = []
        self._fail_times = fail_times

    async def __call__(self, np_base: str, token: str, trace_id: str, payload: dict) -> bool:
        self.calls.append(
            {"np_base": np_base, "token": token, "trace_id": trace_id, "payload": payload}
        )
        if self._fail_times > 0:
            self._fail_times -= 1
            return False
        return True


async def _drain_report_tasks() -> None:
    tasks = list(billing_client._REPORT_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
class TestXiaoyiBillingClient:
    async def test_new_body_and_headers(self, monkeypatch) -> None:
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        assert billing_client.report_new("帮我写周报", "sess-1&abcd1234") is True
        await _drain_report_tasks()

        assert len(post.calls) == 1
        call = post.calls[0]
        assert call["np_base"] == "np://claw-billing"
        assert call["token"] == "blt_test"
        assert call["trace_id"] == "sess-1&abcd1234"
        body = call["payload"]
        assert body["conversationStatus"] == "NEW"
        assert body["userId"] == "uid-1"
        assert body["query"] == "帮我写周报"
        assert "sessionId" not in body
        assert "interactionId" not in body
        device = body["endpoint"]["device"]
        assert device["deviceId"] == "dev-1"
        assert device["prdVer"] == "11.6.5.414"
        assert device["osType"] == "Hap"

    async def test_terminal_body_finish_and_failed(self, monkeypatch) -> None:
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        billing_client.report_new("q", "sess-1&abcd1234")
        assert (
            billing_client.report_terminal(
                "sess-1&abcd1234", session_id="sess-1", interaction_id="abcd1234", ok=True
            )
            is True
        )
        await _drain_report_tasks()

        finish = post.calls[-1]["payload"]
        assert finish["conversationStatus"] == "FINISH"
        assert finish["sessionId"] == "sess-1"
        assert finish["interactionId"] == "abcd1234"
        assert "query" not in finish

        # 另一 core：FAILED 形态
        billing_client.report_new("q2", "sess-2&efgh5678")
        billing_client.report_terminal(
            "sess-2&efgh5678", session_id="sess-2", interaction_id="efgh5678", ok=False
        )
        await _drain_report_tasks()
        assert post.calls[-1]["payload"]["conversationStatus"] == "FAILED"

    async def test_new_query_strips_workspace_directive(self, monkeypatch) -> None:
        """渠道层 with_workspace_directive 拼入的 <claw_workspace> 尾段不进计费 query
        （受限档/完全访问档两种文案同剥离）。"""
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        restricted = (
            "整理桌面文件"
            '\n\n<claw_workspace>{"path": "D:/hcj_data/proj"}</claw_workspace>\n'
            "【工作空间】当前项目目录是 `D:/hcj_data/proj`。除非用户明确指定其他位置，"
            "否则所有新建与写入文件必须落在该目录下（可用相对路径）。"
        )
        full_access = (
            "整理桌面文件"
            '\n\n<claw_workspace>{"path": "D:/hcj_data/proj"}</claw_workspace>\n'
            "【工作空间】当前项目目录是 `D:/hcj_data/proj`。除非用户明确指定其他位置，"
            "新建与写入文件请落在该目录下（可用相对路径）。"
        )
        assert billing_client.report_new(restricted, "sess-1&abcd1234") is True
        assert billing_client.report_new(full_access, "sess-2&efgh5678") is True
        await _drain_report_tasks()

        assert post.calls[0]["payload"]["query"] == "整理桌面文件"
        assert post.calls[1]["payload"]["query"] == "整理桌面文件"

    async def test_new_query_without_injection_untouched(self, monkeypatch) -> None:
        """无注入段的 query 原样透传（不误伤用户正文）。"""
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        assert billing_client.report_new("帮我写周报", "sess-1&abcd1234") is True
        await _drain_report_tasks()
        assert post.calls[0]["payload"]["query"] == "帮我写周报"

    async def test_new_deduplicated_per_core(self, monkeypatch) -> None:
        """HITL 续跑同 core：NEW 只发一次。"""
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        assert billing_client.report_new("q", "sess-1&abcd1234") is True
        assert billing_client.report_new("q-续跑", "sess-1&abcd1234") is False
        await _drain_report_tasks()
        assert len(post.calls) == 1

    async def test_terminal_requires_reported_new(self, monkeypatch) -> None:
        """未发 NEW 的轮次（非 xiaoyi 渠道/早退/被禁用）不发 orphan 终态。"""
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)

        assert (
            billing_client.report_terminal(
                "sess-x&never000", session_id="sess-x", interaction_id="never000", ok=True
            )
            is False
        )
        await _drain_report_tasks()
        assert len(post.calls) == 0

    async def test_retry_once_then_succeed(self, monkeypatch) -> None:
        post = _RecordingPost(fail_times=1)
        monkeypatch.setattr(billing_client, "_post_once", post)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)

        billing_client.report_new("q", "sess-1&abcd1234")
        await _drain_report_tasks()
        assert len(post.calls) == 2

    async def test_gives_up_after_retry(self, monkeypatch) -> None:
        post = _RecordingPost(fail_times=99)
        monkeypatch.setattr(billing_client, "_post_once", post)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)

        billing_client.report_new("q", "sess-1&abcd1234")
        await _drain_report_tasks()
        assert len(post.calls) == 2

    async def test_disabled_without_secrets(self, monkeypatch) -> None:
        """密钥包缺 pipes.billing/billingToken/uid（旧桌面/非桌面形态）→ 静默禁用。"""
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)
        monkeypatch.setattr(billing_client, "get_secret", lambda _path, default=None: default)

        assert billing_client.report_new("q", "sess-1&abcd1234") is False
        assert (
            billing_client.report_terminal(
                "sess-1&abcd1234", session_id="sess-1", interaction_id="abcd1234", ok=True
            )
            is False
        )
        await _drain_report_tasks()
        assert len(post.calls) == 0

    async def test_env_kill_switch(self, monkeypatch) -> None:
        post = _RecordingPost()
        monkeypatch.setattr(billing_client, "_post_once", post)
        monkeypatch.setenv("JIUWEN_XIAOYI_BILLING", "off")

        assert billing_client.report_new("q", "sess-1&abcd1234") is False
        await _drain_report_tasks()
        assert len(post.calls) == 0


async def _no_sleep(_delay: float) -> None:
    return None
