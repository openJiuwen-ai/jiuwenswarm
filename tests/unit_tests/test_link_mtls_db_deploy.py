# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the actual shell/template boundary without Docker or cluster writes."""

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from openjiuwen_runtime.foundation.security import link_material_sync
from openjiuwen_runtime.foundation.security.link_material_deploy import deployment_plan

DIRECTORY = Path(__file__).resolve().parents[2] / "deploy/enterprise"


def run_handler(settings, command, overrides=""):
    bash = shutil.which("bash")
    if (
        not bash
        or int(
            subprocess.check_output([bash, "-c", "echo ${BASH_VERSINFO[0]}"], text=True)
        )
        < 4
    ):
        pytest.skip("Bash 4+ required")
    if not shutil.which("jq") or not shutil.which("yq"):
        pytest.skip("jq and mikefarah/yq required")
    script = (
        "declare -A DEPLOY_VARS\n"
        "while IFS= read -r -d '' key && IFS= read -r -d '' value; do DEPLOY_VARS[$key]=$value; done\n"
        "SCRIPT_DIR=" + shlex.quote(str(DIRECTORY)) + "\n"
        'source "$SCRIPT_DIR/link_mtls_handler.sh"\n' + overrides + "\n" + command
    )
    data = "".join(str(k) + "\0" + str(v) + "\0" for k, v in settings.items())
    return subprocess.run(
        [bash, "-c", script], input=data, text=True, capture_output=True, timeout=30
    )


def overlay(document, role, settings):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "workload.yaml"
        path.write_text(
            yaml.safe_dump_all(document if isinstance(document, list) else [document])
        )
        result = run_handler(
            settings,
            "link_mtls_render " + shlex.quote(role) + " " + shlex.quote(str(path)),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        result = list(yaml.safe_load_all(path.read_text()))
        return result if isinstance(document, list) else result[0]


def overlay_agent_template(document, settings):
    return overlay(document, "agentserver", settings)


@pytest.fixture
def settings():
    return {
        "NAMESPACE": "cert-test",
        "GATEWAY_NAME": "jiuwenclaw-gateway",
        "AGENT_RUNTIME_NAME": "jiuwenclaw-agent-runtime",
        "GATEWAY_CONFIG_HTTP_PORT": "8775",
        "AGENT_RUNTIME_PORT": "8091",
        "AGENT_SERVER_PORT": "8766",
        "AGENT_SERVER_NAME": "jiuwenclaw-agentserver",
        "DB_TYPE": "mysql",
        "DB_HOST": "database",
        "DB_PORT": "3306",
        "GATEWAY_DB_NAME": "gateway_cert_test",
        "GATEWAY_DB_USER": "gateway-user",
        "GATEWAY_DB_PASSWORD": "gateway-password",
        "RUNTIME_DB_NAME": "runtime_cert_test",
        "RUNTIME_DB_USER": "runtime-user",
        "RUNTIME_DB_PASSWORD": "runtime-password",
        "MODE": "dev",
        "RUNTIME_CODE_PATH": "/example/runtime",
        "JIUWENSWARM_LINK_MTLS_MODE": "enforce",
        "LINK_SYNC_CODE": Path(link_material_sync.__file__).read_text(),
    }


def workload(role, *, uid=0, fs_group=0):
    container = {
        "name": {
            "gateway": "gateway",
            "runtime": "agent-runtime",
            "manager": "manager",
        }[role],
        "image": "test:21s",
        "command": ["custom"],
        "args": ["unchanged"],
        "securityContext": {"runAsUser": uid, "runAsGroup": 1000},
        "volumeMounts": [{"name": "business", "mountPath": "/data"}],
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": 8091},
            "timeoutSeconds": 2,
        },
    }
    return {
        "kind": "Deployment",
        "spec": {
            "template": {
                "spec": {
                    "securityContext": {"fsGroup": fs_group},
                    "containers": [container, {"name": "unrelated", "image": "other"}],
                    "volumes": [
                        {"name": "business", "nfs": {"server": "nfs", "path": "/data"}}
                    ],
                }
            }
        },
    }


