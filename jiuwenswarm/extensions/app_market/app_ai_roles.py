"""Validate the optional AI role declaration shipped with an application."""

import base64
import json
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import Any


APP_AI_ROLES_FILE = "app-ai-roles.json"
_MAX_BYTES = 64 * 1024
_MAX_AVATAR_BYTES = 256 * 1024
_MAX_TOTAL_AVATAR_BYTES = 1024 * 1024
_AVATAR_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    return value.strip()


def _avatar_path(root: Path, value: object, field: str) -> tuple[Path, str]:
    text = _text(value, field)
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts or any(char in text for char in ("\\", ":")):
        raise ValueError(f"{field} must be relative to the app directory")
    avatar = (root / relative).resolve()
    mime = _AVATAR_TYPES.get(relative.suffix.lower())
    if not avatar.is_relative_to(root) or not mime or not avatar.is_file():
        raise ValueError(f"{field} must reference a local PNG, JPEG, WebP or GIF file")
    return avatar, mime


def _read_avatar(avatar: Path, mime: str, limit: int) -> tuple[str, int]:
    try:
        with avatar.open("rb") as source:
            data = source.read(limit + 1)
    except OSError:
        return "", 0
    if not data or len(data) > limit:
        return "", 0
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", len(data)


def _read_group(root: Path, group: object, field: str, avatar_budget: int) -> tuple[dict[str, Any], int]:
    if not isinstance(group, dict) or group.get("mode") not in ("agent", "team"):
        raise ValueError(f"{field}.mode must be agent or team")
    mode = group["mode"]
    group_id = _text(group.get("id"), f"{field}.id")
    session_description = _text(group.get("session_description"), f"{field}.session_description")
    roles = group.get("roles")
    if mode == "agent":
        if not isinstance(roles, list) or len(roles) != 1 or "leader" in group:
            raise ValueError(f"{field} must have one role and no leader in agent mode")
    elif not isinstance(roles, list) or not 2 <= len(roles) <= 6:
        raise ValueError(f"{field} must have a leader and 1 to 5 members in team mode")

    role_ids: set[str] = set()
    profiles: list[dict[str, str]] = []
    for index, role in enumerate(roles):
        role_field = f"{field}.roles[{index}]"
        if not isinstance(role, dict):
            raise ValueError(f"{role_field} must be an object")
        role_id = _text(role.get("id"), f"{role_field}.id")
        if role_id in role_ids:
            raise ValueError(f"{role_field}.id must be unique within the group")
        role_ids.add(role_id)
        profile = {
            "id": role_id,
            "name": _text(role.get("name"), f"{role_field}.name"),
            "description": _text(role.get("description"), f"{role_field}.description"),
        }
        if "avatar" in role:
            avatar, mime = _avatar_path(root, role["avatar"], f"{role_field}.avatar")
            if avatar_budget:
                url, used = _read_avatar(avatar, mime, min(avatar_budget, _MAX_AVATAR_BYTES))
                if url:
                    profile["avatar_url"] = url
                    avatar_budget -= used
        profiles.append(profile)

    result = {"id": group_id, "mode": mode, "session_description": session_description, "roles": profiles}
    if mode == "team":
        leader = _text(group.get("leader"), f"{field}.leader")
        if leader not in role_ids:
            raise ValueError(f"{field}.leader must reference a role in the group")
        result["leader"] = leader
    return result, avatar_budget


def load_ai_role_groups(app_dir: Path, *, include_avatars: bool = False) -> list[dict[str, Any]]:
    """Read public role metadata, preserving compatibility with apps without a declaration."""
    try:
        root = app_dir.resolve()
        manifest = root / APP_AI_ROLES_FILE
        try:
            file_mode = manifest.lstat().st_mode
        except FileNotFoundError:
            return []
        if not S_ISREG(file_mode):
            raise ValueError("must be a regular file")
        with manifest.open("rb") as source:
            contents = source.read(_MAX_BYTES + 1)
        if len(contents) > _MAX_BYTES:
            raise ValueError("must be at most 64 KiB")
        declaration = json.loads(contents.decode("utf-8"))
        version = declaration.get("schema_version") if isinstance(declaration, dict) else None
        if type(version) is not int or version != 1:
            raise ValueError("schema_version must be 1")
        groups = declaration.get("session_groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError("session_groups must contain at least one group")
        group_ids: set[str] = set()
        groups_for_display = []
        avatar_budget = _MAX_TOTAL_AVATAR_BYTES if include_avatars else 0
        for index, group in enumerate(groups):
            field = f"session_groups[{index}]"
            display_group, avatar_budget = _read_group(root, group, field, avatar_budget)
            group_id = display_group["id"]
            if group_id in group_ids:
                raise ValueError(f"{field}.id must be unique within the app")
            group_ids.add(group_id)
            groups_for_display.append(display_group)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"Invalid {APP_AI_ROLES_FILE}: {exc}") from exc
    return groups_for_display
