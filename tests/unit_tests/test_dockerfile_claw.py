# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for the standalone JiuwenSwarm container image."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile.claw"


def test_dockerfile_pins_uid_gid_contract() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "groupadd --gid 1000 app" in content
    assert "useradd --uid 1000 --gid 1000" in content
    assert "USER app" in content


def test_dockerfile_strips_and_reinstalls_runtime_foundation() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "sed -i '/openjiuwen_runtime_foundation/d' pyproject.toml" in content
    assert 'sed -i \'0,/"openjiuwen @ git+/{/"openjiuwen @ git+/d;}\' pyproject.toml' in content
    assert "git+${AGENT_RUNTIME_REPOS}@${AGENT_RUNTIME_BRANCH}#subdirectory=foundation" in content
    assert "git+${AGENT_CORE_REPOS}@${AGENT_CORE_BRANCH}" in content
    assert "uv pip install --system" in content


def test_dockerfile_starts_via_role_entrypoint() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CMD ["/app/jiuwenswarm/docker/start.sh"]' in content


def test_dockerfile_does_not_install_optional_all_extra() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert ".[all]" not in content


def test_noninteractive_initialization_does_not_read_stdin(monkeypatch) -> None:
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "_is_interactive", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("stdin must not be read")),
    )

    assert utils.prompt_preferred_language() == "zh"
