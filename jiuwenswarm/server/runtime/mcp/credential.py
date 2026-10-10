# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP credential layer.

The complexity of MCPs is the **credential acquisition strategy**, not the
transport. Three kinds exist:

  * none        — free-connect remote MCP (notion/supabase/canva...); no-op.
  * token       — static token the user supplies; injected into env/headers/url
                  ``${VAR}`` placeholders (tianyancha/gildata/gmail/jira...).
  * cli_oauth   — the CLI binary manages its own OAuth (feishu/dingtalk...);
                  delegated to ``CliDriver`` (form C).

This module provides the shared primitives the ``token`` strategy builds on:
placeholder detection, a persistent ``CredentialStore``, and
``resolve_placeholders``. The actual ``CredentialProvider`` abstraction (one
subclass per kind) wraps these; see ``connect_mcp`` for the dispatch point.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_workspace_dir

from jiuwenswarm.server.runtime.mcp.paths import load_json

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")
_load_json = load_json


def _mcp_root() -> Path:
    return get_workspace_dir() / "mcp"


def _packages_dir() -> Path:
    return _mcp_root() / "mcp_builtins"


def _pkg_dir(name: str) -> Path:
    return _packages_dir() / str(name or "").strip()


def _load_mcp_cfg(name: str) -> dict[str, Any] | None:
    """Read marketplace mcp.json, return the first server cfg."""
    raw = _load_json(_pkg_dir(name) / "mcp.json")
    if not raw or not isinstance(raw.get("mcpServers"), dict):
        return None
    first = next(iter(raw["mcpServers"].values()), {})
    return first if isinstance(first, dict) else None

# credential kinds (see module docstring)
KIND_NONE = "none"
KIND_TOKEN = "token"
KIND_CLI_OAUTH = "cli_oauth"


def extract_placeholders(mcp_cfg: dict[str, Any] | None) -> set[str]:
    """Collect all ``${VAR}`` placeholder names from a server cfg's
    env/headers/url (and any nested values).

    Returns the set of bare variable names (e.g. ``{"TIANYANCHA_API_KEY"}``).
    Used by :func:`detect_credential_kind` to decide ``token`` vs ``none``.
    """
    found: set[str] = set()
    if not mcp_cfg:
        return found
    for key in ("env", "headers", "staticEnv"):
        val = mcp_cfg.get(key)
        if isinstance(val, dict):
            for v in val.values():
                if isinstance(v, str):
                    found.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(v))
    url = mcp_cfg.get("url")
    if isinstance(url, str):
        found.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(url))
    args = mcp_cfg.get("args")
    if isinstance(args, list):
        for a in args:
            if isinstance(a, str):
                found.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(a))
    return found


def load_token_schema(name: str) -> dict[str, Any] | None:
    """Read an MCP's ``token-schema.json`` (skill-only MCPs).

    Used by MCPs that have no mcp.json (e.g. ctrip-wendao, netease-mail): they
    declare required API tokens via a token-schema.json instead of ``${VAR}``
    placeholders in mcp.json. Returns the parsed schema
    ({title, docUrl, fields:[{key,label,type,required,...}]}) or None.
    """
    raw = load_json(_pkg_dir(name) / "token-schema.json")
    if not raw or not isinstance(raw.get("fields"), list):
        return None
    return raw


def required_tokens_from_schema(name: str) -> list[str]:
    """Extract required field keys from token-schema.json (in declaration order)."""
    schema = load_token_schema(name)
    if not schema:
        return []
    return [str(f["key"]) for f in schema["fields"]
            if isinstance(f, dict) and f.get("required") and f.get("key")]


def build_credentials_prompt(name: str, missing_tokens: list[str]) -> dict[str, Any]:
    """Build the ``credentials_required`` payload for the frontend.

    Reads token-schema.json (when present) and attaches the doc/apply link
    (``docUrl`` + ``docLabel``) plus per-field render hints (label, placeholder,
    input type) so the credentials modal can show a "go apply for a token" link
    and field-level guidance instead of bare env-var keys.

    Falls back to a minimal payload (only required_tokens + credential_kind)
    when the MCP has no schema — form-B MCPs whose placeholders come from
    mcp.json without a schema still work, the modal just renders bare keys.
    """
    n = str(name or "").strip()
    base: dict[str, Any] = {
        "credentials_required": True,
        "required_tokens": list(missing_tokens),
        "credential_kind": KIND_TOKEN,
    }
    schema = load_token_schema(n)
    if not schema:
        return base
    if schema.get("title"):
        base["title"] = schema["title"]
    if schema.get("description"):
        base["description"] = schema["description"]
    if schema.get("docUrl"):
        base["doc_url"] = schema["docUrl"]
    if schema.get("docLabel"):
        base["doc_label"] = schema["docLabel"]
    # Per-field render hints for the missing tokens only (already-provisioned
    # fields don't need an input). Keyed by field key for frontend lookup.
    fields_by_key: dict[str, dict[str, Any]] = {}
    for f in schema.get("fields", []):
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if not key or key not in missing_tokens:
            continue
        fields_by_key[key] = {
            k: f[k] for k in ("label", "placeholder", "type", "description")
            if f.get(k)
        }
    if fields_by_key:
        base["fields"] = fields_by_key
    return base


