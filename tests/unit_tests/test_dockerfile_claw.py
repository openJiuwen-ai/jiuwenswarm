# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for the standalone JiuwenSwarm container image."""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile.claw"
WEB_ENTRYPOINT = PROJECT_ROOT / "jiuwenswarm" / "channels" / "web" / "app_web.py"


def test_dockerfile_installs_frontend_and_vcs_build_dependencies() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    install_block = re.search(
        r"RUN apt-get update && apt-get install.*?rm -rf /var/lib/apt/lists/\*",
        content,
        flags=re.DOTALL,
    )

    assert install_block is not None
    for package in ("ca-certificates", "git", "nodejs", "npm"):
        assert re.search(rf"\b{re.escape(package)}\b", install_block.group())


def test_container_exposes_web_ui_without_changing_other_launch_modes() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    web_entrypoint = WEB_ENTRYPOINT.read_text(encoding="utf-8")

    assert "ENV FRONTEND_HOST=0.0.0.0" in dockerfile
    assert "sed -i" not in dockerfile
    assert 'os.getenv("FRONTEND_HOST", "localhost")' in web_entrypoint


def test_dockerfile_installs_declared_default_dependencies() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple . --no-cache-dir" in content
    assert '.[all]' not in content


def test_noninteractive_initialization_does_not_read_stdin(monkeypatch) -> None:
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "_is_interactive", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("stdin must not be read")),
    )

    assert utils.prompt_preferred_language() == "zh"
