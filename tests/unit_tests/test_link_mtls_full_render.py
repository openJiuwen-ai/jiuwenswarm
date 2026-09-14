# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Execute ordinary shell render; simulate only Linux/CLI boundaries, no cluster.

Optional LINK_TEST_DEPLOY_ZIP supplies release IMAGE values only, never customer
passwords. Docker execution is deliberately replaced by the real Python worker.
This is not an image-runtime or Kubernetes integration test.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[2] / "deploy/enterprise"


@pytest.mark.parametrize("mode", [None, "off", "observe", "enforce"])
@pytest.mark.parametrize("patch", ["true", "false"])
def test_ordinary_source_render_without_db_or_cluster_writes(tmp_path, mode, patch):
    bash = shutil.which("bash")
    if not bash or int(subprocess.check_output([bash, "-c", "echo ${BASH_VERSINFO[0]}"], text=True)) < 4:
        pytest.skip("Bash 4+ required")
    if not shutil.which("yq") or not shutil.which("jq"):
        pytest.skip("mikefarah/yq and jq required")
    tool, binary, claw = (tmp_path / name for name in ("tool", "bin", "claw"))
    shutil.copytree(SOURCE, tool, ignore=shutil.ignore_patterns("conf", ".env.custom*", ".link-mtls-test"))
    # Use the ordinary source generator, not a stale release-generated template.
    subprocess.run(
        [bash, str(SOURCE / "update_conf.sh"), str(tool / "templates/gateway-config.template.yaml")],
        check=True,
        capture_output=True,
        timeout=15,
    )
    binary.mkdir()
    for name in ("runtime_management_extension", "manager_config_receiver"):
        p = claw / "packages/jiuwenclaw-ee/gateway/extensions" / name
        p.mkdir(parents=True)
        (p / "extension.yaml").write_text("dependencies: {}\n")
    images = "\n".join(
        f'{name}_IMAGE="example/{name.lower()}:21s"'
        for name in (
            "GATEWAY",
            "AGENT_RUNTIME",
            "AGENT_SERVER",
            "WEB",
            "MANAGER_SERVER",
            "MANAGER_WEB",
            "IDENTITY",
            "JIUWENBOX",
        )
    )
    if os.getenv("LINK_TEST_DEPLOY_ZIP"):
        with zipfile.ZipFile(os.environ["LINK_TEST_DEPLOY_ZIP"]) as archive:
            path = next(p for p in archive.namelist() if p.endswith("/.env.custom"))
            images = "\n".join(
                line
                for line in archive.read(path).decode().splitlines()
                if "_IMAGE=" in line and not line.startswith("#")
            )
    runtime = os.getenv("LINK_TEST_RUNTIME_SOURCE")
    if not runtime:
        pytest.skip("matching Runtime checkout needed for source-render test")
    mode_config = "" if mode is None else f"JIUWENSWARM_LINK_MTLS_MODE={mode}\n"
    config = (
        images + f"\nMODE=dev\nCLAW_CODE_PATH={claw}\nRUNTIME_CODE_PATH={runtime}\n"
        f"APPLY_PATCH={patch}\n"
        + mode_config
        + "DB_TYPE=mysql\nDB_HOST=unreachable.invalid\nDB_PORT=3306\nDB_USER=test\nDB_PASSWORD=test\n"
        "JIUWENSWARM_EDITION=enterprise\nCURRENT_NODE_NAME=test-node\n"
        "IS_UP_MANAGER_WEB=true\nIS_MOUNT_WEB_CODE=true\nIS_MOUNT_MANAGER_WEB_CODE=true\n"
        "LOGIN_AUTH_SIMULATE=false\nNFS_SERVER_ADDR=unreachable.invalid\n"
    )
    (tool / ".env.custom").write_text(config)
    stub = f"""#!{sys.executable}
import asyncio, base64, json, os, sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['CLI_TRACE'], 'a') as trace:
    trace.write(json.dumps([name, *args])+'\\n')
if name == 'uname': print('Linux'); sys.exit()
if name == 'base64':
    print(base64.b64encode(sys.stdin.buffer.read()).decode(),end=''); sys.exit()
if name == 'kubectl':
    assert args[:2] == ['create','configmap'] and '--dry-run=client' in args, 'cluster access forbidden'
    data={{}}
    for arg in args:
        if arg.startswith('--from-env-file='):
            data.update(line.split('=',1) for line in Path(arg.split('=',1)[1]).read_text().splitlines() if '=' in line)
        if arg.startswith('--from-file='):
            k,p=arg.split('=',1)[1].split('=',1); data[k]=Path(p).read_text()
    result = {{'apiVersion':'v1','kind':'ConfigMap','metadata':{{'name':'render-only'}},'data':data}}
    print(json.dumps(result)); sys.exit()
assert name == 'docker'
if args[0] == 'context': print('unix:///test-only.sock'); sys.exit()
assert args[args.index('--user')+1] == str(os.geteuid())+':'+str(os.getegid())
msg=json.load(sys.stdin)
assert msg['action'] == 'imports', 'DB/PKI action forbidden in render-only'
assert args[args.index('--network')+1] == 'none'
assert args[-2:] == ['-m', 'openjiuwen_runtime.foundation.security.link_material_deploy']
root=next(a.split(',')[1][4:] for a in args if a.startswith('type=bind,src=') and 'dst=/app/link-foundation' in a)
sys.path.insert(0,root)
from openjiuwen_runtime.foundation.security.link_material_deploy import dispatch
print(json.dumps(asyncio.run(dispatch(msg))))
"""
    for name in ("docker", "kubectl", "base64", "uname"):
        path = binary / name
        path.write_text(stub)
        path.chmod(0o700)
    args = [bash, "deploy.sh", "up", "gateway", "web", "runtime"]
    if patch == "false":
        args.append("manager")
    trace = tmp_path / "calls.jsonl"
    process = subprocess.run(
        [bash, "-c", 'umask 077\nexec "$@"', "private-render", *args, "-n", "link-test", "--render-only"],
        cwd=tool,
        timeout=90,
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "CLI_TRACE": str(trace)},
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stdout[-5000:] + process.stderr[-2000:]
    files = list((tool / "conf").iterdir())
    assert files and all("PRIVATE KEY" not in p.read_text() for p in files if p.is_file())
    gateway = list(yaml.safe_load_all((tool / "conf/gateway.yaml").read_text()))
    runtime_yaml = list(yaml.safe_load_all((tool / "conf/runtime.yaml").read_text()))
    for docs in (gateway, runtime_yaml):
        dep = next(d for d in docs if d and d.get("kind") == "Deployment")
        volumes = dep["spec"]["template"]["spec"]["volumes"]
        assert any(v["name"].startswith("jiuwenswarm-link-mtls") for v in volumes) == (mode == "enforce")
    assert (tool / "conf/manager-server.yaml").exists() == (patch == "false")
    assert (tool / "conf/identity.yaml").exists() == (patch == "false")
    if patch == "false":
        manager_docs = list(yaml.safe_load_all((tool / "conf/manager-server.yaml").read_text()))
        manager = next(d for d in manager_docs if d and d.get("kind") == "Deployment")
        spec = manager["spec"]["template"]["spec"]
        container = next(c for c in spec["containers"] if c["name"] == "manager")
        link_env = {v["name"]: v.get("value") for v in container.get("env", [])}
        if mode in ("observe", "enforce"):
            assert link_env["JIUWENSWARM_LINK_MTLS_MODE"] == mode
        assert any(
            v.get("secret", {}).get("secretName") == "jiuwenswarm-link-manager" for v in spec.get("volumes", [])
        ) == (mode == "enforce")
    assert (tool / ".env.custom").read_text() == config
    if mode in (None, "off"):
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        assert not any(call[0] == "docker" for call in calls)
