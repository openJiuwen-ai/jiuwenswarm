"""Read published group files through existing Session output/import boundaries."""

import hashlib
from typing import Any
import json
import os
import stat
from pathlib import Path

from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime.group_files import apply_file_overrides, attachment_record_time
from jiuwenswarm.server.jiuwenmate.swarmchat.membership import require_membership


def _open_private_file(path: str, flags: int) -> int:
    safe_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    return os.open(path, safe_flags, 0o600)


def published_files(group: dict) -> list[dict]:
    path = GroupConversationLog(group["team_name"], group["group_id"],
                                workspace_path=group.get("workspace_path")).history_path
    records = []
    if path.is_file():
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    records.extend(GroupExecutions(group["group_id"]).read().values())
    files = {}
    for record in records:
        for item in apply_file_overrides(record.get("attachments") or [], group.get("file_overrides", {}),
                                         attachment_record_time(record)):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            candidate = Path(item["path"])
            try:
                if not candidate.is_file() or candidate.resolve(strict=True) != candidate.absolute():
                    continue
                metadata = candidate.stat()
                files[str(candidate)] = {"path": candidate, "name": item.get("name") or candidate.name,
                                         "size": metadata.st_size, "created_at": metadata.st_mtime}
            except OSError:
                continue
    return list(files.values())


def import_file(store: Any, execution: Any, agent_id: str, group: dict, file: dict) -> tuple[str, str, str, str]:
    """Keep source provenance inside a real host Session; expose only a current Session import."""
    require_membership(group["group_id"], agent_id)
    from jiuwenswarm.server.jiuwenmate.output_files import WorkspaceOutputFiles

    source = file["path"]
    metadata = source.stat()
    with open(source, "rb", opener=_open_private_file) as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("The published file changed while opening")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        identity = hashlib.sha256(f"{group['group_id']}:{source}".encode()).hexdigest()[:24]
        suffix = source.suffix[:16]
        if any(character in suffix for character in ('\\', ':', '\x00', '/', '<', '>', '"', '|', '?', '*')):
            suffix = ""
        name = f"swarm-{identity}-{digest[:16]}{suffix}"
        source_session = execution.session_id
        staging = store.ensure_session(execution.namespace_id, execution.agent_id, source_session) / "output" / name
        try:
            with open(staging, "xb", opener=_open_private_file) as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        except FileExistsError:
            pass
    require_membership(group["group_id"], agent_id)
    relative = f"sessions/{source_session}/output/{name}"
    imported = WorkspaceOutputFiles(store).import_approved(
        execution.namespace_id, execution.agent_id, execution.session_id,
        f"swarm-{identity}-{digest[:16]}", relative, f"sha256:{digest}", opened.st_size,
    )
    require_membership(group["group_id"], agent_id)
    return source_session, name, digest, imported


def execution_guidance(group: dict, agent_id: str | None = None) -> str:
    from jiuwenswarm.server.jiuwenmate.swarmchat.membership import ROSTER_GUIDANCE_LIMIT, roster

    members = roster(group, self_agent_id=agent_id)
    lines = []
    for item in members[:ROSTER_GUIDANCE_LIMIT]:
        label = f"{item['display_name']} ({item['kind']}{', you' if item['is_self'] else ''})"
        description = f" - {item['description']}" if item["description"] else ""
        lines.append(f"- {label}: {item['mention']}{description}")
    if len(members) > ROSTER_GUIDANCE_LIMIT:
        lines.append(f"- ... {len(members) - ROSTER_GUIDANCE_LIMIT} more; call list_group_members for the full roster.")
    files = "\n".join(f"- {file['name']}" for file in published_files(group))
    return (
        "You are participating in SwarmChat. Reply to the current public mention. "
        "For Feishu membership, call list_group_members(platform=feishu) before answering. "
        "If it cannot return the complete roster, explain the limitation and stop. "
        "Do not use lark-cli, request user login, or generate an authorization QR code for this lookup. "
        "You may ask another listed member by placing its exact [[agent:...]] token in your final reply. "
        "Use search_group_messages for additional messages or files, including other authorized groups. "
        "Use search_group_messages with platforms=[swarmchat] and content_types=[files] "
        "to import a listed file into this Session. "
        "Original group files are read-only. Do not expose private Session data.\n"
        "Group membership comes only from the roster below or from list_group_members; never infer who is in a "
        "group from who has spoken, because members who never spoke are still members. "
        "Call list_group_members for other group rosters. For Feishu, pass platform=feishu and use the group "
        "directory to choose the target. Do not use bash or CLI to look up group members.\n"
        f"Current group: {group['name']} ({group['group_id']}), "
        f"{len(members)} AI members plus the human owner (the user).\n"
        f"Current group members:\n" + "\n".join(lines) + f"\nCurrent public files:\n{files or '(none)'}"
    )
