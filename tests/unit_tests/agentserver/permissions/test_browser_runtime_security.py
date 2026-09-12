from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved domains or blocked security-test
# addresses; runtime calls are mocked and never reach external endpoints.

import shutil
import subprocess
import textwrap

import pytest
from openjiuwen.core.foundation.tool import McpServerConfig

from jiuwenswarm.server.runtime.agent_adapter import browser_runtime_security


def _config(
    *,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    server_id: str = "playwright_official_stdio",
) -> McpServerConfig:
    params: dict[str, object] = {
        "command": "npx",
        "args": args or ["-y", "@playwright/mcp@latest"],
        "cwd": "/workspace",
        "env": env or {},
        "timeout_s": 180,
    }
    return McpServerConfig(
        server_id=server_id,
        server_name="playwright-official",
        server_path="stdio://playwright",
        client_type="stdio",
        params=params,
    )


@pytest.fixture(autouse=True)
def _clear_browser_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BROWSER_DRIVER",
        "BROWSER_MANAGED_ARGS",
        *browser_runtime_security._REMOTE_OR_CUSTOM_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


def test_local_official_stdio_config_receives_owned_guard() -> None:
    original = _config(args=["-y", "@playwright/mcp@1.2.3", "--headless"])

    guarded, profile = browser_runtime_security.apply_browser_runtime_security_profile(
        original
    )

    assert profile.network_guard_enforced is True
    assert profile.guard_digest == f"sha256:{browser_runtime_security.GUARD_SHA256}"
    assert profile.egress_guard_enforced is False
    assert profile.egress_guard_failure_reason == "egress_guard_unverified"
    assert "--init-page" not in original.params["args"]
    assert guarded.params["args"][-3:] == [
        "--init-page",
        str(browser_runtime_security.GUARD_INIT_PAGE_PATH),
        "--block-service-workers",
    ]


def test_current_official_capability_argument_receives_owned_guard() -> None:
    guarded, profile = browser_runtime_security.apply_browser_runtime_security_profile(
        _config(
            args=[
                "-y",
                "@playwright/mcp@latest",
                browser_runtime_security._OFFICIAL_CAPS_ARG,
            ]
        )
    )

    assert profile.network_guard_enforced is True
    assert browser_runtime_security._OFFICIAL_CAPS_ARG in guarded.params["args"]


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (["-y", "@playwright/mcp@latest", "--extension"], None),
        (["-y", "@playwright/mcp@latest", "--config", "custom.json"], None),
        (["-y", "@playwright/mcp@latest", "--init-page", "custom.js"], None),
        (["-y", "@playwright/mcp@latest", "--proxy-server=http://proxy.invalid"], None),
        (
            ["-y", "@playwright/mcp@latest"],
            {"PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://browser.invalid:9222"},
        ),
    ],
)
def test_custom_remote_or_unsafe_config_remains_unenforced(
    args: list[str],
    env: dict[str, str] | None,
) -> None:
    guarded, profile = browser_runtime_security.apply_browser_runtime_security_profile(
        _config(args=args, env=env)
    )

    assert profile.network_guard_enforced is False
    assert "--block-service-workers" not in guarded.params["args"]


@pytest.mark.parametrize("driver", ["extension", "remote", "managed"])
def test_nonlocal_driver_modes_remain_unenforced(
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
) -> None:
    monkeypatch.setenv("BROWSER_DRIVER", driver)
    _, profile = browser_runtime_security.apply_browser_runtime_security_profile(
        _config()
    )
    assert profile.network_guard_enforced is False
    assert profile.failure_reason == "unsupported_browser_driver"


@pytest.mark.parametrize(("key", "value"), [("args", None), ("env", "malformed")])
def test_malformed_config_publishes_false_profile(key: str, value: object) -> None:
    config = _config()
    config.params[key] = value
    _, profile = browser_runtime_security.apply_browser_runtime_security_profile(config)
    assert profile.network_guard_enforced is False
    assert profile.failure_reason == "unsupported_browser_config"


def test_unrecognized_runtime_and_guard_digest_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, wrong_runtime = browser_runtime_security.apply_browser_runtime_security_profile(
        _config(server_id="custom_playwright")
    )
    monkeypatch.setattr(browser_runtime_security, "GUARD_SHA256", "0" * 64)
    _, wrong_digest = browser_runtime_security.apply_browser_runtime_security_profile(
        _config()
    )

    assert wrong_runtime.failure_reason == "unsupported_browser_runtime"
    assert wrong_digest.failure_reason == "guard_digest_mismatch"
    assert wrong_digest.network_guard_enforced is False


def test_owned_guard_routes_navigation_redirects_and_subresources() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the Playwright init-page guard")
    guard_path = browser_runtime_security.GUARD_INIT_PAGE_PATH
    script = textwrap.dedent(
        f"""
        const assert = require("node:assert/strict");
        const guard = require({str(guard_path)!r});
        const routes = [];
        const context = {{ route: async (pattern, handler) => routes.push([pattern, handler]) }};
        const page = {{ context: () => context }};
        async function decision(url) {{
          let action = "";
          const route = {{
            request: () => ({{ url: () => url }}),
            continue: async () => {{ action = "continue"; }},
            abort: async (reason) => {{ action = `abort:${{reason}}`; }},
          }};
          await routes[0][1](route);
          return action;
        }}
        (async () => {{
          await guard.default({{ page }});
          assert.equal(routes[0][0], "**/*");
          for (const url of ["https://example.invalid/page", "https://cdn.example.invalid/app.js", "about:blank", "data:text/plain,ok"]) assert.equal(await decision(url), "continue");
          for (const url of ["http://example.invalid", "https://127.0.0.1", "https://169.254.169.254/latest", "https://metadata.google.internal", "https://service.internal", "https://home.arpa", "https://router.home.arpa", "https://localhost", "https://foo.localhost", "https://[::1]"]) assert.equal(await decision(url), "abort:blockedbyclient");
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )

    result = subprocess.run(
        ["node", "-e", script], check=False, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
