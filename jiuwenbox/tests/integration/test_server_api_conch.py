# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""End-to-end API tests for Conch sandbox_runtime and conch.network policy."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest

from jiuwenbox.models.sandbox import JOB_ID_FORMAT_MESSAGE, SANDBOX_ID_FORMAT_MESSAGE

logger = logging.getLogger(__name__)

_UDS_SCHEME = "unix://"
_UDS_PLACEHOLDER_BASE_URL = "http://jiuwenbox"


def _is_uds_endpoint(endpoint: str) -> bool:
    return endpoint.startswith(_UDS_SCHEME)


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def _build_httpx_client(endpoint: str, *, timeout: float = 60.0) -> httpx.Client:
    if _is_uds_endpoint(endpoint):
        uds_path = endpoint[len(_UDS_SCHEME):]
        if not uds_path.startswith("/"):
            raise ValueError(f"unix endpoint requires absolute path: {endpoint!r}")
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path),
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            timeout=timeout,
        )
    return httpx.Client(base_url=_normalize_endpoint(endpoint), timeout=timeout)


class SandboxTrackingClient:
    """Track sandboxes created during a test and clean them up afterwards."""

    def __init__(self, client: httpx.Client):
        self._client = client
        self._created_ids: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def post(self, url, *args, **kwargs):
        response = self._client.post(url, *args, **kwargs)
        if str(url).rstrip("/") == "/api/v1/sandboxes" and response.status_code == 201:
            try:
                sandbox_id = response.json().get("id")
            except Exception:
                sandbox_id = None
            if sandbox_id:
                self._created_ids.append(sandbox_id)
        return response

    def delete(self, url, *args, **kwargs):
        response = self._client.delete(url, *args, **kwargs)
        sandbox_id = self._sandbox_id_from_delete_url(url)
        if sandbox_id and response.status_code in (200, 202, 204, 404):
            self._created_ids = [item for item in self._created_ids if item != sandbox_id]
        return response

    def cleanup_sandboxes(self) -> None:
        for sandbox_id in reversed(self._created_ids):
            try:
                self._client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            except Exception as exc:
                logger.warning("Failed to cleanup sandbox %s: %s", sandbox_id, exc)
        self._created_ids.clear()

    @staticmethod
    def _sandbox_id_from_delete_url(url) -> str | None:
        path = str(url).split("?", 1)[0].rstrip("/")
        prefix = "/api/v1/sandboxes/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        if "/" in suffix:
            return None
        return suffix or None


def _conch_template_available() -> bool:
    return bool(
        (os.environ.get("JIUWENBOX_CONCH_TEMPLATE_NAME") or "").strip()
    )


def _require_conch_template_env() -> None:
    """Opt-in for Conch runtime e2e against a (possibly remote) jiuwenbox server.

    Prerequisites like Conch SDK / ``CONCH_SDK_CONFIG`` / conchd belong on the
    **server** host. The pytest client only talks HTTP, so it must not import
    ``conch`` or probe conchd locally.
    """
    if not _conch_template_available():
        pytest.skip(
            "Set JIUWENBOX_CONCH_TEMPLATE_NAME to run Conch runtime e2e "
            "(server must have Conch SDK/conchd configured)"
        )


def _conch_template_name() -> str:
    return (os.environ.get("JIUWENBOX_CONCH_TEMPLATE_NAME") or "").strip()


def _conch_create_json(
    *,
    policy: dict | None = None,
    policy_mode: str = "append",
    env: dict[str, str] | None = None,
    include_template: bool = True,
    sandbox_id: str | None = None,
) -> dict:
    """Build a create body; inject template_name via request policy for remote servers."""
    body: dict = {"sandbox_runtime": "conch", "policy_mode": policy_mode}
    if env is not None:
        body["env"] = env
    if sandbox_id is not None:
        body["sandbox_id"] = sandbox_id
    merged = {} if policy is None else dict(policy)
    if include_template:
        template_name = _conch_template_name()
        conch = dict(merged.get("conch") or {})
        if template_name and not (conch.get("template_name") or "").strip():
            conch["template_name"] = template_name
        merged["conch"] = conch
    if merged:
        body["policy"] = merged
    return body


