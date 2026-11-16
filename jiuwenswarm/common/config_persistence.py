# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Atomic settings writes and a scoped two-file multimodal transaction.

All functions here run under config_write_lock. YAML replacement commits a
changed catalogue; when YAML is identical, the atomic env replacement commits.
"""

from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from dotenv.parser import parse_stream

from jiuwenswarm.common.multimodal_models import MultimodalError


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(mode)
        from jiuwenswarm.common.config import _atomic_replace

        _atomic_replace(staged, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)


def env_content(original: str, updates: dict[str, str | None]) -> str:
    """Preserve unrelated lines; quote values using python-dotenv syntax."""
    lines = []
    seen = set()
    for binding in parse_stream(StringIO(original)):
        key = binding.key
        if key not in updates:
            lines.append(binding.original.string)
        elif key not in seen:
            seen.add(key)
            if updates[key] is not None:
                lines.append(_env_line(key, updates[key]))
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key, value in updates.items():
        if key not in seen and value is not None:
            lines.append(_env_line(key, value))
    return "".join(lines)


def _env_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'\n"


def recover_multimodal_transaction(config_path: Path) -> None:
    journal_path = config_path.with_name(".multimodal-transaction.json")
    if not journal_path.exists():
        return
    transaction_id = ""
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        transaction_id = journal["transaction_id"]
        yaml_hash = sha256(config_path.read_bytes()).hexdigest()
        env_path = config_path.with_name(".env")
        env_bytes = env_path.read_bytes() if env_path.exists() else b""
        old_env, new_env = journal["old_env"].encode(), journal["new_env"].encode()
        if env_bytes not in (old_env, new_env):
            raise ValueError("unexpected environment revision")
        if yaml_hash == journal["old_yaml_hash"] == journal["new_yaml_hash"]:
            target_env = env_bytes
        elif yaml_hash == journal["old_yaml_hash"]:
            target_env = old_env
        elif yaml_hash == journal["new_yaml_hash"]:
            target_env = new_env
        else:
            raise ValueError("unexpected YAML revision")
        if env_bytes != target_env:
            atomic_write(env_path, target_env)
        journal_path.unlink()
    except Exception:
        raise MultimodalError("MM_RECOVERY_REQUIRED", {"transaction_id": transaction_id}) from None


def commit_multimodal(config_path: Path, yaml_bytes: bytes, updates: dict[str, str | None]) -> None:
    """Commit catalogue and all old execution fields, or restore pre-commit env."""
    env_path = config_path.with_name(".env")
    journal_path = config_path.with_name(".multimodal-transaction.json")
    old_yaml = config_path.read_bytes()
    # Text-mode reads normalize CRLF, but recovery compares the original bytes.
    old_env = env_path.read_bytes().decode("utf-8") if env_path.exists() else ""
    new_env = env_content(old_env, updates)
    if old_yaml == yaml_bytes and old_env == new_env:
        return
    journal = {"transaction_id": uuid4().hex, "old_yaml_hash": sha256(old_yaml).hexdigest(),
               "new_yaml_hash": sha256(yaml_bytes).hexdigest(), "old_env": old_env, "new_env": new_env}
    try:
        atomic_write(journal_path, json.dumps(journal, ensure_ascii=False).encode())
        atomic_write(env_path, new_env.encode())
        if old_yaml != yaml_bytes:
            atomic_write(config_path, yaml_bytes)
    except OSError:
        # A directory fsync may fail after replacement: consult the commit point
        # instead of claiming a rollback when the new YAML is already durable.
        recover_multimodal_transaction(config_path)
        actual_env = env_path.read_bytes() if env_path.exists() else b""
        if config_path.read_bytes() != yaml_bytes or actual_env != new_env.encode():
            raise MultimodalError("MM_STORAGE_FAILED") from None
    else:
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            # The commit already succeeded. Retain the verified journal for
            # the next lock acquisition; never report this as an unsaved edit.
            pass


def publish_environment(updates: dict[str, str | None]) -> None:
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
