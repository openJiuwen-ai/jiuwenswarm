# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Describe the operations hidden by Flash tool cards; never decide permissions."""

from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.tool_display import extract_call_goal


@dataclass(frozen=True)
class Operation:
    name: str
    args: dict[str, Any]
    explicit: bool = False


CRON_OPERATIONS = {
    "list": "cron_list_jobs",
    "get": "cron_get_job",
    "preview": "cron_preview_job",
    "add": "cron_create_job",
    "update": "cron_update_job",
    "remove": "cron_delete_job",
    "toggle": "cron_toggle_job",
    "status": "cron_flash.status",
    "run": "cron_flash.run",
    "wake": "cron_flash.wake",
}


def is_projected(name: str, args: dict) -> bool:
    return name in {"web_flash", "cron_flash", "todo"} or (
        name == "read_file" and "file_paths" in args
    )


def project_operations(name: str, args: dict, config: dict) -> list[Operation]:
    """Return every operation that must pass before the outer tool executes."""
    _, args = extract_call_goal(args)
    if name == "web_flash":
        if len(args) != 1:
            raise ValueError("fill exactly one web action object")
        action, inner = next(iter(args.items()))
        if (
            action not in {"search", "fetch"}
            or not isinstance(inner, dict)
            or not inner
        ):
            raise ValueError("invalid web action")
        if action == "search":
            operations = [Operation("web_search", dict(inner))]
            # The merged default search can fall back between paid/free sources.
            # Preserve explicitly configured legacy provider boundaries as well.
            for provider in ("mcp_paid_search", "mcp_free_search"):
                if provider in config.get("tools", {}) or any(
                    provider in rule.get("tools", [])
                    for rule in config.get("rules", [])
                    if isinstance(rule, dict)
                ):
                    operations.append(Operation(provider, dict(inner)))
            return operations
        urls = inner.get("url")
        if (
            not isinstance(urls, list)
            or not urls
            or not all(isinstance(u, str) and u.strip() for u in urls)
        ):
            raise ValueError("fetch requires a non-empty URL list")
        return [Operation("mcp_fetch_webpage", {**inner, "url": url}) for url in urls]

    if name == "cron_flash":
        from jiuwenswarm.agents.harness.flash.tools.cron_flash import (
            _detect_action,
            _translate_to_native,
        )

        action = _detect_action(args)
        if action not in CRON_OPERATIONS:
            raise ValueError("invalid cron action")
        inner = dict(args[action])
        if action == "add":
            inner = _translate_to_native(inner, is_update=False, context=None)
        elif action == "update":
            job_id = inner.pop("job_id", None)
            inner = {
                "job_id": job_id,
                **_translate_to_native(inner, is_update=True, context=None),
            }
        return [
            Operation(
                CRON_OPERATIONS[action], inner, action in {"run", "wake", "status"}
            )
        ]

    if name == "todo":
        action = str(args.get("action") or "").strip().lower()
        if action not in {"create", "list", "get", "modify"}:
            raise ValueError("invalid todo action")
        inner = {k: v for k, v in args.items() if k != "action"}
        if action != "modify":
            return [Operation(f"todo_{action}", inner)]
        modify = str(inner.pop("modify_action", "") or "").strip().lower()
        if modify not in {
            "update",
            "append",
            "delete",
            "cancel",
            "insert_before",
            "insert_after",
        }:
            raise ValueError("invalid todo modify_action")
        operations = [Operation("todo_modify", {"action": modify, **inner})]
        names = set()
        if modify in {"append", "insert_before", "insert_after"}:
            names.add("todo_insert")
        if modify in {"delete", "cancel"}:
            names.add("todo_remove")
        if modify == "update" and any(
            isinstance(item, dict) and item.get("status") == "completed"
            for item in inner.get("todos", [])
        ):
            names.add("todo_complete")
        operations.extend(
            Operation(key, {"action": modify, **inner}) for key in sorted(names)
        )
        return operations

    if name == "read_file" and "file_paths" in args:
        paths = args["file_paths"]
        if args.get("file_path") or not isinstance(paths, list):
            raise ValueError("read_file requires 1–10 unambiguous file_paths")
        if not 1 <= len(paths) <= 10 or not all(
            isinstance(path, str) and path.strip() for path in paths
        ):
            raise ValueError("read_file requires 1–10 unambiguous file_paths")
        return [Operation("read_file", {"file_path": path}) for path in paths]
    return []
