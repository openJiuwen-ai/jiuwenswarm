"""No cluster access: execute deployment guards against an isolated tool copy."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("apply_patch", ["true", "false"])
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_k8s_guard_precedes_dependency_or_cluster_operations(tmp_path, apply_patch, mode):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required by the deployment tool")
    major = subprocess.check_output([bash, "-c", "echo ${BASH_VERSINFO[0]}"], text=True).strip()
    if int(major) < 4:
        pytest.skip("the upstream deployment tool requires Bash 4+ associative arrays")
    source = Path(__file__).resolve().parents[2] / "deploy/enterprise"
    target = tmp_path / "deploy"
    shutil.copytree(source, target)
    config = f"APPLY_PATCH={apply_patch}\nJIUWENSWARM_LINK_MTLS_MODE={mode}\n"
    (target / ".env.custom").write_text(config)
    # Help must never provision, query a DB or require a deployment image.
    result = subprocess.run([bash, "deploy.sh", "--help"], cwd=target, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert (target / ".env.custom").read_text() == config
    assert not (target / "conf").exists()