@pytest.mark.parametrize("role", ["gateway", "runtime", "manager"])
@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_overlay_preserves_business_fields_and_role_isolation(settings, role, mode):
    settings["JIUWENSWARM_LINK_MTLS_MODE"] = mode
    original = [workload(role)]
    before = copy.deepcopy(original)
    rendered = overlay(original, role, settings)
    assert original == before
    spec = rendered[0]["spec"]["template"]["spec"]
    main, unrelated = spec["containers"]
    assert main["command"] == ["custom"] and main["args"] == ["unchanged"]
    assert unrelated == before[0]["spec"]["template"]["spec"]["containers"][1]
    assert spec["volumes"][0] == before[0]["spec"]["template"]["spec"]["volumes"][0]
    if mode == "off":
        assert rendered == before
    elif mode == "observe":
        assert "httpGet" in main["readinessProbe"]
        assert not any("secret" in v for v in spec["volumes"])
    else:
        assert spec["securityContext"] == {"fsGroup": 0}
        assert (
            next(v for v in spec["volumes"] if "secret" in v)["secret"]["defaultMode"]
            == 0o400
        )
        assert "PRIVATE KEY" not in json.dumps(rendered)
        if role != "manager":
            assert "httpGet" not in main["readinessProbe"]
            assert "exec" in main["readinessProbe"]


@pytest.mark.parametrize("uid,group", [(1000, 1000), (None, 1000), (0, 1000)])
def test_private_copy_does_not_chown_business_volumes(settings, uid, group):
    spec = overlay([workload("runtime", uid=uid, fs_group=group)], "runtime", settings)[
        0
    ]["spec"]["template"]["spec"]
    assert spec["securityContext"] == {"fsGroup": group}
    assert len(spec["containers"]) == 3
    main, unrelated, helper = spec["containers"]
    assert helper["securityContext"].get("runAsUser") == uid
    assert len(helper["volumeMounts"]) == 2
    assert not any(m["mountPath"] == "/data" for m in helper["volumeMounts"])
    assert not any("volumeMounts" in c for c in [unrelated])
    assert any(
        v["name"] == "JIUWENSWARM_LINK_MTLS_PROFILE"
        and "/private/identity/" in v["value"]
        for v in main["env"]
    )


def test_agent_template_changes_only_main_source_package(settings):
    template = {
        "type": "config_sync",
        "rawdata": {
            "templates": [
                {
                    "main_container_id": "as",
                    "fsGroup": 0,
                    "volumes": [
                        {"name": "data", "nfs": {"server": "nfs", "path": "/data"}}
                    ],
                }
            ],
            "containers": [
                {
                    "container_id": "as",
                    "name": "agent",
                    "command": ["agent"],
                    "args": ["custom"],
                },
                {"container_id": "box", "name": "jiuwenbox"},
            ],
        },
    }
    result = overlay_agent_template(template, settings)
    assert result["rawdata"]["containers"][1] == template["rawdata"]["containers"][1]
    assert (
        result["rawdata"]["templates"][0]["volumes"][0]
        == template["rawdata"]["templates"][0]["volumes"][0]
    )
    assert result["rawdata"]["containers"][0]["command"] == ["agent"]
    assert result["rawdata"]["containers"][0]["args"] == ["custom"]
    assert "tls.key" not in json.dumps(result)


def test_sans_use_headless_dns_not_pod_ip(settings):
    plan = deployment_plan(settings)
    assert plan["endpoints"]["runtime"] == "jiuwenclaw-agent-runtime:8091"
    assert plan["sans"]["agentserver"] == [
        "*.jiuwenclaw-agentserver.cert-test.svc.cluster.local"
    ]


