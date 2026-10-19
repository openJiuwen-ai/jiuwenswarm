from __future__ import annotations

from typing import Any

from .inventory import SkillInventory


def render_disabled(language: str = "en") -> str:
    normalized = str(language or "").lower()
    if normalized.startswith(("zh", "cn")):
        return (
            "技能检索当前已关闭。\n\n"
            "可以继续使用 jiuwenswarm 原有流程；如需使用技能索引，请在设置中开启 Agentic 技能检索并重新加载配置。"
        )
    return (
        "Skill retrieval is currently disabled.\n\n"
        "Proceed with the original jiuwenswarm flow, or enable Agentic skill retrieval in settings and reload "
        "configuration."
    )


def render_build_success(
    *,
    reused: bool,
    inventory: SkillInventory,
    index_dir: str,
    elapsed_seconds: float,
) -> str:
    action = "reused existing index" if reused else "built skill retrieval index"
    return (
        "# Skill Retrieval Index\n\n"
        "## Build Summary\n"
        f"- Result: {action}\n"
        f"- Indexed skills: {inventory.count}\n"
        f"- Index directory: `{index_dir}`\n"
        f"- Inventory fingerprint: `{inventory.fingerprint[:12]}`\n"
        f"- Elapsed: {elapsed_seconds:.1f}s\n\n"
        "The index is ready. Use `skill_retrieve` with a task query to retrieve relevant installed skills."
    )


def render_build_failure(reason: str) -> str:
    return (
        "# Skill Retrieval Index Failed\n\n"
        f"{reason.strip() or 'Index build failed.'}\n\n"
        "Handling options:\n"
        "- Ignore this tool result and continue with the original jiuwenswarm flow.\n"
        "- Check LLM configuration and installed skills, then call `skill_index_build` again."
    )


def render_retrieve_failure(reason: str) -> str:
    return (
        "# Skill Retrieval Failed\n\n"
        f"{reason.strip() or 'Skill retrieval failed.'}\n\n"
        "Handling options:\n"
        "- Only call `skill_index_build` when the reason above explicitly says the index is missing or stale.\n"
        "- After `skill_index_build` succeeds, call `skill_retrieve` again with the same query.\n"
        "- Otherwise continue with the original jiuwenswarm flow without retrieved skill hints."
    )


def render_retrieve_success(
    *,
    query: str,
    index_dir: str,
    indexed_count: int,
    result: Any,
    catalog_by_worker: dict[str, dict[str, Any]],
    settings_summary: str,
) -> str:
    records = _candidate_records(result)
    lines = [
        "# Skill Retrieval Result",
        "",
        "## Retrieval Summary",
        f"- Query: {query}",
        f"- Index directory: `{index_dir}`",
        f"- Indexed skills: {indexed_count}",
        f"- Retrieved candidates: {len(records)}",
        f"- Retrieval settings: {settings_summary}",
    ]
    elapsed = getattr(result, "elapsed_ms", None)
    if elapsed is not None:
        lines.append(f"- Retrieval elapsed: {float(elapsed):.0f}ms")
    summary_lines = getattr(result, "summary_lines", None) or []
    if summary_lines:
        lines.extend(["", "## Retrieval Summary"])
        lines.extend(f"- {line}" for line in summary_lines[:10])
    lines.extend(["", "## Retrieved Skills"])
    if not records:
        lines.append("No relevant installed skills were returned by retrieval.")
    else:
        for index, record in enumerate(records, start=1):
            worker_id = str(record.get("worker_id") or record.get("resolved_payload") or "").strip()
            catalog = catalog_by_worker.get(worker_id, {})
            name = str(record.get("skill_name") or catalog.get("name") or worker_id or f"skill-{index}").strip()
            description = str(record.get("description") or catalog.get("description") or "").strip()
            path = str(catalog.get("skill_path") or "").strip()
            source = str(record.get("source") or "").strip()
            lines.append(f"{index}. `{name}`")
            if worker_id and worker_id != name:
                lines.append(f"   - Worker id: `{worker_id}`")
            if description:
                lines.append(f"   - Description: {_compact(description, 420)}")
            if path:
                lines.append(f"   - SKILL.md: `{path}`")
            if source:
                lines.append(f"   - Match source: {source}")
    lines.extend(
        [
            "",
            "## Usage Notes",
            "- Read the returned `SKILL.md` files before using a skill.",
            "- This result is a retrieval hint only; execute the task through jiuwenswarm's original runtime.",
            "- Do not treat the retrieved order as an execution plan.",
        ]
    )
    return "\n".join(lines)


def _candidate_records(result: Any) -> list[dict[str, Any]]:
    raw = getattr(result, "candidate_records", None) or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _compact(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:max(0, limit - 1)].rstrip() + "..."