def detect_credential_kind(name: str) -> str:
    """Classify an MCP's credential acquisition strategy from its package.

    * has cli.json -> ``cli_oauth`` (CLI manages OAuth; form C).
    * mcp.json declares any ``${VAR}`` placeholder -> ``token`` (form B).
    * has token-schema.json with required fields -> ``token`` (form B variant:
      skill-only MCPs like ctrip-wendao that have no mcp.json).
    * otherwise -> ``none`` (form A free-connect).
    """
    n = str(name or "").strip()
    if not n:
        return KIND_NONE
    if (_pkg_dir(n) / "cli.json").is_file():
        return KIND_CLI_OAUTH
    if extract_placeholders(_load_mcp_cfg(n)):
        return KIND_TOKEN
    if required_tokens_from_schema(n):
        return KIND_TOKEN
    return KIND_NONE


# ---------------------------------------------------------------------------
# CredentialStore — persistent token storage (plaintext JSON; the public API
# is stable so an aes-256-gcm + hkdf-sha256 backend can be swapped in later).
# ---------------------------------------------------------------------------


class CredentialStore:
    """File-backed per-MCP token storage.

    Layout: ``<workspace>/mcp/credentials/<name>.json`` holding
    ``{key: value}``. One file per MCP makes delete trivial and keeps
    concurrent MCPs from clobbering each other.
    """

    def __init__(self, *, workspace_dir: Path | None = None) -> None:
        self._workspace = workspace_dir if workspace_dir is not None else get_workspace_dir()
        self._root = self._workspace / "mcp" / "credentials"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self._root / f"{str(name or '').strip()}.json"

    def _load(self, name: str) -> dict[str, str]:
        p = self._path(name)
        if not p.is_file():
            return {}
        try:
            # utf-8-sig tolerates a leading BOM (EF BB BF) that legacy tooling
            # or PowerShell ``Set-Content -Encoding utf8`` may have written.
            with p.open("r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, name: str, data: dict[str, str]) -> None:
        """原子写入 + Unix 权限收紧到 0600。

        凭证文件存明文 token，写到一半崩溃会留下截断文件（下次读取 token
        丢失，需重输）。用 tempfile + os.replace 保证原子性：要么完整新值，
        要么保留旧值。Unix 下额外 chmod 600 防止多用户机其他账号读取；
        Windows 无 stat 权限语义（NTFS ACL 不走 mode），原子写仍生效。
        """
        p = self._path(name)
        try:
            # dir= 同目录保证 os.replace 是原子的 rename，不跨文件系统。
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(p.parent), suffix=".tmp",
                delete=False,
            ) as fh:
                json.dump(data, fh, ensure_ascii=False)
                tmp_path = Path(fh.name)
            # Unix 收紧权限（仅在非 Windows 且 mode 可写时）；Windows 跳过。
            if os.name != "nt":
                try:
                    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
                except OSError as chmod_exc:  # noqa: BLE001
                    logger.debug(
                        "[mcp.credential] chmod 0600 %s failed: %s",
                        tmp_path, chmod_exc,
                    )
            os.replace(tmp_path, p)
        except OSError as exc:  # noqa: BLE001
            logger.warning("[mcp.credential] failed to write %s: %s", p, exc)
            # 失败时清理残留临时文件，避免 .tmp 文件堆积。
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # noqa: BLE001
                pass

    def save_token(self, name: str, key: str, value: str) -> None:
        data = self._load(name)
        data[str(key)] = str(value)
        self._save(name, data)

    def get_token(self, name: str, key: str) -> str | None:
        return self._load(name).get(str(key))

    def get_all(self, name: str) -> dict[str, str]:
        return dict(self._load(name))

    def delete_mcp(self, name: str) -> None:
        p = self._path(name)
        if p.is_file():
            try:
                p.unlink()
            except OSError as exc:  # noqa: BLE001
                logger.warning("[mcp.credential] failed to delete %s: %s", p, exc)


# ---------------------------------------------------------------------------
# resolve_placeholders — substitute ${VAR} in an entry from the store.
# Handles env/headers/url/args and any other string fields. Missing tokens
# are left as-is (the caller decides whether to error or proceed).
# ---------------------------------------------------------------------------


def _substitute(value: str, tokens: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        return tokens.get(m.group(1), m.group(0))

    return _PLACEHOLDER_RE.sub(repl, value)


def _resolve_node(node: Any, tokens: dict[str, str]) -> Any:
    if isinstance(node, str):
        return _substitute(node, tokens)
    if isinstance(node, dict):
        return {k: _resolve_node(v, tokens) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_node(v, tokens) for v in node]
    return node


def resolve_placeholders(entry: dict[str, Any], store: CredentialStore, name: str | None = None) -> dict[str, Any]:
    """Return a copy of ``entry`` with all ``${VAR}`` replaced from the store.

    ``name`` (the MCP name) selects which stored tokens apply; defaults to
    ``entry["name"]``. Missing tokens leave the placeholder literal so callers
    can detect un-provisioned MCPs.
    """
    n = name or str(entry.get("name", "")).strip()
    tokens = store.get_all(n) if n else {}
    return _resolve_node(entry, tokens)  # type: ignore[return-value]


__all__ = [
    "CredentialStore",
    "detect_credential_kind",
    "extract_placeholders",
    "load_token_schema",
    "required_tokens_from_schema",
    "build_credentials_prompt",
    "resolve_placeholders",
    "KIND_NONE",
    "KIND_TOKEN",
    "KIND_CLI_OAUTH",
]