def test_database_service_endpoint_precedes_host_dns(settings):
    """Do not let stale host DNS select a different headless DB endpoint."""
    overrides = r"""
    getent() { printf '%s\n' '192.0.2.99 STREAM stale'; }
    kubectl() {
        case "$*" in
            *'-n cert-test get service database -o json')
                printf '%s' '{"spec":{"clusterIP":"None"}}'
                ;;
            *'-n cert-test get endpoints database -o json')
                printf '%s' '{"subsets":[{"addresses":[{"ip":"10.24.1.55"}]}]}'
                ;;
            *) return 99 ;;
        esac
    }
    """
    result = run_handler(
        settings,
        "link_mtls_resolve_host database.cert-test.svc.cluster.local",
        overrides,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "10.24.1.55"


def test_external_database_host_falls_back_to_host_dns(settings):
    overrides = r"""
    kubectl() { return 1; }
    getent() { printf '%s\n' '198.51.100.20 STREAM external'; }
    """
    result = run_handler(
        settings,
        "link_mtls_resolve_host mysql.customer.example",
        overrides,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "198.51.100.20"


@pytest.mark.parametrize("uid,gid", [(0, 0), (1234, 2345)])
@pytest.mark.parametrize("action", ["imports", "ensure", "status", "request", "wait"])
def test_image_worker_matches_deployer_uid_without_relaxing_isolation(
    monkeypatch, settings, tmp_path, uid, gid, action
):
    binary, capture = tmp_path / "docker", tmp_path / "capture.json"
    binary.write_text(
        f"#!{sys.executable}\nimport sys,json\n"
        f"open({str(capture)!r},'w').write(json.dumps({{'args':sys.argv[1:],'message':json.load(sys.stdin)}}))\n"
        "print('{\"checked\":true}')\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DOCKER_HOST", "unix:///test.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    settings = {
        **settings,
        "MODE": "product",
        "AGENT_RUNTIME_IMAGE": "runtime:21s",
        "DB_HOST": "database",
        "GATEWAY_DB_PASSWORD": "private-test-password",
        "UNRELATED_PRIVATE_VALUE": "must-not-enter-worker-protocol\nwith-a-second-line",
    }
    overrides = (
        f'id() {{ if [[ "$1" == -u ]]; then echo {uid}; else echo {gid}; fi; }}\n'
        "link_mtls_resolve_host() { echo 127.0.0.1; }\n"
        "link_mtls_preflight() { :; }\n"
        "link_mtls_kube() { :; }"
    )
    result = run_handler(
        settings, f"link_mtls_worker {action} gateway /api/v1/ready", overrides
    )
    assert result.returncode == 0, result.stderr
    captured = json.loads(capture.read_text())
    args = captured["args"]
    assert args[args.index("--user") + 1] == f"{uid}:{gid}"
    assert "--read-only" in args and "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert args[args.index("--network") + 1] == (
        "none" if action == "imports" else "host"
    )
    assert "--privileged" not in args and not any("docker.sock" in a for a in args)
    assert "private-test-password" not in " ".join(args) + result.stdout + result.stderr
    assert captured["message"]["settings"]["GATEWAY_DB_NAME"] == "gateway_cert_test"
    assert captured["message"]["settings"]["RUNTIME_DB_NAME"] == "runtime_cert_test"
    assert (
        captured["message"]["settings"]["GATEWAY_DB_PASSWORD"]
        == "private-test-password"
    )
    assert captured["message"]["settings"]["RUNTIME_DB_PASSWORD"] == (
        settings["RUNTIME_DB_PASSWORD"]
    )
    assert "UNRELATED_PRIVATE_VALUE" not in captured["message"]["settings"]
    assert args[-2:] == [
        "-m",
        "openjiuwen_runtime.foundation.security.link_material_deploy",
    ]
    assert json.loads(result.stdout) == {"checked": True}


@pytest.mark.parametrize(
    "missing",
    [
        "GATEWAY_DB_NAME",
        "GATEWAY_DB_USER",
        "GATEWAY_DB_PASSWORD",
        "RUNTIME_DB_NAME",
        "RUNTIME_DB_USER",
        "RUNTIME_DB_PASSWORD",
    ],
)
def test_worker_rejects_unresolved_database_setting_before_docker(settings, missing):
    settings[missing] = ""
    overrides = """docker() { echo unexpected-docker >&2; return 99; }
    link_mtls_resolve_host() { echo 127.0.0.1; }"""
    result = run_handler(settings, "link_mtls_worker ensure", overrides)
    assert result.returncode != 0
    assert f"{missing} must be resolved before certificate preparation" in result.stderr
    assert "unexpected-docker" not in result.stderr


def test_worker_rejects_serialized_database_setting_loss_before_docker(settings):
    settings["MODE"] = "product"
    overrides = """
    link_mtls_settings_json() {
        printf '%s' '{"GATEWAY_DB_NAME":"gateway","GATEWAY_DB_USER":"user","GATEWAY_DB_PASSWORD":"password","RUNTIME_DB_NAME":"runtime","RUNTIME_DB_USER":"user","RUNTIME_DB_PASSWORD":null}'
    }
    link_mtls_resolve_host() { echo 127.0.0.1; }
    docker() {
        if [[ "$1" == context ]]; then
            echo unix:///var/run/docker.sock
            return
        fi
        echo unexpected-docker >&2
        return 99
    }
    """
    result = run_handler(
        settings,
        "link_mtls_worker request gateway /api/v1/ready",
        overrides,
    )
    assert result.returncode != 0
    assert "Serialized mTLS database settings are incomplete" in result.stderr
    assert "unexpected-docker" not in result.stderr


def test_conflicting_headless_service_stops_before_writes(settings):
    overrides = """link_mtls_kube() {
        [[ "$1" == get && "$2" == service ]] || return 99
        printf '%s' '{"spec":{"clusterIP":"10.0.0.8","selector":{"app":"other"}}}'
    }"""
    result = run_handler(settings, "link_mtls_preflight", overrides)
    assert result.returncode != 0
    assert "conflicts with the certificate binding" in result.stderr


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_secret_create_accepts_only_db_winner(settings, same):
    secrets = [
        {
            "kind": "Secret",
            "metadata": {"name": "jiuwenswarm-link-" + role},
            "data": {"test": "committed"},
        }
        for role in ("gateway", "runtime", "agentserver", "manager")
    ]
    worker = json.dumps({"secrets": secrets, "summary": {"mtls_deployment_id": "test"}})
    current = json.dumps({"data": {"test": "committed" if same else "other"}})
    overrides = (
        "link_mtls_worker() { printf '%s' " + shlex.quote(worker) + "; }\n"
        "link_mtls_kube() { case \"$1\" in create) return 1;; get) printf '%s' "
        + shlex.quote(current)
        + ";; apply) cat >/dev/null;; *) return 99;; esac; }"
    )
    result = run_handler(settings, "link_mtls_call ensure", overrides)
    assert (result.returncode == 0) == same
    if not same:
        assert "Secret conflicts with database material" in result.stderr


def test_partial_worker_result_cannot_mark_deployment_ready(settings):
    overrides = """link_mtls_worker() { echo '{"secrets":[],"summary":{"mtls_deployment_id":"x"}}'; }
    link_mtls_kube() { echo unexpected-write >&2; return 99; }"""
    result = run_handler(settings, "link_mtls_call ensure", overrides)
    assert result.returncode != 0 and "unexpected-write" not in result.stderr


def test_deployment_directory_has_one_native_entry_and_one_customer_document():
    assert not list(DIRECTORY.glob("*.py"))
    docs = DIRECTORY.parents[1] / "docs/zh"
    assert len(list(docs.glob("HTTP-SSE链路mTLS部署*.md"))) == 1


@pytest.mark.parametrize(
    "mode,command,module",
    [
        ("off", "up", "GATEWAY"),
        ("observe", "up", "RUNTIME"),
        ("enforce", "down", "GATEWAY"),
        ("enforce", "up", "WEB"),
    ],
)
def test_unrelated_operations_do_not_require_certificate_image(
    settings, mode, command, module
):
    settings["JIUWENSWARM_LINK_MTLS_MODE"] = mode
    overrides = f"CMD={command}; MODULES=({module})\nlink_mtls_call() {{ echo unexpected-image >&2; return 99; }}"
    result = run_handler(settings, "link_mtls_check", overrides)
    assert result.returncode == 0 and "unexpected-image" not in result.stderr


@pytest.mark.parametrize("edition", ["dev", "product"])
def test_generated_customer_config_explicitly_defaults_off(tmp_path, edition):
    script = tmp_path / "gen_env_custom.sh"
    shutil.copyfile(DIRECTORY / script.name, script)
    result = subprocess.run(
        ["bash", str(script), "amd64", "test-version", edition],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    config = (tmp_path / ".env.custom").read_text()
    assert config.count("JIUWENSWARM_LINK_MTLS_MODE=") == 1
    assert "JIUWENSWARM_LINK_MTLS_MODE=off" in config
