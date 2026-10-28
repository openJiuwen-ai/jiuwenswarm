# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read local Process CLI Session IDs without starting an Agent or Runtime."""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from typing import TYPE_CHECKING

from jiuwenswarm.channels.process_cli.diagnostics import capture_diagnostics
from jiuwenswarm.channels.process_cli.ui import _display_width

if TYPE_CHECKING:
    from jiuwenswarm.runtime.session_catalog import SessionListResult


def _validate_query(
    args: argparse.Namespace, parser: argparse.ArgumentParser, command_args: list[str]
) -> None:
    if not (args.list_sessions or args.latest_session):
        parser.error("--search、--limit、--offset 仅用于会话查询")
    allowed = {"--list-sessions", "--latest-session", "--search", "--limit", "--offset"}
    provided_options = {
        value.partition("=")[0]
        for value in command_args
        if value.startswith("-") and not value.lstrip("-").isdecimal()
    }
    # parse_args has already rejected unknown and ambiguous options. Match its
    # accepted long-option abbreviations before enforcing the query allowlist.
    if parser.allow_abbrev:
        for option in provided_options - allowed:
            matches = [name for name in allowed if name.startswith(option)]
            if option.startswith("--") and len(matches) == 1:
                provided_options.remove(option)
                provided_options.add(matches[0])
    if args.prompt is not None or provided_options - allowed:
        parser.error("会话查询仅支持 --search、--limit、--offset，不能混用任务参数")
    if args.latest_session and (args.limit is not None or args.offset is not None):
        parser.error("--latest-session 仅支持 --search，不支持分页参数")
    if args.limit is not None and not 1 <= args.limit <= 200:
        parser.error("--limit 必须在 1 到 200 之间")
    if args.offset is not None and args.offset < 0:
        parser.error("--offset 必须是非负整数")
    if args.search is not None and len(args.search) > 200:
        parser.error("--search 不能超过 200 个字符")


def _display_text(value: str) -> str:
    return "".join(
        " " if ord(char) < 32 or 127 <= ord(char) < 160 else char for char in value
    )


def _display_time(value: float) -> str:
    if not math.isfinite(value) or value <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return "-"


def _render_sessions(result: SessionListResult) -> str:
    rows = [("会话ID", "最近消息时间", "模式", "标题", "项目目录")]
    rows.extend(
        (
            item.session_id,
            _display_time(item.last_message_at),
            item.mode,
            _display_text(item.title),
            _display_text(item.project_dir),
        )
        for item in result.sessions
    )
    column_widths = [
        max(_display_width(row[column]) for row in rows)
        for column in range(len(rows[0]) - 1)
    ]
    lines = []
    for row in rows:
        cells = [
            value + " " * (column_widths[column] - _display_width(value))
            for column, value in enumerate(row[:-1])
        ]
        lines.append("  ".join((*cells, row[-1])))
    lines.append(
        f"本页 {len(result.sessions)} 条；匹配共 {result.total} 条；offset={result.offset}；limit={result.limit}"
    )
    return "\n".join(lines) + "\n"


def execute_session_query(
    args: argparse.Namespace, parser: argparse.ArgumentParser, command_args: list[str]
) -> int:
    """Validate first, then reuse the Runtime catalog with metadata writeback disabled."""
    _validate_query(args, parser, command_args)
    with capture_diagnostics(capture_stdout=True, debug=False) as streams:
        try:
            from jiuwenswarm.runtime.session_catalog import (
                SessionListInput,
                list_sessions,
            )

            result = list_sessions(
                SessionListInput(
                    channel_id="process_cli",
                    search=args.search or "",
                    limit=1
                    if args.latest_session
                    else (args.limit if args.limit is not None else 20),
                    offset=args.offset if args.offset is not None else 0,
                ),
                include_team=True,
                read_only=True,
            )
            if args.latest_session:
                if not result.sessions:
                    streams.stderr.write("未找到匹配的 Process CLI 会话。\n")
                    return 1
                output = result.sessions[0].session_id + "\n"
            elif not result.sessions and result.total == 0:
                output = "没有匹配的 Process CLI 会话。\n"
            else:
                output = _render_sessions(result)
            streams.stdout.write(output)
            streams.stdout.flush()
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception:  # pylint: disable=broad-exception-caught
            detail = (
                f"诊断日志：{streams.log_path}"
                if streams.log_path
                else "请检查数据目录与文件访问权限"
            )
            streams.stderr.write(f"会话查询失败；{detail}\n")
            return 1
