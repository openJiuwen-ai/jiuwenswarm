# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reconcile the configured built-in model catalog with what each CLI reports.

The catalog in ``external_cli_agents[*].builtin_models`` mixes two kinds of
information: which models this deployment allows the team leader to pick (a
policy choice, e.g. leaving out an expensive model), and what those models
actually are (their reasoning efforts). Only the second kind belongs to the
CLI, so a reconcile drops names the CLI no longer offers, corrects their
effort lists, and merely reports models the CLI added.

Probing starts the CLI and reads its model list; it issues no model request.
"""

import asyncio
import logging
import threading
from typing import Any

from jiuwenswarm.common.config import get_config, update_external_cli_builtin_models_in_config

logger = logging.getLogger(__name__)

# Catalog entry the CLI uses for "whatever model is configured"; it is not a
# model name a member can be spawned on.
_DEFAULT_MODEL_ALIAS = "default"
DEFAULT_PROBE_TIMEOUT_S = 15.0


def reconcile_builtin_models(
    cli_agent: str,
    declared: list[dict[str, Any]],
    reported: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Correct one CLI kind's declared catalog against the reported models.

    Args:
        cli_agent: The CLI kind, used in the warning text.
        declared: Configured catalog entries.
        reported: Model id to its supported efforts, as reported by the CLI.

    Returns:
        The corrected catalog and the human-readable drift warnings.
    """
    corrected: list[dict[str, Any]] = []
    warnings: list[str] = []
    for item in declared:
        name = str(item.get("name") or "")
        if name not in reported:
            warnings.append(f"{cli_agent}: removed '{name}' — the CLI no longer offers it")
            continue
        entry = dict(item)
        efforts = reported[name]
        declared_efforts = list(entry.get("efforts") or [])
        if efforts != declared_efforts:
            warnings.append(f"{cli_agent}: '{name}' efforts {declared_efforts or '[]'} -> {efforts or '[]'}")
            if efforts:
                entry["efforts"] = efforts
            else:
                entry.pop("efforts", None)
        default_effort = str(entry.get("default_effort") or "")
        if default_effort and default_effort not in efforts:
            warnings.append(f"{cli_agent}: '{name}' default_effort '{default_effort}' is not supported; dropped")
            entry.pop("default_effort", None)
        corrected.append(entry)

    declared_names = {str(item.get("name") or "") for item in declared}
    added = [name for name in reported if name not in declared_names and name != _DEFAULT_MODEL_ALIAS]
    if added:
        # Adding a model is a cost decision, so it stays with the operator.
        warnings.append(f"{cli_agent}: the CLI also offers {', '.join(added)} (add them to builtin_models to use)")
    return corrected, warnings


async def _probe_models(cli_agent: str, cli_path: str | None) -> dict[str, list[str]]:
    """Read one CLI's model list through its harness provider."""
    if cli_agent == "claude":
        from openjiuwen.harness_providers.claudecode import ClaudeCodeHarness, ClaudeCodeHarnessConfig

        harness = ClaudeCodeHarness(ClaudeCodeHarnessConfig(cli_path=cli_path or None))
    else:
        from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig

        harness = CodexHarness(CodexHarnessConfig(codex_bin=cli_path or None))
    options = await harness.list_models()
    return {option.model_id: list(option.efforts) for option in options}


async def _probe_all(targets: dict[str, str | None], timeout_s: float) -> dict[str, dict[str, list[str]] | str]:
    """Probe every target concurrently; a failure is reported, never raised."""

    async def one(cli_agent: str, cli_path: str | None) -> tuple[str, dict[str, list[str]] | str]:
        try:
            async with asyncio.timeout(timeout_s):
                return cli_agent, await _probe_models(cli_agent, cli_path)
        except Exception as exc:  # noqa: BLE001 - a health check must not break the switch
            return cli_agent, f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(one(agent, path) for agent, path in targets.items()))
    return dict(results)


def _run_probes(targets: dict[str, str | None], timeout_s: float) -> dict[str, dict[str, list[str]] | str]:
    """Run the async probes from a synchronous caller (the config handler)."""
    probed: dict[str, dict[str, list[str]] | str] = {}

    def runner() -> None:
        nonlocal probed
        probed = asyncio.run(_probe_all(targets, timeout_s))

    thread = threading.Thread(target=runner, name="external-cli-catalog-probe", daemon=True)
    thread.start()
    # The probes carry their own deadline; this join only bounds a hung CLI.
    thread.join(timeout=timeout_s * len(targets) + 5.0)
    return probed


def refresh_external_cli_builtin_models(timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> list[str]:
    """Correct the configured catalogs against the CLIs installed on this host.

    Only CLI kinds that declare a catalog are probed, so a deployment not
    using built-in model selection pays nothing. A probe that fails (the CLI
    is not logged in, cannot start, or times out) leaves that kind's catalog
    untouched.

    Args:
        timeout_s: Per-CLI probe deadline.

    Returns:
        The drift warnings, empty when every catalog already matched.
    """
    team = get_config().get("modes", {}).get("team", {}).get("jiuwen_team", {})
    entries = team.get("external_cli_agents")
    if not isinstance(entries, list):
        return []

    declared_by_agent: dict[str, list[dict[str, Any]]] = {}
    paths: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cli_agent = str(entry.get("cli_agent") or "").strip()
        builtin_models = entry.get("builtin_models")
        if not cli_agent or not isinstance(builtin_models, list) or not builtin_models:
            continue
        declared_by_agent[cli_agent] = [dict(item) for item in builtin_models if isinstance(item, dict)]
        paths[cli_agent] = str(entry.get("cli_path") or "").strip() or None
    if not declared_by_agent:
        return []

    probed = _run_probes(paths, timeout_s)
    warnings: list[str] = []
    catalogs: dict[str, list[dict[str, Any]]] = {}
    for cli_agent, declared in declared_by_agent.items():
        reported = probed.get(cli_agent)
        if not isinstance(reported, dict):
            reason = reported or "probe did not finish"
            logger.warning("[external-cli] %s model probe failed, keeping the configured catalog: %s",
                           cli_agent, reason)
            continue
        corrected, agent_warnings = reconcile_builtin_models(cli_agent, declared, reported)
        warnings.extend(agent_warnings)
        if corrected != declared:
            catalogs[cli_agent] = corrected

    if catalogs:
        update_external_cli_builtin_models_in_config(catalogs)
    for warning in warnings:
        logger.warning("[external-cli] %s", warning)
    return warnings