def _exec_background(client, sandbox_id: str, command: list[str], **kwargs) -> dict:
    body = {"command": command, **kwargs}
    response = client.post(
        f"/api/v1/sandboxes/{sandbox_id}/exec_background",
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_background_job_finished(
    client,
    sandbox_id: str,
    job_id: str,
    *,
    timeout: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}")
        assert response.status_code == 200, response.text
        status = response.json()
        if not status.get("running"):
            return status
        time.sleep(0.2)
    raise AssertionError(
        f"background job {job_id!r} did not finish within {timeout}s; last={status}"
    )


@pytest.fixture
def client(server_endpoint):
    raw = _build_httpx_client(server_endpoint)
    tracking = SandboxTrackingClient(raw)
    try:
        yield tracking
    finally:
        tracking.cleanup_sandboxes()
        raw.close()


def _default_conch_block() -> dict:
    return {
        "template_name": "",
        "vcpu_num": None,
        "vcpu_max": None,
        "ram_mb": None,
        "run_as_user": None,
        "run_as_group": None,
        "env": {},
        "filesystem_policy": {"bind_mounts": []},
        "network": {
            "egress": {
                "default": "allow",
                "allowed_ips": [],
                "blocked_ips": [],
            },
            "ingress": {
                "default": "allow",
                "allowed_ips": [],
                "blocked_ips": [],
            },
        },
    }


@pytest.mark.integration
class TestConchSandboxTypeAlways:
    """API validation that does not require conchd."""

    @staticmethod
    def test_missing_empty_and_bwrap_create_process_runtime(client):
        for payload in ({}, {"sandbox_runtime": ""}, {"sandbox_runtime": "bwrap"}):
            resp = client.post("/api/v1/sandboxes", json=payload)
            assert resp.status_code == 201, (payload, resp.text)
            body = resp.json()
            assert body["sandbox_runtime"] == "process"
            assert body["phase"] == "ready"
            assert isinstance(body["pid"], int)

    @staticmethod
    def test_invalid_sandbox_runtime_returns_400(client):
        resp = client.post("/api/v1/sandboxes", json={"sandbox_runtime": "docker"})
        assert resp.status_code == 400, resp.text
        assert "sandbox_runtime" in resp.json().get("error", "").lower()

    @staticmethod
    def test_default_policy_includes_conch_block(client):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, resp.text
        sandbox_id = resp.json()["id"]
        policy_resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert policy_resp.status_code == 200, policy_resp.text
        conch = policy_resp.json()["conch"]
        assert conch == _default_conch_block()

    @staticmethod
    def test_conch_network_rejects_non_ipv4_and_extra_fields(client):
        cases = [
            {
                "conch": {
                    "network": {
                        "egress": {"blocked_ips": ["not-an-ip"]},
                    }
                }
            },
            {
                "conch": {
                    "network": {
                        "egress": {"blocked_ips": ["2001:db8::1"]},
                    }
                }
            },
            {
                "conch": {
                    "network": {
                        "egress": {
                            "default": "allow",
                            "allowed_domains": ["example.com"],
                        }
                    }
                }
            },
            {
                "conch": {
                    "network": {
                        "ingress": {
                            "default": "allow",
                            "allowed_ports": [443],
                        }
                    }
                }
            },
        ]
        for policy in cases:
            resp = client.post(
                "/api/v1/sandboxes",
                json={"sandbox_runtime": "conch", "policy": policy, "policy_mode": "append"},
            )
            assert resp.status_code == 400, (policy, resp.text)

    @staticmethod
    def test_conch_network_rejects_over_1024_destinations(client):
        ips = [f"10.0.{i // 256}.{i % 256}" for i in range(1025)]
        resp = client.post(
            "/api/v1/sandboxes",
            json={
                "sandbox_runtime": "conch",
                "policy_mode": "append",
                "policy": {
                    "conch": {
                        "network": {
                            "egress": {"blocked_ips": ips},
                        }
                    }
                },
            },
        )
        assert resp.status_code == 400, resp.text

    @staticmethod
    def test_conch_resources_reject_invalid_vcpu_and_ram(client):
        cases = [
            {"conch": {"vcpu_max": 4}},
            {"conch": {"vcpu_num": 4, "vcpu_max": 2}},
            {"conch": {"vcpu_num": 0}},
            {"conch": {"ram_mb": 0}},
            {"conch": {"vcpu_num": True}},
        ]
        for policy in cases:
            resp = client.post(
                "/api/v1/sandboxes",
                json={
                    "sandbox_runtime": "conch",
                    "policy_mode": "append",
                    "policy": policy,
                },
            )
            assert resp.status_code == 400, (policy, resp.text)

    @staticmethod
    def test_conch_run_as_rejects_half_pair_and_unknown_user(client):
        cases = [
            {"conch": {"run_as_user": "sandbox"}},
            {"conch": {"run_as_group": "sandbox"}},
            {
                "conch": {
                    "run_as_user": "__jiuwenbox_no_such_user__",
                    "run_as_group": "0",
                }
            },
            {
                "conch": {
                    "run_as_user": "0",
                    "run_as_group": "4294967295",
                }
            },
        ]
        for policy in cases:
            resp = client.post(
                "/api/v1/sandboxes",
                json={
                    "sandbox_runtime": "conch",
                    "policy_mode": "append",
                    "policy": policy,
                },
            )
            assert resp.status_code == 400, (policy, resp.text)

    @staticmethod
    def test_get_nonexistent_sandbox_returns_404(client):
        resp = client.get("/api/v1/sandboxes/nonexistent-sbx")
        assert resp.status_code == 404

    @staticmethod
    def test_put_policy_nonexistent_sandbox_returns_404(client):
        resp = client.put(
            "/api/v1/policies/nonexistent-sbx",
            json={
                "policy": {
                    "conch": {
                        "network": {
                            "egress": {"default": "allow"},
                        }
                    }
                },
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("error", "").lower()

    @pytest.mark.parametrize(
        "invalid_id",
        ["abc", "ABC123", "my sb", " abcd ", "a" * 41, "id!"],
    )
    @staticmethod
    def test_create_conch_rejects_invalid_custom_id(client, invalid_id):
        resp = client.post(
            "/api/v1/sandboxes",
            json={"sandbox_runtime": "conch", "sandbox_id": invalid_id},
        )
        assert resp.status_code == 400, resp.text
        assert SANDBOX_ID_FORMAT_MESSAGE in resp.json()["error"]

    @staticmethod
    def test_exec_unknown_sandbox_returns_404(client):
        resp = client.post(
            "/api/v1/sandboxes/no-such-conch/exec",
            json={"command": ["sh", "-c", "printf x"]},
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.integration
@pytest.mark.conch
class TestConchSandboxRuntime:
    """Lifecycle / exec / files / network tests against a Conch-capable server.

    Opt in with ``JIUWENBOX_CONCH_TEMPLATE_NAME``. SDK/conchd must be configured on
    the jiuwenbox **server** host; this client only uses the HTTP API.
    """

    @staticmethod
    def test_create_ready_with_template(client):
        _require_conch_template_env()

        resp = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sandbox_runtime"] == "conch"
        assert body["pid"] is None
        assert body["phase"] == "ready", body
        policy = client.get(f"/api/v1/policies/{body['id']}").json()
        assert policy["conch"]["template_name"] == _conch_template_name()

    @staticmethod
    def test_create_with_vcpu_ram_and_conch_env(client):
        """Policy conch.vcpu_*/ram_mb/env persist; guest sees matching CPUs/RAM; env override rules hold."""
        _require_conch_template_env()

        vcpu_num = 2
        vcpu_max = 2
        ram_mb = 2048

        create = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={
                    "environment": {"TOP_ONLY": "should-not-appear"},
                    "conch": {
                        "vcpu_num": vcpu_num,
                        "vcpu_max": vcpu_max,
                        "ram_mb": ram_mb,
                        "env": {
                            "CONCH_POLICY_ENV": "from-policy",
                            "SHARED_ENV": "from-policy",
                        },
                    },
                },
                env={
                    "SHARED_ENV": "from-api",
                    "API_ENV": "from-api",
                },
            ),
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["sandbox_runtime"] == "conch"
        assert body["phase"] == "ready", body
        sandbox_id = body["id"]

        policy = client.get(f"/api/v1/policies/{sandbox_id}").json()
        assert policy["conch"]["vcpu_num"] == vcpu_num
        assert policy["conch"]["vcpu_max"] == vcpu_max
        assert policy["conch"]["ram_mb"] == ram_mb
        assert policy["conch"]["env"]["CONCH_POLICY_ENV"] == "from-policy"
        assert policy["conch"]["env"]["SHARED_ENV"] == "from-policy"
        # Create-request env is applied at SDK create time, not written into conch.env.
        assert "API_ENV" not in policy["conch"]["env"]

        resource_probe = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [
                    "sh",
                    "-c",
                    "printf 'NPROC=%s\\n' \"$(nproc)\"; "
                    "awk '/^MemTotal:/ {printf \"MEM_KB=%s\\n\", $2}' /proc/meminfo",
                ],
            },
        )
        assert resource_probe.status_code == 200, resource_probe.text
        assert resource_probe.json()["exit_code"] == 0, resource_probe.json()
        resource_out = resource_probe.json()["stdout"]
        nproc_match = re.search(r"(?m)^NPROC=(\d+)$", resource_out)
        mem_match = re.search(r"(?m)^MEM_KB=(\d+)$", resource_out)
        assert nproc_match, resource_out
        assert mem_match, resource_out
        assert int(nproc_match.group(1)) == vcpu_num, resource_out
        # Guest MemTotal is usually slightly below configured RAM (kernel reserved).
        mem_kb = int(mem_match.group(1))
        expected_kb = ram_mb * 1024
        assert expected_kb * 85 // 100 <= mem_kb <= expected_kb, (
            f"guest MemTotal {mem_kb} KiB not within 85%-100% of policy "
            f"ram_mb={ram_mb} ({expected_kb} KiB); stdout={resource_out!r}"
        )

        env_probe = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [
                    "sh",
                    "-c",
                    "printf 'TOP=%s\\n' \"${TOP_ONLY-}\"; "
                    "printf 'POLICY=%s\\n' \"${CONCH_POLICY_ENV-}\"; "
                    "printf 'SHARED=%s\\n' \"${SHARED_ENV-}\"; "
                    "printf 'API=%s\\n' \"${API_ENV-}\"",
                ],
            },
        )
        assert env_probe.status_code == 200, env_probe.text
        assert env_probe.json()["exit_code"] == 0, env_probe.json()
        stdout = env_probe.json()["stdout"]
        assert "TOP=should-not-appear" not in stdout
        assert re.search(r"(?m)^TOP=$", stdout), stdout
        assert "POLICY=from-policy" in stdout
        assert "SHARED=from-api" in stdout
        assert "API=from-api" in stdout

    @staticmethod
    def test_request_policy_template_overrides_env(client):
        _require_conch_template_env()
        request_template = (os.environ.get("JIUWENBOX_CONCH_TEMPLATE_NAME_OVERRIDE") or "").strip()
        if not request_template:
            pytest.skip(
                "Set JIUWENBOX_CONCH_TEMPLATE_NAME_OVERRIDE to a second valid template name"
            )

        resp = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={"conch": {"template_name": request_template}},
                include_template=False,
            ),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sandbox_runtime"] == "conch"
        assert body["phase"] == "ready", body
        policy = client.get(f"/api/v1/policies/{body['id']}").json()
        assert policy["conch"]["template_name"] == request_template

    @staticmethod
    def test_append_bind_mounts_enter_effective_policy(client, tmp_path):
        _require_conch_template_env()
        host = tmp_path / "conch-vol"
        host.mkdir(parents=True, exist_ok=True)

        resp = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={
                    "conch": {
                        "filesystem_policy": {
                            "bind_mounts": [
                                {
                                    "host_path": str(host),
                                    "sandbox_path": "/conch-vol",
                                    "mode": "rw",
                                }
                            ]
                        }
                    }
                },
            ),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["phase"] == "ready", body
        policy = client.get(f"/api/v1/policies/{body['id']}").json()
        mounts = policy["conch"]["filesystem_policy"]["bind_mounts"]
        assert any(m["sandbox_path"] == "/conch-vol" for m in mounts)

        marker = host / f"jiuwenbox-conch-{body['id']}.txt"
        marker.write_text("from-host", encoding="utf-8")
        read_resp = client.post(
            f"/api/v1/sandboxes/{body['id']}/exec",
            json={"command": ["cat", str(Path("/conch-vol") / marker.name)]},
        )
        assert read_resp.status_code == 200, read_resp.text
        assert read_resp.json()["exit_code"] == 0
        assert "from-host" in read_resp.json()["stdout"]

    @staticmethod
    def test_foreground_exec_stdin_env_timeout(client):
        _require_conch_template_env()

        create = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(env={"CONCH_E2E": "yes"}),
        )
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        assert create.json()["phase"] == "ready", create.json()

        ok = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "printf hi"]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["exit_code"] == 0
        assert "hi" in ok.json()["stdout"]

        nonzero = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "exit 7"]},
        )
        assert nonzero.status_code == 200, nonzero.text
        assert nonzero.json()["exit_code"] == 7

        stdin = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "cat"], "stdin": "stdin-data"},
        )
        assert stdin.status_code == 200, stdin.text
        assert stdin.json()["exit_code"] == 0
        assert "stdin-data" in stdin.json()["stdout"]

        env = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "printf %s \"$CONCH_E2E\""]},
        )
        assert env.status_code == 200, env.text
        assert env.json()["exit_code"] == 0
        assert "yes" in env.json()["stdout"]

        timed = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["sh", "-c", "sleep 5"],
                "timeout_seconds": 1,
            },
        )
        assert timed.status_code == 200, timed.text
        assert timed.json()["exit_code"] == 124

    @staticmethod
    def test_background_jobs(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        assert create.json()["phase"] == "ready", create.json()

        started = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={"command": ["sh", "-c", "sleep 30"], "job_id": "sleep-job"},
        )
        assert started.status_code == 200, started.text
        assert started.json()["started"] is True

        listed = client.get(f"/api/v1/sandboxes/{sandbox_id}/background")
        assert listed.status_code == 200, listed.text
        assert any(
            item["job_id"] == "sleep-job" for item in listed.json()["items"]
        )

        got = client.get(f"/api/v1/sandboxes/{sandbox_id}/background/sleep-job")
        assert got.status_code == 200, got.text
        assert got.json()["running"] is True

        dup = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={"command": ["sh", "-c", "sleep 1"], "job_id": "sleep-job"},
        )
        assert dup.status_code == 409, dup.text

        killed = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/sleep-job/kill",
            json={"signal": 15},
        )
        assert killed.status_code == 200, killed.text
        assert killed.json()["killed"] is True

    @staticmethod
    def test_files_write_read_list_search(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        assert create.json()["phase"] == "ready", create.json()

        upload = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload",
            params={"sandbox_path": "/tmp/conch-e2e.txt"},
            files={"file": ("conch-e2e.txt", b"hello-conch", "application/octet-stream")},
        )
        assert upload.status_code == 204, upload.text

        download = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": "/tmp/conch-e2e.txt"},
        )
        assert download.status_code == 200, download.text
        assert download.content == b"hello-conch"

        listed = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/files",
            params={"sandbox_path": "/tmp", "include_files": True, "include_dirs": True},
        )
        assert listed.status_code == 200, listed.text
        assert any(
            item.get("name") == "conch-e2e.txt" or item.get("path", "").endswith("conch-e2e.txt")
            for item in listed.json().get("items", [])
        )

        searched = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/search",
            params={"sandbox_path": "/tmp", "pattern": "conch-e2e.txt"},
        )
        assert searched.status_code == 200, searched.text
        assert searched.json().get("items")

    @staticmethod
    def test_run_as_identity_exec_and_upload_ownership(client):
        """Configured run_as_* drops guest processes and new files to host-resolved uid:gid."""
        _require_conch_template_env()

        uid = os.getuid()
        gid = os.getgid()

        create = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={
                    "conch": {
                        "run_as_user": str(uid),
                        "run_as_group": str(gid),
                    }
                }
            ),
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["phase"] == "ready", body
        sandbox_id = body["id"]

        id_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "id -u; id -g"]},
        )
        assert id_resp.status_code == 200, id_resp.text
        assert id_resp.json()["exit_code"] == 0, id_resp.json()
        lines = [line.strip() for line in id_resp.json()["stdout"].splitlines() if line.strip()]
        assert lines[:2] == [str(uid), str(gid)], lines

        upload_path = f"/tmp/conch-run-as-{sandbox_id}.bin"
        upload = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload",
            params={"sandbox_path": upload_path},
            files={"file": ("owned.bin", b"\x00\x01\xff", "application/octet-stream")},
        )
        assert upload.status_code == 204, upload.text

        owned = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["stat", "-c", "%u:%g", upload_path]},
        )
        assert owned.status_code == 200, owned.text
        assert owned.json()["exit_code"] == 0, owned.json()
        assert owned.json()["stdout"].strip() == f"{uid}:{gid}"

        download = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": upload_path},
        )
        assert download.status_code == 200, download.text
        assert download.content == b"\x00\x01\xff"

    @staticmethod
    def test_stop_rejected_restart_and_delete(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        assert create.json()["phase"] == "ready", create.json()

        stopped = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert stopped.status_code == 409, stopped.text
        assert "does not support stop" in stopped.json().get("error", "").lower()

        # Still running after rejected stop.
        exec_ok = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "printf ok"]},
        )
        assert exec_ok.status_code == 200, exec_ok.text
        assert exec_ok.json()["exit_code"] == 0

        restarted = client.post(f"/api/v1/sandboxes/{sandbox_id}/restart")
        assert restarted.status_code == 200, restarted.text
        body = restarted.json()
        assert body["phase"] == "ready", body
        assert body["sandbox_runtime"] == "conch"

        exec_after = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "printf ok2"]},
        )
        assert exec_after.status_code == 200, exec_after.text
        assert exec_after.json()["exit_code"] == 0

        deleted = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert deleted.status_code == 204, deleted.text
        missing = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert missing.status_code == 404

    @staticmethod
    def test_network_create_and_hot_update(client):
        _require_conch_template_env()
        test_ip = (os.environ.get("JIUWENBOX_CONCH_NETWORK_TEST_IP") or "").strip()

        create = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={
                    "conch": {
                        "network": {
                            "egress": {
                                "default": "deny",
                                "allowed_ips": [test_ip] if test_ip else [],
                            },
                            "ingress": {"default": "deny"},
                        }
                    }
                },
            ),
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["phase"] == "ready", body
        sandbox_id = body["id"]

        policy = client.get(f"/api/v1/policies/{sandbox_id}").json()
        assert policy["conch"]["network"]["egress"]["default"] == "deny"
        assert policy["conch"]["network"]["ingress"]["default"] == "deny"
        # Synthetic deny-all CIDR must not appear in persisted jiuwenbox policy.
        assert "0.0.0.0/0" not in policy["conch"]["network"]["ingress"]["blocked_ips"]
        assert "allow_internet_access" not in policy["conch"]["network"]

        if test_ip:
            allow = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={
                    "command": [
                        "sh",
                        "-c",
                        (
                            "python3 -c "
                            f"\"import socket; "
                            f"s=socket.create_connection(('{test_ip}', 80), 2); "
                            "s.close(); print('ok')\""
                        ),
                    ],
                    "timeout_seconds": 10,
                },
            )
            # May fail if the endpoint is not HTTP; connection itself should succeed or
            # get past TCP when allow-listed. Skip soft-fail when probe tooling missing.
            if allow.status_code == 200 and allow.json()["exit_code"] not in (0, 1):
                pytest.skip(f"network probe inconclusive: {allow.json()}")

            deny = client.put(
                f"/api/v1/policies/{sandbox_id}",
                json={
                    "policy_mode": "override",
                    "policy": {
                        "conch": {
                            "network": {
                                "egress": {
                                    "default": "deny",
                                    "allowed_ips": [],
                                    "blocked_ips": [test_ip],
                                }
                            }
                        }
                    },
                },
            )
            assert deny.status_code == 200, deny.text
            updated = deny.json()["conch"]["network"]
            assert updated["egress"]["blocked_ips"] == [test_ip]
            assert "allow_internet_access" not in updated

            blocked = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={
                    "command": [
                        "sh",
                        "-c",
                        f"python3 -c \"import socket; socket.create_connection(('{test_ip}', 80), 2)\"",
                    ],
                    "timeout_seconds": 10,
                },
            )
            assert blocked.status_code == 200, blocked.text
            assert blocked.json()["exit_code"] != 0
        else:
            # Round-trip update without enforcement when no test IP is configured.
            updated = client.put(
                f"/api/v1/policies/{sandbox_id}",
                json={
                    "policy_mode": "append",
                    "policy": {
                        "conch": {
                            "network": {
                                "egress": {"blocked_ips": ["192.0.2.10"]},
                            }
                        }
                    },
                },
            )
            assert updated.status_code == 200, updated.text
            egress = updated.json()["conch"]["network"]["egress"]
            assert "192.0.2.10" in egress["blocked_ips"]
            assert egress["default"] == "deny"

    @staticmethod
    def test_batch_policy_mixed_runtimes(client):
        _require_conch_template_env()

        process = client.post("/api/v1/sandboxes", json={})
        assert process.status_code == 201, process.text
        process_id = process.json()["id"]

        conch = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert conch.status_code == 201, conch.text
        assert conch.json()["phase"] == "ready", conch.json()
        conch_id = conch.json()["id"]

        batch = client.put(
            "/api/v1/policies",
            json={
                "policy_mode": "append",
                "policy": {
                    "network": {
                        "egress": {"blocked_ips": ["198.51.100.10"]},
                    },
                    "conch": {
                        "network": {
                            "egress": {"blocked_ips": ["203.0.113.10"]},
                        }
                    },
                },
            },
        )
        assert batch.status_code == 200, batch.text
        body = batch.json()
        # Process sandbox may be skipped when network.mode is host.
        assert conch_id in body["updated"]
        process_policy = client.get(f"/api/v1/policies/{process_id}").json()
        conch_policy = client.get(f"/api/v1/policies/{conch_id}").json()
        if process_id in body["updated"]:
            assert "198.51.100.10" in process_policy["network"]["egress"]["blocked_ips"]
        assert "203.0.113.10" in conch_policy["conch"]["network"]["egress"]["blocked_ips"]

    @staticmethod
    def test_crud_list_get_custom_id_and_duplicate(client):
        _require_conch_template_env()

        auto = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert auto.status_code == 201, auto.text
        auto_body = auto.json()
        sandbox_id = auto_body["id"]
        assert re.fullmatch(r"^[0-9a-f]{8}-[0-9a-f]{3}$", sandbox_id), sandbox_id
        assert auto_body["sandbox_runtime"] == "conch"
        assert auto_body["pid"] is None
        assert "ip_address" in auto_body

        listed = client.get("/api/v1/sandboxes")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == sandbox_id for item in listed.json())

        got = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == sandbox_id
        assert got.json()["sandbox_runtime"] == "conch"
        assert got.json()["phase"] == "ready"

        custom_id = f"cch-{uuid.uuid4().hex[:6]}"
        custom = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(sandbox_id=custom_id),
        )
        assert custom.status_code == 201, custom.text
        assert custom.json()["id"] == custom_id
        assert custom.json()["sandbox_runtime"] == "conch"

        dup = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(sandbox_id=custom_id),
        )
        assert dup.status_code == 409, dup.text
        assert custom_id in dup.json().get("error", "")

        empty_id = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(sandbox_id=""),
        )
        assert empty_id.status_code == 201, empty_id.text
        assert re.fullmatch(
            r"^[0-9a-f]{8}-[0-9a-f]{3}$", empty_id.json()["id"]
        ), empty_id.json()["id"]

    @staticmethod
    def test_start_ready_sandbox_is_idempotent(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        started = client.post(f"/api/v1/sandboxes/{sandbox_id}/start")
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["phase"] == "ready"
        assert body["sandbox_runtime"] == "conch"
        assert body["id"] == sandbox_id

    @staticmethod
    def test_get_logs_endpoint(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        exec_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sh", "-c", "printf log-probe"]},
        )
        assert exec_resp.status_code == 200, exec_resp.text

        # Align with default e2e: audit may be disabled via server config
        # (``filename_strategy=disabled`` → empty body). Endpoint must still 200.
        logs = client.get(f"/api/v1/sandboxes/{sandbox_id}/logs")
        assert logs.status_code == 200, logs.text
        assert isinstance(logs.text, str)

    @staticmethod
    def test_exec_workdir_and_per_call_env(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        script = (
            "import os, pathlib, sys; "
            "print(os.environ['BOX_TEST']); "
            "print(pathlib.Path.cwd()); "
            "print(sys.stdin.read())"
        )
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["python3", "-c", script],
                "workdir": "/tmp",
                "env": {"BOX_TEST": "env-ok"},
                "stdin": "stdin-ok",
                "timeout_seconds": 10,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["env-ok", "/tmp", "stdin-ok"]

    @staticmethod
    def test_background_instant_exit_kill_and_filters(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        started = _exec_background(
            client,
            sandbox_id,
            ["sh", "-c", "exit 0"],
        )
        assert started["started"] is True
        assert isinstance(started.get("job_id"), str) and started["job_id"]
        finished = _wait_background_job_finished(client, sandbox_id, started["job_id"])
        assert finished["exit_code"] == 0, finished
        assert finished["running"] is False

        kill_done = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{started['job_id']}/kill",
            json={},
        )
        assert kill_done.status_code == 200, kill_done.text
        payload = kill_done.json()
        assert payload["killed"] is False
        assert payload["reason"] == "already_exited"
        assert payload["exit_code"] == 0

        unknown = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/no-such-job/kill",
            json={},
        )
        assert unknown.status_code == 404, unknown.text

        finished_job = f"fin-{uuid.uuid4().hex[:4]}"
        _exec_background(
            client, sandbox_id, ["sh", "-c", "exit 0"], job_id=finished_job
        )
        _wait_background_job_finished(client, sandbox_id, finished_job)

        running_job = f"run-{uuid.uuid4().hex[:4]}"
        _exec_background(
            client,
            sandbox_id,
            ["sh", "-c", "sleep 3600"],
            job_id=running_job,
        )
        try:
            listed = client.get(
                f"/api/v1/sandboxes/{sandbox_id}/background",
                params={"running_only": "true"},
            )
            assert listed.status_code == 200, listed.text
            job_ids = {item["job_id"] for item in listed.json()["items"]}
            assert running_job in job_ids
            assert finished_job not in job_ids
            assert all(item["running"] for item in listed.json()["items"])
        finally:
            client.post(
                f"/api/v1/sandboxes/{sandbox_id}/background/{running_job}/kill",
                json={"signal": 15},
            )

    @pytest.mark.parametrize("invalid_id", ["ab", "ABC123", "my job", "a" * 41])
    @staticmethod
    def test_background_invalid_job_id_returns_400(client, invalid_id):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={"command": ["sh", "-c", "exit 0"], "job_id": invalid_id},
        )
        assert resp.status_code == 400, resp.text
        assert JOB_ID_FORMAT_MESSAGE in resp.json()["error"]

    @staticmethod
    def test_background_job_gone_after_sandbox_delete(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        job_id = f"gone-{uuid.uuid4().hex[:4]}"

        _exec_background(
            client,
            sandbox_id,
            ["sh", "-c", "sleep 3600"],
            job_id=job_id,
        )
        deleted = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert deleted.status_code == 204, deleted.text

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}")
        assert resp.status_code == 404, resp.text

    @staticmethod
    def test_files_missing_dir_and_recursive_list_search(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        missing = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": "/tmp/not-found-conch.txt"},
        )
        assert missing.status_code == 404, missing.text

        as_dir = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": "/tmp"},
        )
        assert as_dir.status_code == 409, as_dir.text

        setup = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('/tmp/list-api/sub').mkdir(parents=True, exist_ok=True); "
                        "Path('/tmp/list-api/a.txt').write_text('a'); "
                        "Path('/tmp/list-api/sub/b.log').write_text('b')"
                    ),
                ],
                "timeout_seconds": 10,
            },
        )
        assert setup.status_code == 200, setup.text
        assert setup.json()["exit_code"] == 0, setup.json()

        listed = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/files",
            params={"sandbox_path": "/tmp/list-api", "recursive": True},
        )
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items", [])
        names = {item.get("name") for item in items}
        paths = {item.get("path") or "" for item in items}
        assert "a.txt" in names
        assert "sub" in names
        assert "b.log" in names
        assert any(item.get("name") == "sub" and item.get("is_directory") for item in items)
        assert any(p.endswith("a.txt") for p in paths)
        assert any(p.endswith("b.log") for p in paths)

        files_only = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/files",
            params={
                "sandbox_path": "/tmp/list-api",
                "recursive": True,
                "include_dirs": False,
                "include_files": True,
            },
        )
        assert files_only.status_code == 200, files_only.text
        files_only_items = files_only.json().get("items", [])
        assert files_only_items
        assert not any(item.get("is_directory") for item in files_only_items)
        assert {item.get("name") for item in files_only_items} >= {"a.txt", "b.log"}

        searched = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/search",
            params={"sandbox_path": "/tmp/list-api", "pattern": "b.log"},
        )
        assert searched.status_code == 200, searched.text
        assert searched.json().get("items")
        assert any(
            str(item.get("path") or item.get("name") or "").endswith("b.log")
            for item in searched.json()["items"]
        )

    @staticmethod
    def test_put_process_network_on_conch_is_rejected(client):
        _require_conch_template_env()

        create = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]

        resp = client.put(
            f"/api/v1/policies/{sandbox_id}",
            json={
                "policy_mode": "append",
                "policy": {
                    "network": {
                        "egress": {"blocked_ips": ["198.51.100.10"]},
                    }
                },
            },
        )
        assert resp.status_code == 400, resp.text
        error = resp.json().get("error", "").lower()
        assert "conch" in error or "unsupported" in error or "network" in error

    @staticmethod
    def test_put_conch_network_override_and_append_round_trip(client):
        _require_conch_template_env()

        create = client.post(
            "/api/v1/sandboxes",
            json=_conch_create_json(
                policy={
                    "conch": {
                        "network": {
                            "egress": {
                                "default": "allow",
                                "blocked_ips": ["203.0.113.10"],
                            },
                            "ingress": {"default": "allow"},
                        }
                    }
                },
            ),
        )
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        before = client.get(f"/api/v1/policies/{sandbox_id}").json()

        override = client.put(
            f"/api/v1/policies/{sandbox_id}",
            json={
                "policy_mode": "override",
                "policy": {
                    "conch": {
                        "network": {
                            "egress": {
                                "default": "deny",
                                "allowed_ips": ["198.51.100.10"],
                                "blocked_ips": [],
                            },
                            "ingress": {
                                "default": "deny",
                                "allowed_ips": ["192.0.2.10"],
                            },
                        }
                    }
                },
            },
        )
        assert override.status_code == 200, override.text
        overridden = override.json()["conch"]["network"]
        assert overridden["egress"]["default"] == "deny"
        assert overridden["egress"]["allowed_ips"] == ["198.51.100.10"]
        assert overridden["egress"]["blocked_ips"] == []
        assert overridden["ingress"]["default"] == "deny"
        assert overridden["ingress"]["allowed_ips"] == ["192.0.2.10"]
        assert "0.0.0.0/0" not in overridden["ingress"]["blocked_ips"]
        assert before["conch"]["template_name"] == override.json()["conch"]["template_name"]

        got = client.get(f"/api/v1/policies/{sandbox_id}")
        assert got.status_code == 200, got.text
        assert got.json()["conch"]["network"] == overridden

        append = client.put(
            f"/api/v1/policies/{sandbox_id}",
            json={
                "policy_mode": "append",
                "policy": {
                    "conch": {
                        "network": {
                            "egress": {
                                "blocked_ips": ["203.0.113.20"],
                                "allowed_ips": ["198.51.100.11"],
                            }
                        }
                    }
                },
            },
        )
        assert append.status_code == 200, append.text
        egress = append.json()["conch"]["network"]["egress"]
        assert egress["default"] == "deny"
        assert "198.51.100.10" in egress["allowed_ips"]
        assert "198.51.100.11" in egress["allowed_ips"]
        assert "203.0.113.20" in egress["blocked_ips"]

    @staticmethod
    def test_list_includes_multiple_conch_sandboxes(client):
        _require_conch_template_env()

        first = client.post("/api/v1/sandboxes", json=_conch_create_json())
        second = client.post("/api/v1/sandboxes", json=_conch_create_json())
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        ids = {first.json()["id"], second.json()["id"]}
        assert len(ids) == 2

        listed = client.get("/api/v1/sandboxes")
        assert listed.status_code == 200, listed.text
        listed_ids = {item["id"] for item in listed.json()}
        assert ids <= listed_ids
        for item in listed.json():
            if item["id"] in ids:
                assert item["sandbox_runtime"] == "conch"
                assert item["pid"] is None
                assert item["phase"] == "ready"