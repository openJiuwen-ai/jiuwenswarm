# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gate behaviour for the secret-shield rail."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.secret_shield import (
    SecretShieldConfig,
    SecretShieldRail,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield import rail as rail_module
from jiuwenswarm.agents.harness.common.rails.secret_shield.tokenize import token_for
from jiuwenswarm.agents.harness.common.rails.secret_shield.vault import SecretVault

SECRET = "s3cr3t-Value-01"


def _session(session_id: str = "session-1") -> SimpleNamespace:
    return SimpleNamespace(get_session_id=lambda: session_id)


def _tool_ctx(
    result: object = None,
    *,
    tool_name: str = "bash",
    tool_args: object = None,
    session_id: str = "session-1",
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        inputs=ToolCallInputs(
            tool_name=tool_name,
            tool_args=tool_args if tool_args is not None else {},
            tool_result=result,
        ),
        session=_session(session_id),
    )


def _model_ctx(content: str, *, session_id: str = "session-1") -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(response=SimpleNamespace(content=content)),
        session=_session(session_id),
    )


# --------------------------------------------------------------------------- #
# G1: tool-result gate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_observe_mode_keeps_result_and_only_records() -> None:
    rail = SecretShieldRail(SecretShieldConfig())
    ctx = _tool_ctx(f"DB_PASSWORD={SECRET}")

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_result == f"DB_PASSWORD={SECRET}"
    assert len(rail._vault_for("session-1")) == 1


@pytest.mark.asyncio
async def test_enforce_mode_tokenizes_and_appends_hint() -> None:
    rail = SecretShieldRail(SecretShieldConfig(tool_result_mode="enforce"))
    ctx = _tool_ctx(f"DB_PASSWORD={SECRET}")

    await rail.after_tool_call(ctx)

    entry = rail._vault_for("session-1").entries()[0]
    result = ctx.inputs.tool_result
    assert SECRET not in result
    assert token_for(entry.fp) in result
    assert "sensitive value was withheld" in result


@pytest.mark.asyncio
async def test_enforce_tokenizes_every_candidate_in_one_large_result() -> None:
    rail = SecretShieldRail(SecretShieldConfig(tool_result_mode="enforce"))
    secrets = [f"secret-{index:06d}" for index in range(80)]
    ctx = _tool_ctx("\n".join(f"token={value}" for value in secrets))

    await rail.after_tool_call(ctx)

    result = ctx.inputs.tool_result
    assert all(value not in result for value in secrets)
    assert len(rail._vault_for("session-1")) == 80


@pytest.mark.asyncio
async def test_small_configured_capacity_still_tokenizes_one_full_result() -> None:
    rail = SecretShieldRail(
        SecretShieldConfig(tool_result_mode="enforce", vault_max_entries=2)
    )
    secrets = [f"secret-{index:06d}" for index in range(10)]
    ctx = _tool_ctx("\n".join(f"token={value}" for value in secrets))

    await rail.after_tool_call(ctx)

    result = ctx.inputs.tool_result
    assert all(value not in result for value in secrets)
    assert len(rail._vault_for("session-1")) == 10


@pytest.mark.asyncio
async def test_empty_result_is_ignored() -> None:
    rail = SecretShieldRail(SecretShieldConfig(tool_result_mode="enforce"))
    ctx = _tool_ctx(None)

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_result is None
    assert len(rail._vault_for("session-1")) == 0


@pytest.mark.asyncio
async def test_session_vaults_are_isolated() -> None:
    rail = SecretShieldRail(SecretShieldConfig())
    ctx_one = _tool_ctx(f"token={SECRET}", session_id="session-1")
    ctx_two = _tool_ctx(f"token={SECRET}", session_id="session-2")

    await rail.after_tool_call(ctx_one)
    await rail.after_tool_call(ctx_two)

    first = rail._vault_for("session-1")
    second = rail._vault_for("session-2")
    assert first.fingerprint(SECRET) != second.fingerprint(SECRET)


# --------------------------------------------------------------------------- #
# G2: execution gate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execution_gate_audits_token_reference_without_rewriting_in_observe() -> None:
    rail = SecretShieldRail(SecretShieldConfig(exec_gate_mode="observe"))
    await rail.after_tool_call(_tool_ctx(f"token={SECRET}"))
    entry = rail._vault_for("session-1").entries()[0]
    command = f"curl -H 'X-Token: {token_for(entry.fp)}'"
    ctx = _tool_ctx(tool_args={"command": command})

    await rail.before_tool_call(ctx)

    assert ctx.inputs.tool_args["command"] == command


@pytest.mark.asyncio
async def test_execution_gate_substitutes_in_enforce_mode() -> None:
    rail = SecretShieldRail(SecretShieldConfig(exec_gate_mode="enforce"))
    await rail.after_tool_call(_tool_ctx(f"token={SECRET}"))
    entry = rail._vault_for("session-1").entries()[0]
    ctx = _tool_ctx(tool_args={"command": f"echo {token_for(entry.fp)}"})

    await rail.before_tool_call(ctx)

    assert ctx.inputs.tool_args["command"] == f"echo {SECRET}"


