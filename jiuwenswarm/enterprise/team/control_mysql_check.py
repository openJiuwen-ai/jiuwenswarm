# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""巡检 / 探针：确认 team 控制面写进 Gateway MySQL。

用法（仓库根，已配置 GATEWAY_DB_*）::

    python3 -m jiuwenswarm.enterprise.team.control_mysql_check inspect
    python3 -m jiuwenswarm.enterprise.team.control_mysql_check probe
"""

from __future__ import annotations

import argparse
import logging
import uuid
from typing import Any

from jiuwenswarm.infrastructure.db.team_mysql_actor import (
    get_team_mysql_actor,
    list_record_dicts,
    record_to_dict,
    reset_team_mysql_actor,
    use_team_mysql,
)
from jiuwenswarm.enterprise.team.control_store import OwnerIdentity

logger = logging.getLogger(__name__)


def _list(
    table_name: str,
    *,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    order_by: list[tuple[str, bool]] | None = None,
) -> list[dict[str, Any]]:
    actor = get_team_mysql_actor()

    async def _op() -> list[dict[str, Any]]:
        return await list_record_dicts(
            actor.handler,
            table_name,
            filters=filters,
            limit=limit,
            order_by=order_by,
        )

    return actor.call(_op)


def _get(table_name: str, filters: dict[str, Any]) -> dict[str, Any] | None:
    actor = get_team_mysql_actor()

    async def _op() -> dict[str, Any] | None:
        record = await actor.handler.get(table_name, filters)
        return record_to_dict(record) if record is not None else None

    return actor.call(_op)


def inspect() -> int:
    if not use_team_mysql():
        logger.error("GATEWAY_DB_TYPE is not mysql")
        return 1
    get_team_mysql_actor()
    entities = _list(
        "team_entity",
        limit=20,
        order_by=[("updated_at", True)],
    )
    members = _list(
        "team_membership",
        limit=20,
        order_by=[("updated_at", True)],
    )
    routes = _list(
        "team_route_binding",
        limit=20,
        order_by=[("updated_at", True)],
    )
    logger.info("team_entity rows shown=%s", len(entities))
    for row in entities:
        logger.info(
            "  %s  name=%r  owner=%r  status=%s  session=%r",
            row["team_id"],
            row["team_name"],
            row["owner_user_id"],
            row["status"],
            row["team_session_id"],
        )
    logger.info("team_membership rows shown=%s", len(members))
    for row in members:
        logger.info(
            "  team=%s  user=%r  seat=%r  status=%s",
            row["team_id"],
            row["user_id"],
            row["member_name"],
            row["status"],
        )
    logger.info("team_route_binding rows shown=%s", len(routes))
    for row in routes:
        logger.info(
            "  session=%r  team=%r  ver=%s  ep=%r",
            row["team_session_id"],
            row["team_id"],
            row["route_version"],
            row["endpoint"],
        )
    return 0


def probe() -> int:
    if not use_team_mysql():
        logger.error("GATEWAY_DB_TYPE is not mysql")
        return 1
    from jiuwenswarm.enterprise.team.route_binding_mysql import MysqlTeamRouteBindingStore
    from jiuwenswarm.enterprise.team.control_mysql import MysqlTeamControlStore

    prefix = f"_probe_{uuid.uuid4().hex[:10]}"
    control = MysqlTeamControlStore()
    routes = MysqlTeamRouteBindingStore()
    owner = OwnerIdentity(user_id="probe-user", group_id="probe-g", bot_id="probe-b")
    try:
        entity = control.create_entity(
            team_name=prefix,
            owner=owner,
            team_id=prefix,
            team_session_id=f"{prefix}_sess",
            extra={"probe": True},
        )
        control.upsert_membership(
            team_id=entity.team_id,
            identity=owner,
            member_name="human-probe",
            status="active",
        )
        routes.upsert(
            team_session_id=f"{prefix}_sess",
            endpoint="http://127.0.0.1:9",
            team_id=entity.team_id,
            owner=("probe-g", "probe-b", "probe-user"),
        )
        row = _get("team_entity", {"team_id": entity.team_id})
        if row is None or row["team_name"] != prefix:
            logger.error("FAIL: team_entity missing after insert")
            return 1
        extra = row["extra"]
        if isinstance(extra, str):
            import json

            extra = json.loads(extra)
        if not extra or extra.get("probe") is not True:
            logger.error("FAIL: extra json not round-tripped: %r", extra)
            return 1
        seats = _list("team_membership", filters={"team_id": entity.team_id})
        if [r["member_name"] for r in seats] != ["human-probe"]:
            logger.error("FAIL: membership %r", seats)
            return 1
        binding = _get("team_route_binding", {"team_session_id": f"{prefix}_sess"})
        if binding is None or int(binding["route_version"]) != 1:
            logger.error("FAIL: route %r", binding)
            return 1
        logger.info("OK probe wrote then verified in MySQL team_id=%s", entity.team_id)
        return 0
    finally:
        try:
            control.delete_entity(prefix)
        except Exception:  # noqa: BLE001
            pass
        try:
            routes.delete(f"{prefix}_sess")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or probe team MySQL tables")
    parser.add_argument("command", choices=("inspect", "probe"))
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return inspect()
        return probe()
    finally:
        reset_team_mysql_actor()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
