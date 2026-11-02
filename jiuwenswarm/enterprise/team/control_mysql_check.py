# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""巡检 / 探针：确认 team 控制面写进 Gateway MySQL。

用法（仓库根，已配置 GATEWAY_DB_*）::

    python3 -m jiuwenswarm.enterprise.team.control_mysql_check inspect
    python3 -m jiuwenswarm.enterprise.team.control_mysql_check probe
"""

from __future__ import annotations

import argparse
import sys
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
        print("GATEWAY_DB_TYPE is not mysql", file=sys.stderr)
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
    print(f"team_entity rows shown={len(entities)}")
    for row in entities:
        print(
            f"  {row['team_id']}  name={row['team_name']!r}  "
            f"owner={row['owner_user_id']!r}  status={row['status']}  "
            f"session={row['team_session_id']!r}"
        )
    print(f"team_membership rows shown={len(members)}")
    for row in members:
        print(
            f"  team={row['team_id']}  user={row['user_id']!r}  "
            f"seat={row['member_name']!r}  status={row['status']}"
        )
    print(f"team_route_binding rows shown={len(routes)}")
    for row in routes:
        print(
            f"  session={row['team_session_id']!r}  team={row['team_id']!r}  "
            f"ver={row['route_version']}  ep={row['endpoint']!r}"
        )
    return 0


def probe() -> int:
    if not use_team_mysql():
        print("GATEWAY_DB_TYPE is not mysql", file=sys.stderr)
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
            print("FAIL: team_entity missing after insert", file=sys.stderr)
            return 1
        extra = row["extra"]
        if isinstance(extra, str):
            import json

            extra = json.loads(extra)
        if not extra or extra.get("probe") is not True:
            print(f"FAIL: extra json not round-tripped: {extra!r}", file=sys.stderr)
            return 1
        seats = _list("team_membership", filters={"team_id": entity.team_id})
        if [r["member_name"] for r in seats] != ["human-probe"]:
            print(f"FAIL: membership {seats!r}", file=sys.stderr)
            return 1
        binding = _get("team_route_binding", {"team_session_id": f"{prefix}_sess"})
        if binding is None or int(binding["route_version"]) != 1:
            print(f"FAIL: route {binding!r}", file=sys.stderr)
            return 1
        print(f"OK probe wrote then verified in MySQL team_id={entity.team_id}")
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
    raise SystemExit(main())
