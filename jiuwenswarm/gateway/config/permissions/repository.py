# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permissions_config 领域：整段 ``/permissions``。"""

from __future__ import annotations

import uuid
from typing import Any

from jiuwenswarm.gateway.config.section import (
    SectionDocument,
    SectionDocumentRepository,
    YamlSectionCodec,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

PERMISSIONS_CONFIG_STORE_NAME = "permissions_config"

_VALID_PERM_LEVEL = frozenset({"allow", "ask", "deny"})
_VALID_RULE_SEVERITY = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_RULE_MUTABLE_KEYS = frozenset(
    {"tools", "pattern", "severity", "action", "description", "match_type"}
)


def _validate_tools_map(tools: Any) -> dict[str, str]:
    if not isinstance(tools, dict):
        raise ValueError("tools must be an object")
    out: dict[str, str] = {}
    for key, value in tools.items():
        name = str(key).strip()
        if not name:
            raise ValueError("tool name must be non-empty")
        if isinstance(value, dict) and isinstance(value.get("*"), str):
            level = str(value["*"]).strip().lower()
        elif isinstance(value, str):
            level = value.strip().lower()
        else:
            raise ValueError(
                f"tools[{name!r}]: value must be allow|ask|deny or object {{'*': level}}"
            )
        if level not in _VALID_PERM_LEVEL:
            raise ValueError(f"tools[{name!r}]: invalid level {level!r}")
        out[name] = level
    return out


def _normalize_rule_tools(raw: Any) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        return [
            str(item).strip()
            for item in raw
            if isinstance(item, str) and str(item).strip()
        ]
    raise ValueError("tools must be a string or array of strings")


def _normalize_rule_severity_action(rule: dict[str, Any]) -> None:
    if "severity" in rule:
        severity = str(rule["severity"]).strip().upper()
        if severity not in _VALID_RULE_SEVERITY:
            raise ValueError(f"invalid severity {severity!r}")
        rule["severity"] = severity
    if "action" in rule:
        action = str(rule["action"]).strip().lower()
        if action not in _VALID_PERM_LEVEL:
            raise ValueError(f"invalid action {action!r}")
        rule["action"] = action


class PermissionsConfigRepository:
    """``permissions_config`` 读写；不判断 edition。"""

    def __init__(
        self,
        store: PersistentStore,
        codec: YamlSectionCodec,
        *,
        instance_id: str = "",
    ) -> None:
        self._inner = SectionDocumentRepository(
            store,
            codec,
            PERMISSIONS_CONFIG_STORE_NAME,
            instance_id=instance_id,
        )

    async def get(self) -> SectionDocument | None:
        return await self._inner.get()

    async def get_body(self) -> dict[str, Any]:
        return await self._inner.get_body()

    async def upsert_body(
        self,
        body: dict[str, Any],
        *,
        source: str = "manager",
    ) -> SectionDocument:
        """企业配置 upsert：整段替换 body 并递增 revision。"""
        existing = await self.get()
        document = SectionDocument(
            body=dict(body),
            source=source,
            revision=(int(existing.revision) + 1 if existing else 1),
        )
        return await self._inner.upsert(document)

    async def replace(self, body: dict[str, Any]) -> SectionDocument:
        return await self._inner.replace(body)

    async def merge(self, updates: dict[str, Any]) -> SectionDocument:
        return await self._inner.merge(updates)

    async def mutate(self, mutate_fn) -> SectionDocument:
        return await self._inner.mutate(mutate_fn)

    async def delete(self) -> bool:
        return await self._inner.delete()

    async def set_enabled(self, value: bool) -> SectionDocument:
        return await self.merge({"enabled": bool(value)})

    async def set_file_guard_workspace_rw_enabled(self, value: bool) -> SectionDocument:
        def _mutate(body: dict[str, Any]) -> None:
            fg = body.get("file_guard")
            if not isinstance(fg, dict):
                fg = {}
                body["file_guard"] = fg
            ws = fg.get("workspace")
            if not isinstance(ws, dict):
                ws = {}
                fg["workspace"] = ws
            ws["rw_enabled"] = bool(value)

        return await self.mutate(_mutate)

    async def set_owner_scopes(
        self,
        owner_scopes: Any,
        deny_guidance_message: str | None = None,
    ) -> SectionDocument:
        def _mutate(body: dict[str, Any]) -> None:
            body["owner_scopes"] = owner_scopes
            if deny_guidance_message is not None:
                body["deny_guidance_message"] = deny_guidance_message

        return await self.mutate(_mutate)

    async def set_deny_guidance(self, message: str) -> SectionDocument:
        return await self.merge({"deny_guidance_message": message})

    async def replace_tools(self, tools: Any) -> SectionDocument:
        normalized = _validate_tools_map(tools)
        return await self.merge({"tools": normalized})

    async def update_tool(self, tool_name: str, level: Any) -> dict[str, Any]:
        name = str(tool_name).strip()
        if not name:
            raise ValueError("tool name must be non-empty")
        piece = _validate_tools_map({name: level})
        result: dict[str, str] = {}

        def _mutate(body: dict[str, Any]) -> None:
            existing = body.get("tools")
            if not isinstance(existing, dict):
                existing = {}
            merged = {str(key): value for key, value in existing.items()}
            merged[name] = piece[name]
            body["tools"] = merged
            result.clear()
            result.update(merged)

        await self.mutate(_mutate)
        return {"tools": dict(result)}

    async def delete_tool(self, tool_name: str) -> bool:
        name = str(tool_name).strip()
        if not name:
            raise ValueError("tool name must be non-empty")
        found = {"value": False}

        def _mutate(body: dict[str, Any]) -> None:
            tools = body.get("tools")
            if not isinstance(tools, dict):
                return
            key_to_drop = None
            for key in tools:
                if str(key).strip() == name:
                    key_to_drop = key
                    break
            if key_to_drop is None:
                return
            body["tools"] = {
                key: value for key, value in tools.items() if key != key_to_drop
            }
            found["value"] = True

        await self.mutate(_mutate)
        return bool(found["value"])

    async def create_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(rule, dict):
            raise ValueError("rule must be an object")
        rid = str(rule.get("id") or "").strip() or f"ui_rule_{uuid.uuid4().hex[:12]}"
        stored: dict[str, Any] = {"id": rid}
        for key in _RULE_MUTABLE_KEYS:
            if key in rule and rule[key] is not None:
                stored[key] = rule[key]
        if "tools" not in stored or "pattern" not in stored:
            raise ValueError("tools and pattern are required")
        stored["tools"] = _normalize_rule_tools(stored["tools"])
        stored["pattern"] = str(stored["pattern"]).strip()
        if not stored["tools"]:
            raise ValueError("tools must be a non-empty list")
        if not stored["pattern"]:
            raise ValueError("pattern must be non-empty")
        _normalize_rule_severity_action(stored)

        def _mutate(body: dict[str, Any]) -> None:
            rules = body.get("rules")
            if not isinstance(rules, list):
                rules = []
            if any(
                isinstance(item, dict) and str(item.get("id") or "").strip() == rid
                for item in rules
            ):
                raise ValueError(f"rule id already exists: {rid}")
            rules.append(stored)
            body["rules"] = rules

        await self.mutate(_mutate)
        return stored

    async def update_rule(
        self, rule_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        rid = str(rule_id or "").strip()
        if not rid:
            raise ValueError("id is required")
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")

        merged_result: dict[str, Any] = {}

        def _mutate(body: dict[str, Any]) -> None:
            rules = body.get("rules")
            if not isinstance(rules, list):
                rules = []
            idx: int | None = None
            for i, item in enumerate(rules):
                if isinstance(item, dict) and str(item.get("id") or "").strip() == rid:
                    idx = i
                    break
            if idx is None:
                raise ValueError(f"rule not found: {rid}")

            merged: dict[str, Any] = dict(rules[idx])
            for key, value in patch.items():
                if key == "id":
                    continue
                if key not in _RULE_MUTABLE_KEYS:
                    continue
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            merged["id"] = rid
            if "tools" in merged:
                merged["tools"] = _normalize_rule_tools(merged["tools"])
            if "pattern" in merged:
                merged["pattern"] = str(merged["pattern"]).strip()
            if not merged.get("tools"):
                raise ValueError("tools must be a non-empty list")
            if not merged.get("pattern"):
                raise ValueError("pattern must be non-empty")
            _normalize_rule_severity_action(merged)
            rules[idx] = merged
            body["rules"] = rules
            merged_result.clear()
            merged_result.update(merged)

        await self.mutate(_mutate)
        return merged_result

    async def delete_rule(self, rule_id: str) -> bool:
        rid = str(rule_id or "").strip()
        if not rid:
            raise ValueError("id is required")
        found = {"value": False}

        def _mutate(body: dict[str, Any]) -> None:
            rules = body.get("rules")
            if not isinstance(rules, list):
                return
            new_rules = []
            for item in rules:
                if isinstance(item, dict) and str(item.get("id") or "").strip() == rid:
                    continue
                new_rules.append(item)
            if len(new_rules) == len(rules):
                return
            body["rules"] = new_rules
            found["value"] = True

        await self.mutate(_mutate)
        return bool(found["value"])

    async def delete_approval_override(self, override_id: str) -> bool:
        oid = str(override_id or "").strip()
        if not oid:
            raise ValueError("id is required")
        found = {"value": False}

        def _mutate(body: dict[str, Any]) -> None:
            overrides = body.get("approval_overrides")
            if not isinstance(overrides, list):
                return
            new_overrides = []
            for item in overrides:
                if isinstance(item, dict) and str(item.get("id") or "").strip() == oid:
                    continue
                new_overrides.append(item)
            if len(new_overrides) == len(overrides):
                return
            body["approval_overrides"] = new_overrides
            found["value"] = True

        await self.mutate(_mutate)
        return bool(found["value"])


__all__ = [
    "PERMISSIONS_CONFIG_STORE_NAME",
    "PermissionsConfigRepository",
]