@pytest.mark.asyncio
async def test_unresolvable_token_rejects_the_call_in_enforce_mode() -> None:
    rail = SecretShieldRail(SecretShieldConfig(exec_gate_mode="enforce"))
    unknown_token = token_for("ffffffffffffffff")
    ctx = _tool_ctx(tool_args={"command": f"echo {unknown_token}"})

    await rail.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "SECRET_SHIELD=TOKEN_UNRESOLVED" in ctx.inputs.tool_result


@pytest.mark.asyncio
async def test_unresolvable_token_is_not_rejected_in_observe_mode() -> None:
    rail = SecretShieldRail(SecretShieldConfig())
    unknown_token = token_for("ffffffffffffffff")
    ctx = _tool_ctx(tool_args={"command": f"echo {unknown_token}"})

    await rail.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is None
    assert ctx.inputs.tool_args["command"] == f"echo {unknown_token}"


@pytest.mark.asyncio
async def test_evicted_token_rejects_the_call_in_enforce_mode() -> None:
    rail = SecretShieldRail(SecretShieldConfig(exec_gate_mode="enforce"))
    vault = SecretVault(max_entries=1, session_key=b"k" * 32)
    rail._vaults["session-1"] = vault
    evicted_entry = vault.remember("first-Secret-01")
    vault.remember("second-Secret-02")
    assert evicted_entry is not None
    assert vault.was_evicted(evicted_entry.fp) is True
    ctx = _tool_ctx(tool_args={"command": f"echo {token_for(evicted_entry.fp)}"})

    await rail.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "SECRET_SHIELD=TOKEN_UNRESOLVED" in ctx.inputs.tool_result


@pytest.mark.asyncio
async def test_execution_gate_ignores_arguments_without_tokens() -> None:
    rail = SecretShieldRail(SecretShieldConfig(exec_gate_mode="enforce"))
    ctx = _tool_ctx(tool_args={"command": "echo hello"})

    await rail.before_tool_call(ctx)

    assert ctx.inputs.tool_args == {"command": "echo hello"}


# --------------------------------------------------------------------------- #
# G3: model-output gate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_output_observe_mode_keeps_response() -> None:
    rail = SecretShieldRail(SecretShieldConfig())
    await rail.after_tool_call(_tool_ctx(f"token={SECRET}"))
    ctx = _model_ctx(f"your key is {SECRET}")

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.content == f"your key is {SECRET}"


@pytest.mark.asyncio
async def test_output_enforce_masks_plaintext_and_tokens() -> None:
    rail = SecretShieldRail(SecretShieldConfig(output_mode="enforce"))
    await rail.after_tool_call(_tool_ctx(f"token={SECRET}"))
    entry = rail._vault_for("session-1").entries()[0]
    ctx = _model_ctx(f"key={SECRET} ref={token_for(entry.fp)}")

    await rail.after_model_call(ctx)

    content = ctx.inputs.response.content
    assert SECRET not in content
    assert token_for(entry.fp) not in content
    assert content == f"key=[REDACTED:{entry.fp}] ref=[REDACTED:{entry.fp}]"


@pytest.mark.asyncio
async def test_output_enforce_preserves_non_text_blocks_in_list_content() -> None:
    rail = SecretShieldRail(SecretShieldConfig(output_mode="enforce"))
    await rail.after_tool_call(_tool_ctx(f"token={SECRET}"))
    entry = rail._vault_for("session-1").entries()[0]
    tool_block = {
        "type": "tool_use",
        "id": "call_1",
        "name": "bash",
        "input": {"command": "echo hi"},
    }
    image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    content = [
        tool_block,
        {"type": "text", "text": f"key={SECRET}"},
        f"raw {SECRET}",
        image_block,
    ]
    response = SimpleNamespace(content=list(content))
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(response=response),
        session=_session(),
    )

    await rail.after_model_call(ctx)

    rewritten = response.content
    mask = f"[REDACTED:{entry.fp}]"
    assert isinstance(rewritten, list)
    assert len(rewritten) == 4
    assert rewritten[0] == tool_block
    assert rewritten[1] == {"type": "text", "text": f"key={mask}"}
    assert rewritten[2] == f"raw {mask}"
    assert rewritten[3] == image_block
    assert SECRET not in str(rewritten)


@pytest.mark.asyncio
async def test_output_enforce_leaves_content_untouched_when_no_match() -> None:
    rail = SecretShieldRail(SecretShieldConfig(output_mode="enforce"))
    content = [{"type": "text", "text": "hello"}, "plain"]
    response = SimpleNamespace(content=content)
    ctx = AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(response=response),
        session=_session(),
    )

    await rail.after_model_call(ctx)

    assert response.content is content


@pytest.mark.asyncio
async def test_output_gate_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    rail = SecretShieldRail(SecretShieldConfig(output_mode="enforce"))
    ctx = _model_ctx("hello")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(rail_module, "mask_text", _boom)

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.content == "hello"


@pytest.mark.asyncio
async def test_disabled_rail_is_inert() -> None:
    rail = SecretShieldRail(SecretShieldConfig(enabled=False, tool_result_mode="enforce"))
    ctx = _tool_ctx(f"token={SECRET}")

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_result == f"token={SECRET}"
    assert len(rail._vault_for("session-1")) == 0
