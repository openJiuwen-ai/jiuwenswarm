"""The host must import without any extension installed.

An extension may depend on the host. The host may **not** depend on an extension --
otherwise it is not an extension, it is a part of the product that happens to live in
a subdirectory, and a deployment that does not want it cannot start.

This is not hypothetical. Co-scribe reached the host through eight modules and
thirty-five import sites, and one unguarded top-level line in
``team_runtime_inheritance`` -- reached from ``harness.common.rails.__init__`` through
``team.__init__`` and ``team_manager`` -- took **five** host modules down with it,
including the agent adapter and the swarm tool registry. Nothing noticed, because the
plugin ships inside the package and so the import never actually failed.

The test simulates the deployment that does not have it: the module finder refuses
every ``jiuwenswarm.extensions.co_scribe`` name, and the host modules must still
import. Where a host path genuinely needs something from the plugin it reads it
lazily, inside the function that needs it, and answers for its absence.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

# The host modules that reference the plugin at all, plus the two entry points a
# deployment starts through. Every one of these imported the plugin transitively.
HOST_MODULES = [
    "jiuwenswarm.gateway.app_gateway",
    "jiuwenswarm.gateway.channel_manager.web.app_web_handlers",
    "jiuwenswarm.server.agent_ws_server",
    "jiuwenswarm.server.runtime.agent_adapter.interface_deep",
    "jiuwenswarm.agents.swarm.registry",
    "jiuwenswarm.agents.swarm.providers.runtime_tools",
    "jiuwenswarm.agents.harness.team.team_runtime_inheritance",
    "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers",
    "jiuwenswarm.channels.web.app_web",
]

_BLOCKED_PREFIX = "jiuwenswarm.extensions.co_scribe"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


_PROBE = """
import sys, importlib

class NotInstalled:
    # find_spec, not the removed find_module: raising straight out of the finder is
    # what an absent package looks like to every importer, and it needs no loader.
    def find_spec(self, name, path=None, target=None):
        if name.startswith({prefix!r}):
            raise ImportError(name + " is not installed (simulated)")
        return None

sys.meta_path.insert(0, NotInstalled())
importlib.import_module({module!r})
{extra}
"""


def _import_without_the_plugin(module: str, extra: str = "") -> None:
    """Import one host module in a fresh interpreter with the plugin made absent.

    **A subprocess, not sys.modules surgery.** Popping a module and re-importing it
    in this process looked simpler and was wrong twice over: modules import each
    other, so a cached neighbour lets the target resolve a name it never imported
    itself; and some of them register harness elements at import time, so a second
    import raises ``Duplicate harness element name`` -- a failure about re-import,
    not about the dependency this is checking. A clean interpreter has neither
    problem and cannot pollute the rest of the suite.
    """
    code = _PROBE.format(prefix=_BLOCKED_PREFIX, module=module, extra=extra)
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=180,
    )
    assert done.returncode == 0, (
        f"{module} does not import without the plugin:\n{done.stderr[-2000:]}"
    )


@pytest.mark.parametrize("module", HOST_MODULES)
def test_a_host_module_imports_without_the_plugin(module):
    _import_without_the_plugin(module)


def test_the_unattended_predicate_answers_for_a_missing_plugin():
    """The one host path that asks the plugin a question must answer without it.

    ``is_unattended_clouddoc_turn`` compares the turn's channel against an id only
    the plugin's watcher ever stamps, so with no plugin the honest answer is False --
    and it must be an answer, not an ImportError from inside a request.
    """
    _import_without_the_plugin(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep",
        extra=(
            "mod = sys.modules['jiuwenswarm.server.runtime.agent_adapter.interface_deep']\n"
            "assert mod.is_unattended_clouddoc_turn() is False\n"
        ),
    )
