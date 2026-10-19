from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from shared.storage import is_s3_uri, read_s3_text
from shared.tags import normalize_tags

LOGGER = logging.getLogger("index_builder")


def is_http_uri(value: str | Path) -> bool:
    parsed = urlparse(str(value).strip())
    scheme = str(parsed.scheme or "").strip().lower()
    return scheme in {"http", "https"}


def is_passthrough_item_uri(value: str | Path) -> bool:
    raw = str(value or "").strip()
    lowered = raw.lower()
    return is_s3_uri(raw) or is_http_uri(raw) or lowered.startswith("jsonl://")


def _read_http_text(uri: str, *, encoding: str = "utf-8") -> str:
    with urlopen(uri, timeout=60) as response:  # nosec B310
        payload = response.read()
    return payload.decode(encoding)


def download_http_object_to_path(uri: str, destination_path: str | Path) -> Path:
    path = Path(destination_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(uri, timeout=60) as response:  # nosec B310
        path.write_bytes(response.read())
    return path


def load_items_jsonl_text(*, item_jsonl_path: str | None = None) -> str:
    raw_path = str(item_jsonl_path or "").strip()
    if not raw_path:
        return ""
    if is_s3_uri(raw_path):
        return read_s3_text(raw_path)
    if is_http_uri(raw_path):
        return _read_http_text(raw_path)
    local_path = Path(raw_path).expanduser().resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"JSONL path not found: {local_path}")
    return local_path.read_text(encoding="utf-8")


def parse_jsonl_scanned_items(jsonl_content: str) -> tuple[dict[str, dict], list[str]]:
    scanned: dict[str, dict] = {}
    ordered_paths: list[str] = []
    seen_paths: set[str] = set()
    decoder = json.JSONDecoder()
    text = str(jsonl_content or "").lstrip("\ufeff")
    index = 0
    item_no = 0
    text_len = len(text)

    while index < text_len:
        # Accept whitespace or comma as separators between adjacent JSON objects.
        while index < text_len and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= text_len:
            break
        start_index = index
        try:
            payload, next_index = decoder.raw_decode(text, index)
        except Exception as exc:
            LOGGER.warning("skip invalid json item at char %s: %s", start_index, exc)
            next_brace = text.find("{", start_index + 1)
            if next_brace == -1:
                break
            index = next_brace
            continue
        item_no += 1
        index = next_index
        try:
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid JSON item #{item_no}: expected object")
            content_extend = payload.get("contentExtendParam")
            if not isinstance(content_extend, dict):
                raise ValueError(f"Invalid JSON item #{item_no}: missing object field 'contentExtendParam'")

            skill_id = str(content_extend.get("skillId") or "").strip()
            if not skill_id:
                raise ValueError(f"Invalid JSON item #{item_no}: missing required field contentExtendParam.skillId")
            if skill_id in scanned:
                continue

            skill_name = str(content_extend.get("skillName") or "").strip() or skill_id
            skill_desc = str(content_extend.get("skillDesc") or "").strip()
            source_path = f"jsonl://skill/{skill_id}"
            description = skill_desc or skill_name

            normalized_star = content_extend.get("stars", 0)
            try:
                stars = int(normalized_star or 0)
            except Exception:
                stars = 0

            scanned[skill_id] = {
                "id": skill_id,
                "name": skill_name,
                "description": description,
                "skill_path": source_path,
                "path": source_path,
                "github_url": str(content_extend.get("githubUrl") or content_extend.get("github_url") or ""),
                "stars": stars,
                "is_official": bool(content_extend.get("isOfficial", content_extend.get("is_official", False))),
                "author": str(content_extend.get("author") or ""),
                "tags": list(normalize_tags(content_extend.get("tags"))),
                "content_extend_param": dict(content_extend),
            }
            if source_path not in seen_paths:
                seen_paths.add(source_path)
                ordered_paths.append(source_path)
        except Exception as exc:
            LOGGER.warning("skip invalid json item #%s: %s", item_no, exc)
    return scanned, ordered_paths
