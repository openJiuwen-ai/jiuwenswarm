"""Keep Agent-side A2A contracts independent of Gateway service startup."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_fresh_python(tmp_path: Path, code: str) -> None:
    """Use a fresh interpreter so pytest collection cannot hide eager imports."""
    runtime = tmp_path / "runtime"
    config = runtime / "config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("{}\n", encoding="utf-8")
    (config / ".env").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        JIUWENSWARM_DATA_DIR=str(runtime),
        JIUWENCLAW_DATA_DIR=str(runtime),
        JIUWENSWARM_CONFIG_DIR=str(config),
        JIUWENSWARM_HOME=str(runtime / "home"),
        JIUWENSWARM_EDITION="personal",
        JIUWENSWARM_LOG_CONSOLE_ENABLED="false",
        JIUWENSWARM_LOG_FILE_ENABLED="false",
        OTEL_SDK_DISABLED="true",
    )
    script = (
        f"import sys\nsys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        + textwrap.dedent(code)
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "import_code",
    [
        "import jiuwenswarm.gateway.a2a_manager",
        "import jiuwenswarm.gateway.a2a_manager.outbound",
        """
from jiuwenswarm.gateway.a2a_manager.outbound import (
    A2AOutboundErrorCode, safe_error_summary,
)
from jiuwenswarm.gateway.a2a_manager.tool_rpc import A2A_TOOL_FIND_AGENTS
assert A2A_TOOL_FIND_AGENTS == "a2a.outbound.tool.find_agents"
assert safe_error_summary(A2AOutboundErrorCode.MANAGER_UNAVAILABLE)
""",
        """
from jiuwenswarm.agents.harness.common.rails.a2a_outbound_toolkit_rail import (
    A2AOutboundToolkitRail,
)
from jiuwenswarm.agents.harness.common.tools.a2a_outbound_tools import (
    A2AOutboundToolkit,
)
class OfflineBackend:
    ready = False
tools = A2AOutboundToolkit(OfflineBackend()).get_tools()
assert [tool.card.name for tool in tools] == [
    "a2a_find_agents", "a2a_dispatch_task", "a2a_get_dispatch",
]
A2AOutboundToolkitRail()
""",
    ],
    ids=["ingress-package", "outbound-package", "agent-contracts", "agent-tool-cards"],
)
def test_agent_imports_do_not_load_gateway_a2a_services(
    tmp_path: Path, import_code: str,
) -> None:
    _run_fresh_python(
        tmp_path,
        import_code + """
heavy_prefixes = (
    "jiuwenswarm.gateway.a2a_manager.manager",
    "jiuwenswarm.gateway.a2a_manager.outbound.discovery",
    "jiuwenswarm.gateway.a2a_manager.outbound.dispatcher",
    "jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect",
    "a2a.server",
)
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in heavy_prefixes)
)
assert not loaded, f"Agent-side import loaded Gateway A2A services: {loaded}"
""",
    )


@pytest.mark.parametrize(
    ("package_name", "expected_exports"),
    [
        (
            "jiuwenswarm.gateway.a2a_manager",
            {
                "config": (
                    "A2AIngressConfigRepository", "A2AOutboundSettingsRepository",
                    "load_a2a_ingress_config", "load_a2a_ingress_config_safely",
                ),
                "manager": ("A2AManager",),
                "models": (
                    "A2AIngressConfig", "A2AIngressError", "A2AIngressSnapshot", "A2AIngressState",
                ),
            },
        ),
        (
            "jiuwenswarm.gateway.a2a_manager.outbound",
            {
                "credentials": ("A2AOutboundCredentialStore",),
                "discovery": ("A2AOutboundDiscoveryService", "DiscoveredCard"),
                "dispatcher": ("A2AOutboundDispatcher",),
                "enterprise": ("EnterpriseA2AAgentView", "EnterpriseA2AProjection"),
                "errors": ("A2AOutboundError", "A2AOutboundErrorCode", "safe_error_summary"),
                "models": (
                    "A2ACompatibleInterface", "A2ADiscoveredAgent", "A2AOutboundAgent",
                    "A2AOutboundAvailability", "A2AOutboundDiscovery", "A2AOutboundDispatch",
                    "A2AOutboundDispatchMode", "A2AOutboundDispatchStatus", "TERMINAL_DISPATCH_STATUSES",
                ),
                "repository": (
                    "A2A_OUTBOUND_AGENT_STORE_NAME", "A2A_OUTBOUND_DISPATCH_STORE_NAME",
                    "A2AOutboundRepository", "JsonA2AOutboundRecordCodec",
                ),
                "registry": ("A2AOutboundRegistry",),
            },
        ),
    ],
    ids=["ingress-exports", "outbound-exports"],
)
def test_public_exports_keep_identity_and_star_import_compatibility(
    tmp_path: Path, package_name: str, expected_exports: dict[str, tuple[str, ...]],
) -> None:
    _run_fresh_python(
        tmp_path,
        f"""
from importlib import import_module
package_name = {package_name!r}
expected_exports = {expected_exports!r}
package = import_module(package_name)
expected_names = {{name for names in expected_exports.values() for name in names}}
assert set(package.__all__) == expected_names
assert expected_names <= set(dir(package))
assert not hasattr(package, "not_an_a2a_export")
namespace = {{}}
exec(f"from {{package_name}} import *", namespace)
for module_name, names in expected_exports.items():
    module = import_module(f"{{package_name}}.{{module_name}}")
    for name in names:
        original = getattr(module, name)
        assert getattr(package, name) is original, name
        assert namespace[name] is original, name
""",
    )


def test_concurrent_first_manager_access_keeps_one_class_identity(tmp_path: Path) -> None:
    _run_fresh_python(
        tmp_path,
        """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import jiuwenswarm.gateway.a2a_manager as package
barrier = Barrier(4)
def resolve(_):
    barrier.wait(timeout=30)
    return package.A2AManager
with ThreadPoolExecutor(max_workers=4) as pool:
    managers = list(pool.map(resolve, range(4)))
from jiuwenswarm.gateway.a2a_manager.manager import A2AManager
assert all(manager is A2AManager for manager in managers)
""",
    )
