# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把已有的 team_control.db / team_route_bindings.db 导入 Gateway MySQL。

主键冲突的行保留库里已有的数据。文件或表不存在时跳过。
在把进程切到 mysql 之前执行一次。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from jiuwenswarm.infrastructure.db.team_mysql_actor import (
    get_team_mysql_actor,
    run_tx,
    use_team_mysql,
)


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback


def _sqlite_rows(path: Path, table: str) -> list[sqlite3.Row] | None:
    if not path.is_file():
        print(f"skip missing file: {path}")
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if found is None:
            print(f"skip missing table {table} in {path}")
            return None
        return list(conn.execute(f"SELECT * FROM {table}").fetchall())
    finally:
        conn.close()


def _insert_ignore(sql: str, rows: list[dict[str, Any]]) -> int:
    actor = get_team_mysql_actor()

    async def _op() -> int:
        inserted = 0

        async def _tx(conn: Any) -> None:
            nonlocal inserted
            for params in rows:
                result = await conn.execute(text(sql), params)
                inserted += int(result.rowcount or 0)

        await run_tx(actor.handler, _tx)
        return inserted

    return actor.call(_op)


def import_control(path: Path) -> None:
    entities = _sqlite_rows(path, "team_entity")
    if entities is not None:
        written = _insert_ignore(
            "INSERT IGNORE INTO team_entity ("
            "team_id, team_name, owner_user_id, owner_group_id, owner_bot_id, status, "
            "runtime_mode, template_id, team_session_id, runtime_team_name, "
            "agent_service_id, gateway_id, created_at, updated_at, extra"
            ") VALUES ("
            ":team_id, :team_name, :owner_user_id, :owner_group_id, :owner_bot_id, :status, "
            ":runtime_mode, :template_id, :team_session_id, :runtime_team_name, "
            ":agent_service_id, :gateway_id, :created_at, :updated_at, CAST(:extra AS json)"
            ")",
            [
                {
                    "team_id": row["team_id"],
                    "team_name": row["team_name"],
                    "owner_user_id": row["owner_user_id"],
                    "owner_group_id": row["owner_group_id"],
                    "owner_bot_id": row["owner_bot_id"],
                    "status": row["status"],
                    "runtime_mode": row["runtime_mode"],
                    "template_id": row["template_id"],
                    "team_session_id": row["team_session_id"],
                    "runtime_team_name": row["runtime_team_name"],
                    "agent_service_id": row["agent_service_id"],
                    "gateway_id": row["gateway_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "extra": json.dumps(_loads(row["extra"], {}), ensure_ascii=False),
                }
                for row in entities
            ],
        )
        print(f"team_entity source={len(entities)} inserted={written}")

    members = _sqlite_rows(path, "team_membership")
    if members is None:
        return
    written = _insert_ignore(
        "INSERT IGNORE INTO team_membership ("
        "membership_id, team_id, user_id, group_id, bot_id, member_name, member_type, "
        "display_name, permissions, status, expires_at, joined_at, updated_at"
        ") VALUES ("
        ":membership_id, :team_id, :user_id, :group_id, :bot_id, :member_name, :member_type, "
        ":display_name, CAST(:permissions AS json), :status, :expires_at, :joined_at, "
        ":updated_at)",
        [
            {
                "membership_id": row["membership_id"],
                "team_id": row["team_id"],
                "user_id": row["user_id"],
                "group_id": row["group_id"],
                "bot_id": row["bot_id"],
                "member_name": row["member_name"],
                "member_type": row["member_type"],
                "display_name": row["display_name"],
                "permissions": json.dumps(_loads(row["permissions"], []), ensure_ascii=False),
                "status": row["status"],
                "expires_at": row["expires_at"],
                "joined_at": row["joined_at"],
                "updated_at": row["updated_at"],
            }
            for row in members
        ],
    )
    print(f"team_membership source={len(members)} inserted={written}")


def import_routes(path: Path) -> None:
    rows = _sqlite_rows(path, "team_route_binding")
    if rows is None:
        return
    written = _insert_ignore(
        "INSERT IGNORE INTO team_route_binding ("
        "team_session_id, team_id, endpoint, agent_service_id, route_request_id, "
        "owner_user_id, owner_group_id, owner_bot_id, gateway_id, status, route_version, "
        "created_at, updated_at, last_heartbeat_at"
        ") VALUES ("
        ":team_session_id, :team_id, :endpoint, :agent_service_id, :route_request_id, "
        ":owner_user_id, :owner_group_id, :owner_bot_id, :gateway_id, :status, "
        ":route_version, :created_at, :updated_at, :last_heartbeat_at)",
        [dict(row) for row in rows],
    )
    print(f"team_route_binding source={len(rows)} inserted={written}")


def main(argv: list[str] | None = None) -> int:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    parser = argparse.ArgumentParser(
        description="Import team SQLite rows into Gateway MySQL"
    )
    workspace = get_user_workspace_dir()
    parser.add_argument(
        "--control-db",
        type=Path,
        default=workspace / "agent" / "teams" / "team_control.db",
    )
    parser.add_argument(
        "--route-db",
        type=Path,
        default=workspace / "gateway" / "team_route_bindings.db",
    )
    args = parser.parse_args(argv)
    if not use_team_mysql():
        print("GATEWAY_DB_TYPE is not mysql; refuse to import", file=sys.stderr)
        return 1
    get_team_mysql_actor()
    import_control(args.control_db)
    import_routes(args.route_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
