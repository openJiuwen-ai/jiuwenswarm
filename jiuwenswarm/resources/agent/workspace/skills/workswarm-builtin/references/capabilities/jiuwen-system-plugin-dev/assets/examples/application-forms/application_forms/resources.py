# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validate immutable frontend resource archives and their declared settings."""

import hashlib
import json
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZipFile

ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
FIELD = re.compile(r"[a-z][a-zA-Z0-9_]{0,63}")
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
FILE_TYPES = {".html", ".js", ".css", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".woff", ".woff2", ".mp3", ".wav", ".ogg", ".webm", ".mp4"}


def relative_path(value: object) -> str:
    """Accept one canonical relative resource path."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("Invalid resource path")
    if any(char in '<>:"\\|?*' or ord(char) < 32 for char in value):
        raise ValueError("Invalid resource path")
    parts = value.split("/")
    if len(parts) > 16 or any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255
                              or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts):
        raise ValueError("Invalid resource path")
    return value


def localized(value: object) -> dict[str, str]:
    """Require both supported languages instead of guessing labels."""
    if not isinstance(value, dict) or set(value) != {"zh", "en"}:
        raise ValueError("Both zh and en are required")
    if any(not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 1024 for item in value.values()):
        raise ValueError("Invalid localized text")
    return value


def setting_value(field: dict, value: object) -> object:
    """Validate a saved or submitted value against its exact resource field."""
    kind = field["kind"]
    if kind == "switch" and type(value) is bool:
        return value
    if kind == "select" and isinstance(value, str) and value in {item["value"] for item in field["options"]}:
        return value
    if kind == "input" and isinstance(value, str) and len(value.encode("utf-16-le")) // 2 <= field["maxLength"]:
        return value
    if kind == "image" and isinstance(value, str) and (not value or (Path(value).suffix in {".png", ".jpg", ".webp", ".gif"} and re.fullmatch(r"[0-9a-f]{64}", Path(value).stem))):
        return value
    raise ValueError("Setting value does not match its definition")


def validate_manifest(raw: object) -> dict:
    """Validate the resource format independently of executable page code."""
    required = {"schemaVersion", "id", "version", "name", "description", "entry", "preview", "window", "config", "settings"}
    if not isinstance(raw, dict) or set(raw) != required or type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ValueError("Unsupported resource manifest")
    if not isinstance(raw["id"], str) or not ID.fullmatch(raw["id"]):
        raise ValueError("Invalid resource ID")
    if not isinstance(raw["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", raw["version"]):
        raise ValueError("Invalid resource version")
    for key in ("name", "description"):
        localized(raw[key])
    if PurePosixPath(relative_path(raw["entry"])).suffix != ".html":
        raise ValueError("Resource entry must be HTML")
    if PurePosixPath(relative_path(raw["preview"])).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
        raise ValueError("Invalid preview image")
    window = raw["window"]
    if not isinstance(window, dict) or set(window) != {"width", "height"}:
        raise ValueError("Invalid resource window")
    if any(type(window[key]) is not int or not low <= window[key] <= 960 for key, low in (("width", 160), ("height", 120))):
        raise ValueError("Invalid resource dimensions")
    if not isinstance(raw["config"], dict) or {"settings", "assetBase", "transitionSound"}.intersection(raw["config"]):
        raise ValueError("Invalid resource config")
    if len(json.dumps(raw["config"], allow_nan=False).encode("utf-8")) > 8192:
        raise ValueError("Resource config is too large")
    fields = raw["settings"]
    if not isinstance(fields, list) or len(fields) > 32:
        raise ValueError("Invalid resource settings")
    keys = set()
    for field in fields:
        if not isinstance(field, dict) or not {"key", "kind", "title", "default"}.issubset(field):
            raise ValueError("Invalid setting definition")
        key = field["key"]
        if not isinstance(key, str) or not FIELD.fullmatch(key) or key in keys:
            raise ValueError("Invalid setting key")
        keys.add(key)
        localized(field["title"])
        if "description" in field:
            localized(field["description"])
        allowed = {"key", "kind", "title", "description", "default"}
        if field["kind"] == "select":
            allowed.add("options")
            options = field.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 32:
                raise ValueError("Invalid setting options")
            values = set()
            for option in options:
                if not isinstance(option, dict) or set(option) != {"value", "label"}:
                    raise ValueError("Invalid setting option")
                value = option["value"]
                if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 200 or value in values:
                    raise ValueError("Invalid setting option value")
                values.add(value)
                localized(option["label"])
        elif field["kind"] == "input":
            allowed.add("maxLength")
            if type(field.get("maxLength")) is not int or not 1 <= field["maxLength"] <= 1000:
                raise ValueError("Invalid input length")
        elif field["kind"] == "image":
            if field["default"] != "":
                raise ValueError("Image default must be empty")
        elif field["kind"] != "switch":
            raise ValueError("Unsupported setting kind")
        if set(field) - allowed:
            raise ValueError("Unknown setting properties")
        setting_value(field, field["default"])
    return raw


def settings_values(resource: dict, saved: object = None) -> dict:
    """Resolve only absent values to declared defaults; malformed state is an error."""
    fields = resource["settings"]
    if saved is None:
        return {field["key"]: field["default"] for field in fields}
    if not isinstance(saved, dict) or set(saved) != {field["key"] for field in fields}:
        raise ValueError("Saved settings do not match this resource")
    return {field["key"]: setting_value(field, saved[field["key"]]) for field in fields}


def load_json(content: bytes) -> object:
    """Reject duplicate keys and non-finite JSON constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError(f"Invalid JSON constant: {value}")
    return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid)


