# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent frontmatter recipes using the Worker's existing ResourceKernel."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
from typing import Callable


def _stamp(path: Path) -> list:
    value = path.stat()
    marker = None
    if sys.platform == "win32":
        from jiuwenswarm.server.supervisor.identity_cache import _ntfs_marker

        marker = _ntfs_marker(path)
    if sys.platform == "win32" and marker is None:
        # Unsupported filesystems/open writers cannot lend a reliable NTFS
        # version. Hash just the small header instead of trusting restored mtime.
        marker = hashlib.sha256(_header(path).encode()).hexdigest()
    return [value.st_mtime_ns, value.st_ctime_ns, value.st_size, value.st_ino, marker]


def _header(path: Path) -> str:
    # Do not consume the body, even for a multi-megabyte single-line skill.
    with path.open(encoding="utf-8") as stream:
        first = stream.readline(65536)
        if first.strip() != "---":
            return ""
        lines = [first]
        for line in stream:
            lines.append(line)
            if line.strip() == "---":
                return "".join(lines) + "\n"
    return ""  # An unterminated header has the established filename defaults.


def read_metadata(path: Path, parse: Callable[..., dict | None]) -> dict | None:
    from jiuwenswarm.common.utils import get_user_workspace_dir
    from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKey, process_kernel

    absolute = os.path.normcase(os.path.abspath(path))
    try:
        stamp = _stamp(path)
    except (OSError, UnicodeError):
        return None
    identity = KernelIdentity.for_agent(project_dir="", work_mode="skill-metadata",
                                       definition="frontmatter-v1", capabilities="discovery", configuration="v1")
    key = ResourceKey(identity, "skill-metadata", absolute, "", "", json.dumps(stamp))

    def load() -> str:
        index = get_user_workspace_dir() / "cache/skill-metadata" / (hashlib.sha256(absolute.encode()).hexdigest() + ".json")
        try:
            cached = json.loads(index.read_text(encoding="utf-8"))
            if (cached.get("schema") == 1 and cached.get("path") == absolute
                    and cached.get("stamp") == stamp and isinstance(cached.get("metadata"), dict)):
                cached["metadata"].pop("body", None)
                cached["metadata"]["path"] = str(path)
                return json.dumps(cached["metadata"], ensure_ascii=False)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            metadata = parse(path, text=_header(path))
            if metadata is not None:
                metadata.pop("body", None)
            encoded = json.dumps(metadata, ensure_ascii=False, default=str)
            # Never stamp an old snapshot with a later external edit's version.
            if _stamp(path) != stamp:
                return encoded
            index.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".skill-", dir=index.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"schema": 1, "path": absolute, "stamp": stamp,
                               "metadata": metadata}, stream, ensure_ascii=False, default=str)
                os.replace(temporary, index)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return encoded
        except (OSError, UnicodeError):
            # A read-only index directory must not hide an otherwise usable skill.
            return locals().get("encoded", "null")

    return json.loads(process_kernel().recipe(key, load))
