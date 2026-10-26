# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.web_search.constants import PAID_API_KEYS
from jiuwenswarm.agents.harness.common.tools.web_search.paid import diagnose_petal_search
from jiuwenswarm.agents.harness.common.tools.web_search.types import ProviderRun, WebSearchSettings
from jiuwenswarm.common.local_env_config import read_env_if_set


def truncate_query(query: str, limit: int = 120) -> str:
    text = (query or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def settings_summary(settings: WebSearchSettings) -> str:
    order = ",".join(settings.paid_provider_order)
    return (
        f"timeout={settings.timeout_seconds}s max_results={settings.max_results} "
        f"paid_order=[{order}]"
    )


def provider_run_summary(run: ProviderRun | None) -> str:
    if run is None:
        return "none"
    parts = [f"provider={run.provider}"]
    if run.error:
        parts.append(f"error={run.error}")
    parts.append(f"records={len(run.records)}")
    parts.append(f"quality_passed={run.quality_passed}")
    if run.quality_reason:
        parts.append(f"quality_reason={run.quality_reason}")
    if run.answer:
        parts.append(f"answer_len={len(run.answer)}")
    return " ".join(parts)


def paid_provider_skip_reason(name: str) -> str:
    if name == "petal":
        reason = diagnose_petal_search()
        return "available" if reason == "ok" else reason
    env_key = PAID_API_KEYS.get(name)
    if not env_key:
        return "unknown_provider"
    if read_env_if_set(env_key):
        return "available"
    return f"missing_{env_key}"


def paid_availability_report(order: tuple[str, ...]) -> str:
    items = [f"{name}={paid_provider_skip_reason(name)}" for name in order]
    return "; ".join(items)
