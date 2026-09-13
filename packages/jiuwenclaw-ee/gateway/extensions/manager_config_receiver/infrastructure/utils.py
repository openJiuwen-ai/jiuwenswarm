# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Gateway 扩展基础设施工具函数。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def is_running_in_k8s() -> bool:
    """判断当前进程是否运行在 K8s Pod 内。"""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    return os.path.isfile(_SA_TOKEN_FILE)


def get_k8s_namespace() -> str | None:
    """读取当前 Pod 所在 K8s 命名空间（``NAMESPACE`` env 或 SA 挂载文件）。"""
    val = os.getenv("NAMESPACE", "").strip()
    if val:
        return val
    try:
        with open(_SA_NAMESPACE_FILE, encoding="utf-8") as f:
            text = f.read().strip()
            return text or None
    except OSError:
        return None


def get_pod_name() -> str | None:
    """读取当前 Pod 名称（K8s 会将 ``HOSTNAME`` 设为 Pod 名）。"""
    val = os.getenv("HOSTNAME", "").strip()
    return val or None


def get_gateway_register_identity() -> dict[str, str]:
    """采集 Gateway 运行时身份（命名空间、Pod 名）。"""
    if not is_running_in_k8s():
        return {}
    out: dict[str, str] = {}
    ns = get_k8s_namespace()
    if ns:
        out["k8s_namespace"] = ns
    pod = get_pod_name()
    if pod:
        out["jiuwenclaw_name"] = pod
    return out


def utc_now() -> datetime:
    """返回当前 UTC 时间（带 ``timezone.utc`` 的 aware ``datetime``）。"""
    return datetime.now(timezone.utc)


def format_ts(val: Any) -> str:
    """将数据库/ORM 时间值格式化为带时区偏移的 ISO 8601 字符串。"""
    if val is None:
        return ""
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(val)


def parse_iso_datetime(value: Any) -> Any:
    """将 ISO 8601 字符串解析为 ``datetime``；已是 ``datetime`` 或空值则原样返回。"""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    return value


def normalize_template_ref(value: Any) -> dict[str, list[str]]:
    """将 ``template_ref`` 规范为 ``{slot: [ref_string, ...]}``；空值键省略，同槽位去重保序。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("template_ref must be a JSON object")
    out: dict[str, list[str]] = {}
    for key, raw in value.items():
        slot = str(key).strip()
        if not slot or raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"template_ref[{slot!r}] must be a list")
        refs: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            refs.append(text)
        if refs:
            out[slot] = refs
    return out


def merge_template_ref(*layers: dict[str, list[str]]) -> dict[str, list[str]]:
    """按参数顺序从左到右合并 ``template_ref``；后出现的槽位整组覆盖先前的同名槽位。"""
    merged: dict[str, list[str]] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def fill_missing_template_ref_slots(
    merged: dict[str, list[str]],
    fallback: dict[str, list[str]],
) -> dict[str, list[str]]:
    """将 ``fallback`` 中尚未出现在 ``merged`` 的槽位补入（用于全局兜底按槽位回填）。"""
    if not fallback:
        return merged
    out = dict(merged)
    for slot, refs in fallback.items():
        if slot not in out:
            out[slot] = refs
    return out


def read_template_ref_from_row(row: Any) -> dict[str, list[str]]:
    raw = getattr(row, "template_ref", None)
    if isinstance(raw, dict):
        return normalize_template_ref(raw)
    return {}


def apply_template_ref_to_updates(
    updates: dict[str, Any],
    *,
    existing_row: Any | None,
) -> dict[str, Any]:
    """PATCH 含 ``template_ref`` 时整列替换（与 Manager / 前端编辑器一致）。"""
    payload = dict(updates)
    if "template_ref" not in payload:
        return payload
    patch = payload.pop("template_ref")
    payload["template_ref"] = normalize_template_ref(patch)
    return payload
