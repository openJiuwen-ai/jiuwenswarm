"""Distributed team runtime helpers.

This module isolates config parsing/normalization logic used by TeamManager
for distributed (pyzmq) transport mode.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import logging
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_distributed_mode(config_base: dict[str, Any]) -> bool:
    team_cfg = config_base.get("team", {}) if isinstance(config_base.get("team"), dict) else {}
    runtime_cfg = team_cfg.get("runtime", {}) if isinstance(team_cfg.get("runtime"), dict) else {}
    runtime_mode = str(runtime_cfg.get("mode", "")).strip().lower()
    if runtime_mode == "distributed":
        return True
    transport_cfg = team_cfg.get("transport", {}) if isinstance(team_cfg.get("transport"), dict) else {}
    transport_type = str(transport_cfg.get("type", "")).strip().lower()
    return transport_type == "pyzmq"


def runtime_role(config_base: dict[str, Any]) -> str:
    team_cfg = config_base.get("team", {}) if isinstance(config_base.get("team"), dict) else {}
    runtime_cfg = team_cfg.get("runtime", {}) if isinstance(team_cfg.get("runtime"), dict) else {}
    role = str(runtime_cfg.get("role", "leader")).strip().lower()
    return role if role in ("leader", "teammate") else "leader"


def runtime_member_name(config_base: dict[str, Any], team_cfg: dict[str, Any]) -> str | None:
    runtime_cfg = team_cfg.get("runtime", {}) if isinstance(team_cfg.get("runtime"), dict) else {}
    configured = str(runtime_cfg.get("member_name", "")).strip()
    if configured:
        return configured
    predefined = team_cfg.get("predefined_members", [])
    if isinstance(predefined, list):
        for item in predefined:
            if isinstance(item, dict):
                member_name = str(item.get("member_name", "")).strip()
                if member_name:
                    return member_name
    return None


def parse_port(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        raw = stripped
    else:
        raw = value

    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}. Expected an integer in range 1..65535.") from exc

    if port < 1 or port > 65535:
        raise ValueError(f"Invalid {field_name}: {value!r}. Expected an integer in range 1..65535.")
    return port


def normalize_distributed_transport_fields(
    config_base: dict[str, Any],
    team_cfg: dict[str, Any],
) -> dict[str, Any]:
    normalized_cfg = copy.deepcopy(team_cfg)
    transport_cfg = normalized_cfg.get("transport", {})
    if not isinstance(transport_cfg, dict):
        return normalized_cfg
    if str(transport_cfg.get("type", "")).strip().lower() != "pyzmq":
        return normalized_cfg
    params = transport_cfg.get("params", {})
    if not isinstance(params, dict):
        return normalized_cfg

    role = runtime_role(config_base)
    if role == "leader":
        # Apply even when pubsub/direct_addr are pre-filled in the YAML template.
        params.pop("known_peers", None)
        params.pop("bootstrap_peers", None)
        params.pop("bootstrap_known_peers", None)

    if params.get("pubsub_publish_addr") and params.get("pubsub_subscribe_addr"):
        return normalized_cfg

    leader_cfg = params.get("leader", {}) if isinstance(params.get("leader"), dict) else {}
    teammate_cfg = params.get("teammate", {}) if isinstance(params.get("teammate"), dict) else {}
    leader_host = str(leader_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    leader_direct_port = parse_port(
        leader_cfg.get("direct_port"),
        18555,
        "team.transport.params.leader.direct_port",
    )
    leader_pub_port = parse_port(
        leader_cfg.get("pub_port"),
        18556,
        "team.transport.params.leader.pub_port",
    )
    leader_sub_port = parse_port(
        leader_cfg.get("sub_port"),
        18557,
        "team.transport.params.leader.sub_port",
    )
    if role == "leader":
        local_direct_addr = f"tcp://0.0.0.0:{leader_direct_port}"
        pubsub_bind = True
    else:
        leader_member_name = "team_leader"
        leader_identity_cfg = normalized_cfg.get("leader", {})
        if isinstance(leader_identity_cfg, dict):
            leader_member_name = (
                str(leader_identity_cfg.get("member_name", "")).strip() or leader_member_name
            )
        teammate_host = str(teammate_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        teammate_direct_port = parse_port(
            teammate_cfg.get("direct_port"),
            18600,
            "team.transport.params.teammate.direct_port",
        )
        local_direct_addr = f"tcp://0.0.0.0:{teammate_direct_port}"
        known_peers = [
            {
                "agent_id": leader_member_name,
                "addrs": [f"tcp://{leader_host}:{leader_direct_port}"],
            }
        ]
        pubsub_bind = False
        params["known_peers"] = known_peers
        params["bootstrap_peers"] = known_peers

    params["direct_addr"] = local_direct_addr
    params["pubsub_publish_addr"] = f"tcp://{leader_host}:{leader_pub_port}"
    params["pubsub_subscribe_addr"] = f"tcp://{leader_host}:{leader_sub_port}"
    metadata = params.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["pubsub_bind"] = pubsub_bind
    params["metadata"] = metadata
    return normalized_cfg


def is_postgresql_storage(team_cfg: dict[str, Any]) -> bool:
    storage_cfg = team_cfg.get("storage", {}) if isinstance(team_cfg.get("storage"), dict) else {}
    storage_type = str(storage_cfg.get("type", "")).strip().lower()
    return storage_type in {"postgresql", "postgres"}


def missing_distributed_dependencies(config_base: dict[str, Any]) -> list[str]:
    """Return missing Python packages required by distributed runtime."""
    if not is_distributed_mode(config_base):
        return []

    missing: list[str] = []
    if importlib.util.find_spec("zmq") is None:
        missing.append("pyzmq")

    team_cfg = config_base.get("team", {}) if isinstance(config_base.get("team"), dict) else {}
    if is_postgresql_storage(team_cfg) and importlib.util.find_spec("asyncpg") is None:
        missing.append("asyncpg")
    return missing


def fallback_distributed_to_local(config_base: dict[str, Any]) -> dict[str, Any]:
    """Create an in-memory local fallback config from distributed config."""
    normalized = copy.deepcopy(config_base)

    team_cfg = normalized.get("team", {})
    if isinstance(team_cfg, dict):
        runtime_cfg = team_cfg.get("runtime", {})
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = {}
        runtime_cfg["mode"] = "local"
        runtime_cfg["role"] = "leader"
        team_cfg["runtime"] = runtime_cfg

        transport_cfg = team_cfg.get("transport", {})
        if not isinstance(transport_cfg, dict):
            transport_cfg = {}
        transport_cfg["type"] = "inprocess"
        transport_cfg.pop("params", None)
        team_cfg["transport"] = transport_cfg

        if is_postgresql_storage(team_cfg):
            team_cfg["storage"] = {
                "type": "sqlite",
                "params": {"connection_string": "team.db"},
            }

    modes_cfg = normalized.get("modes", {})
    if isinstance(modes_cfg, dict):
        mode_team = modes_cfg.get("team", {})
        if isinstance(mode_team, dict):
            for _, candidate in mode_team.items():
                if not isinstance(candidate, dict):
                    continue
                transport_cfg = candidate.get("transport", {})
                if not isinstance(transport_cfg, dict):
                    transport_cfg = {}
                transport_cfg["type"] = "inprocess"
                transport_cfg.pop("params", None)
                candidate["transport"] = transport_cfg
                if is_postgresql_storage(candidate):
                    candidate["storage"] = {
                        "type": "sqlite",
                        "params": {"connection_string": "team.db"},
                    }

    return normalized


def extract_pg_endpoint(team_cfg: dict[str, Any]) -> tuple[str, int]:
    storage_cfg = team_cfg.get("storage", {}) if isinstance(team_cfg.get("storage"), dict) else {}
    params = storage_cfg.get("params", {}) if isinstance(storage_cfg.get("params"), dict) else {}
    connection_string = str(params.get("connection_string", "")).strip()
    if not connection_string:
        return "127.0.0.1", 5432

    # Accept sqlalchemy-style URLs like postgresql+asyncpg://user:pass@host:5432/db.
    normalized = connection_string
    if normalized.startswith("postgresql+"):
        normalized = "postgresql://" + normalized.split("://", 1)[1]
    parsed = urlparse(normalized)
    return parsed.hostname or "127.0.0.1", parsed.port or 5432


async def run_command(*args: str, subprocess_timeout_sec: float = 120.0) -> tuple[int, str]:
    cmd = " ".join(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        logger.warning("[TeamManager] subprocess spawn failed for %r: %s", cmd, exc)
        return 127, ""
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=subprocess_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[TeamManager] subprocess timed out after %ss: %r",
            subprocess_timeout_sec,
            cmd,
        )
        try:
            proc.kill()
        except OSError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.debug("[TeamManager] subprocess wait after timeout kill: %s", exc)
        return 124, ""
    except OSError as exc:
        logger.warning("[TeamManager] subprocess communicate failed for %r: %s", cmd, exc)
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
        return 126, ""
    return proc.returncode, (
        stdout.decode("utf-8", errors="replace") if stdout else ""
    )


async def is_pg_available(host: str, port: int, *, subprocess_timeout_sec: float = 120.0) -> bool:
    rc, _ = await run_command(
        "pg_isready",
        "-h",
        host,
        "-p",
        str(port),
        subprocess_timeout_sec=subprocess_timeout_sec,
    )
    return rc == 0


async def try_start_pg_cluster(*, subprocess_timeout_sec: float = 120.0) -> bool:
    rc, out = await run_command(
        "pg_lsclusters",
        "--no-header",
        subprocess_timeout_sec=subprocess_timeout_sec,
    )
    if rc != 0:
        return False
    first_line = next((line.strip() for line in out.splitlines() if line.strip()), "")
    if not first_line:
        return False
    parts = first_line.split()
    if len(parts) < 2:
        return False
    version, cluster = parts[0], parts[1]
    start_rc, _ = await run_command(
        "pg_ctlcluster",
        version,
        cluster,
        "start",
        subprocess_timeout_sec=subprocess_timeout_sec,
    )
    return start_rc == 0


async def ensure_postgresql_for_leader(
    config_base: dict[str, Any],
    *,
    subprocess_timeout_sec: float = 120.0,
    post_start_ready_max_sec: float = 30.0,
    post_start_ready_init_sleep: float = 0.4,
    post_start_ready_max_sleep: float = 2.0,
    post_start_ready_backoff: float = 1.45,
    post_start_log_every_sec: float = 5.0,
) -> None:
    if runtime_role(config_base) != "leader":
        return
    team_cfg = (
        config_base.get("team", {})
        if isinstance(config_base.get("team"), dict)
        else {}
    )
    if not is_postgresql_storage(team_cfg):
        return

    host, port = extract_pg_endpoint(team_cfg)
    if await is_pg_available(host, port, subprocess_timeout_sec=subprocess_timeout_sec):
        return

    logger.warning(
        "[TeamManager] PostgreSQL not reachable at %s:%s, attempting auto-start as leader",
        host,
        port,
    )

    started = await try_start_pg_cluster(
        subprocess_timeout_sec=subprocess_timeout_sec
    )
    if not started:
        for cmd in (("systemctl", "start", "postgresql"), ("service", "postgresql", "start")):
            rc, _ = await run_command(
                *cmd,
                subprocess_timeout_sec=subprocess_timeout_sec,
            )
            if rc == 0:
                started = True
                break

    if not started:
        logger.warning(
            "[TeamManager] PostgreSQL auto-start command failed, "
            "will continue and rely on DB connect error"
        )
        return

    logger.info(
        "[TeamManager] start command returned OK; waiting up to %.0fs for %s:%s to accept connections",
        post_start_ready_max_sec,
        host,
        port,
    )
    deadline = time.monotonic() + post_start_ready_max_sec
    sleep_for = post_start_ready_init_sleep
    wait_t0 = time.monotonic()
    last_log = wait_t0
    while time.monotonic() < deadline:
        if await is_pg_available(host, port, subprocess_timeout_sec=subprocess_timeout_sec):
            logger.info(
                "[TeamManager] PostgreSQL is ready at %s:%s (waited %.1fs after start)",
                host,
                port,
                time.monotonic() - wait_t0,
            )
            return
        now = time.monotonic()
        if now - last_log >= post_start_log_every_sec:
            logger.info(
                "[TeamManager] still waiting for PostgreSQL at %s:%s (%.0fs / %.0fs max)",
                host,
                port,
                now - wait_t0,
                post_start_ready_max_sec,
            )
            last_log = now
        remaining = deadline - now
        if remaining <= 0:
            break
        await asyncio.sleep(min(sleep_for, remaining))
        sleep_for = min(
            sleep_for * post_start_ready_backoff,
            post_start_ready_max_sleep,
        )

    logger.warning(
        "[TeamManager] PostgreSQL start attempted but endpoint still not ready after %.0fs: %s:%s",
        post_start_ready_max_sec,
        host,
        port,
    )
