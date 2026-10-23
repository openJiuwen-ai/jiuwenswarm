# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the configured built-in model catalog policy and the CLIs' facts apart.

The catalog in ``external_cli_agents[*].builtin_models`` is policy: which
models this deployment lets the team leader pick, how they are described, and
which effort is the default. What those models actually support belongs to
the CLI. A refresh probes each enabled CLI's ``list_models`` and stores the
report in ``runtime/external-cli-models.json``; the configuration file is
never rewritten with facts. When the team is assembled,
:func:`apply_reported_models` reconciles the policy with the stored report:
names the CLI no longer offers are dropped and efforts come from the CLI.

Custom agents (uploaded harness providers) have no hand-written policy, so an
enabled one whose provider declares ``MODEL_DISCOVERY`` gets a catalog
allowing every model it reports.

Probing starts the CLI and reads its model list; it issues no model request.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config, update_external_cli_builtin_models_in_config
from jiuwenswarm.common.utils import get_user_workspace_dir

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
    from jiuwenswarm.common.windows_cli_paths import resolve_preserved_cli_path

    cli_path = resolve_preserved_cli_path(cli_path)
    if cli_agent == "claude":
        from openjiuwen.harness_providers.claudecode import ClaudeCodeHarness, ClaudeCodeHarnessConfig

        harness = ClaudeCodeHarness(ClaudeCodeHarnessConfig(cli_path=cli_path or None))
    elif cli_agent == "codex":
        from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig

        harness = CodexHarness(CodexHarnessConfig(codex_bin=cli_path or None))
    else:
        return await _probe_custom_agent_models(cli_agent, cli_path)
    options = await harness.list_models()
    return {option.model_id: list(option.efforts) for option in options}


async def _probe_custom_agent_models(cli_agent: str, cli_path: str | None) -> dict[str, list[str]]:
    """Read a custom agent's model list through its uploaded harness provider.

    The probe runs in a scratch directory: an agent that opens a session to
    report its models must not leave one in the user's projects.
    """
    from openjiuwen.harness_providers import resolve_provider

    provider = resolve_provider(cli_agent)
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-model-probe-") as cwd:
        config: dict[str, Any] = {"cwd": cwd}
        if cli_path:
            config["cli_path"] = cli_path
        options = await provider.create(config).list_models()
    return {option.model_id: list(option.efforts) for option in options}


def _discoverable_custom_agents(entries: list[Any]) -> dict[str, str | None]:
    """Return enabled custom agents without a catalog whose provider can list models.

    Maps each agent to its configured CLI path.
    """
    from jiuwenswarm.common.custom_agent_runtime import activate_custom_agent_paths, custom_agent_names
    from openjiuwen.harness_protocol import HarnessCapability
    from openjiuwen.harness_providers import resolve_provider

    custom_names = set(custom_agent_names())
    if not custom_names:
        return {}
    activate_custom_agent_paths()
    targets: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("builtin_models"):
            continue
        cli_agent = str(entry.get("cli_agent") or "").strip()
        if cli_agent not in custom_names:
            continue
        try:
            discoverable = resolve_provider(cli_agent).card.supports(HarnessCapability.MODEL_DISCOVERY)
        except ValueError as exc:
            logger.warning("[external-cli] custom agent %s cannot be loaded: %s", cli_agent, exc)
            continue
        if discoverable:
            targets[cli_agent] = str(entry.get("cli_path") or "").strip() or None
    return targets


def _discovered_catalog(reported: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Allow every reported model; efforts stay in the stored report."""
    return [{"name": name} for name in reported if name != _DEFAULT_MODEL_ALIAS]


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


def reported_models_path() -> Path:
    """Return the file holding each CLI's last reported model list."""
    return get_user_workspace_dir() / "runtime" / "external-cli-models.json"


def load_reported_models() -> dict[str, dict[str, list[str]]]:
    """Return the stored reports: CLI kind to model id to its efforts."""
    try:
        payload = json.loads(reported_models_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[external-cli] ignoring unreadable model report: %s", exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    reports: dict[str, dict[str, list[str]]] = {}
    for cli_agent, models in payload.items():
        if isinstance(models, dict):
            reports[str(cli_agent)] = {
                str(name): [str(effort) for effort in efforts]
                for name, efforts in models.items()
                if isinstance(efforts, list)
            }
    return reports


def _store_reported_models(reports: dict[str, dict[str, list[str]]]) -> None:
    path = reported_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def refresh_external_cli_builtin_models(timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> list[str]:
    """Probe the CLIs whose built-in models the team may pick and store their reports.

    Probed are the kinds declaring a catalog, plus enabled custom agents whose
    provider can list models (those without a catalog get one allowing every
    reported model). A deployment not using built-in model selection pays
    nothing. A probe that fails (the CLI is not logged in, cannot start, or
    times out) keeps that kind's previous report.

    Args:
        timeout_s: Per-CLI probe deadline.

    Returns:
        The drift between each catalog and its CLI's report, for the log.
    """
    team = get_config().get("modes", {}).get("team", {}).get("jiuwen_team", {})
    entries = team.get("external_cli_agents")
    if not isinstance(entries, list):
        return []
    seeds = _discoverable_custom_agents(entries)
    declared_by_agent: dict[str, list[dict[str, Any]]] = {}
    paths: dict[str, str | None] = dict(seeds)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cli_agent = str(entry.get("cli_agent") or "").strip()
        builtin_models = entry.get("builtin_models")
        if not cli_agent or not isinstance(builtin_models, list) or not builtin_models:
            continue
        declared_by_agent[cli_agent] = [dict(item) for item in builtin_models if isinstance(item, dict)]
        paths[cli_agent] = str(entry.get("cli_path") or "").strip() or None
    if not paths:
        return []

    probed = _run_probes(paths, timeout_s)
    reports = load_reported_models()
    catalogs: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for cli_agent in paths:
        reported = probed.get(cli_agent)
        if not isinstance(reported, dict):
            logger.warning("[external-cli] %s model probe failed, keeping the last report: %s",
                           cli_agent, reported or "probe did not finish")
            continue
        reports[cli_agent] = reported
        if cli_agent in seeds:
            catalog = _discovered_catalog(reported)
            if catalog:
                catalogs[cli_agent] = catalog
            continue
        _corrected, agent_warnings = reconcile_builtin_models(cli_agent, declared_by_agent[cli_agent], reported)
        warnings.extend(agent_warnings)

    _store_reported_models(reports)
    if catalogs:
        update_external_cli_builtin_models_in_config(catalogs)
    for warning in warnings:
        logger.warning("[external-cli] %s", warning)
    return warnings


def apply_reported_models(entries: list[Any]) -> None:
    """Reconcile each entry's catalog with its CLI's stored report, in place.

    Entries whose CLI was never probed keep their catalog as configured.
    """
    reports = load_reported_models()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cli_agent = str(entry.get("cli_agent") or "").strip()
        declared = entry.get("builtin_models")
        reported = reports.get(cli_agent)
        if not isinstance(declared, list) or not declared or reported is None:
            continue
        corrected, drift = reconcile_builtin_models(
            cli_agent,
            [dict(item) for item in declared if isinstance(item, dict)],
            reported,
        )
        for line in drift:
            logger.info("[external-cli] %s", line)
        entry["builtin_models"] = corrected