def import_resource(root: Path, data: dict, occupied: set[str]) -> tuple[dict, str]:
    """Validate every member before publishing one immutable resource directory."""
    if len(data["files"]) != 1 or not 0 < data["total_bytes"] <= MAX_ARCHIVE:
        raise ValueError("Choose one resource ZIP up to 64 MiB")
    directory = (root / data["path"]).resolve(strict=True)
    if not directory.is_relative_to((root / ".uploads").resolve(strict=True)):
        raise ValueError("Invalid upload directory")
    source = directory / data["files"][0]["path"]
    if source.is_symlink() or source.parent != directory or source.stat().st_size != data["total_bytes"]:
        raise ValueError("Invalid resource upload")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > MAX_EXPANDED:
            raise ValueError("Resource archive exceeds its limits")
        seen = set()
        names = set()
        for item in entries:
            name = relative_path(item.filename[:-1] if item.is_dir() else item.filename)
            folded = name.casefold()
            if folded in seen or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Duplicate, encrypted or symbolic resource member")
            seen.add(folded)
            if not item.is_dir():
                if PurePosixPath(name).suffix.lower() not in FILE_TYPES:
                    raise ValueError("Unsupported resource file type")
                names.add(name)
        resource = validate_manifest(load_json(archive.read("presentation.json")))
        folded_names = {name.casefold() for name in names}
        for name in names:
            if any(str(parent).casefold() in folded_names for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise ValueError("A resource file is also a directory")
        if resource["id"].startswith("builtin-") or resource["id"] in occupied or resource["entry"] not in names or resource["preview"] not in names:
            raise ValueError("Resource ID exists or a required file is missing")
        destination = root / "public" / "resources" / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".resource-", dir=root))
        try:
            for item in entries:
                if item.is_dir():
                    continue
                output = temporary / item.filename
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
            if not destination.exists():
                temporary.replace(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return resource, digest


def builtin_resources() -> list[dict]:
    """Only the bee is available before users import complete form packages."""
    return [validate_manifest({
        'schemaVersion': 1, 'id': 'builtin-bee', 'version': '1.8.1',
        'name': {'zh': '蜜蜂桌宠', 'en': 'Bee companion'},
        'description': {'zh': '轻点查看会话，双击工作台。', 'en': 'Click to view conversations, double-click for the workspace.'},
        'entry': 'presentation.html', 'preview': 'bee/idle.png',
        'window': {'width': 400, 'height': 720}, 'config': {'mode': 'bee'},
        'settings': [],
    })]
