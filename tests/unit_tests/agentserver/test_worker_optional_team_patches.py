# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Optional SDK Team adaptations must not interrupt later Worker patches."""

from unittest.mock import Mock

import pytest

from jiuwenswarm.server.worker import runtime_backend


@pytest.mark.parametrize("failed_stage", ["guard", "descriptions"])
@pytest.mark.parametrize("error_type", [AttributeError, ImportError, OSError])
def test_optional_team_patch_failure_preserves_remaining_runtime_patches(
    monkeypatch, caplog, failed_stage, error_type,
):
    from jiuwenswarm import llm_sse_patch
    from jiuwenswarm.agents.harness.common.tools import bash_tool_safety
    from jiuwenswarm.common import config, llm_provider_compat_patch, native_tokenizer_cache
    from jiuwenswarm.common import team_workspace_read_guard
    from jiuwenswarm.common.auth import login_credentials

    monkeypatch.setattr(runtime_backend.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(runtime_backend.logger, "propagate", False)
    # Existing log filters can format and clear LogRecord.exc_info. Observe
    # the warning call before those handlers process the exception.
    warning = Mock(wraps=runtime_backend.logger.warning)
    monkeypatch.setattr(runtime_backend.logger, "warning", warning)
    calls = []

    def apply(stage):
        calls.append(stage)
        if stage == failed_stage:
            raise error_type("SDK Team interface unavailable")

    monkeypatch.setattr(native_tokenizer_cache, "install_native_tokenizer_cache", lambda: None)
    monkeypatch.setattr(bash_tool_safety, "install_shell_tool_safety_hooks", lambda: None)
    monkeypatch.setattr(llm_provider_compat_patch, "apply_provider_compat_patches", lambda: None)
    monkeypatch.setattr(team_workspace_read_guard, "install_team_workspace_read_guard", lambda: apply("guard"))
    monkeypatch.setattr(team_workspace_read_guard, "preload_bundled_team_descriptions",
                        lambda: apply("descriptions"))
    monkeypatch.setattr(login_credentials, "apply_login_credential_patch", lambda: apply("login"))
    monkeypatch.setattr(llm_sse_patch, "apply_openai_sse_invoke_patch", lambda: apply("sse"))
    monkeypatch.setattr(config, "get_config", lambda: {"channels": {"xiaoyi": {"mode": "xiaoyi_claw"}}})

    runtime_backend._apply_runtime_entry_patches()

    assert calls == ["guard", "descriptions", "login", "sse"]
    warnings = [record for record in caplog.records if "TeamWorkspace" in record.message]
    assert len(warnings) == 1
    warning.assert_called_once()
    assert warning.call_args.kwargs["exc_info"] is True
