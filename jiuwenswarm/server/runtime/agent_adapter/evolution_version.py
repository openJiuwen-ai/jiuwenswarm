# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side Skill evolution version control (rollback / rebuild orchestration).

Archive SemVer bump, changelog, and paired rollback semantics live in openjiuwen.
This module only wires disk stores, path checks, and rebuild lifecycle for the host.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Sequence

from openjiuwen.agent_evolving.checkpointing.evolution_store import EvolutionStore
from openjiuwen.agent_evolving.experience.archive import EvolutionArchiveService
from openjiuwen.agent_evolving.experience.rebuild import ExperienceRebuildService
from openjiuwen.harness.rails.evolution.commands import build_rebuild_command_prompt

from jiuwenswarm.common.utils import (
    get_shared_agent_skills_dirs,
    resolve_agent_registered_skill_dirs,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    validate_evolution_skill,
)
from jiuwenswarm.server.runtime.skill import filter_visible_skill_names

logger = logging.getLogger(__name__)

_SEMVER_BODY_RE = re.compile(r"^SKILL\.v\d+\.\d+\.\d+(?:_[0-9]+)?\.md$")


def safe_path_name(value: Any, label: str = "skill") -> str:
    """Reject path traversal / empty names for evolution RPC params."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"invalid {label} name")
    path_value = Path(raw)
    has_path_sep = "/" in raw or "\\" in raw
    is_traversal = raw in (".", "..") or path_value.is_absolute() or bool(path_value.drive)
    if has_path_sep or is_traversal:
        raise ValueError(f"invalid {label} name: {raw}")
    return raw


def skills_root_from_skill_md_path(skill_path: str | None) -> str | None:
    """Return ``…/skills`` root for a control-plane ``…/<name>/SKILL.md`` path."""
    if skill_path is None or not str(skill_path).strip():
        return None
    try:
        resolved = Path(str(skill_path).strip()).expanduser().resolve()
    except OSError:
        return None
    if resolved.name != "SKILL.md":
        return None
    return str(resolved.parent.parent)


def extra_trusted_dirs_for_skill_md(skill_md_path: str | None) -> list[str]:
    """Skill-dir + ``…/skills`` root for rebuild ``trusted_dirs`` (workspace-outside)."""
    extra: list[str] = []
    root = skills_root_from_skill_md_path(skill_md_path)
    if root:
        extra.append(root)
    if skill_md_path is None or not str(skill_md_path).strip():
        return extra
    try:
        resolved = Path(str(skill_md_path).strip()).expanduser().resolve()
    except OSError:
        return extra
    parent = str(resolved.parent)
    if parent and parent not in extra:
        extra.append(parent)
    return extra


def disk_only_evolution_skill_dirs(params: dict[str, Any] | None = None) -> list[str]:
    """Skill roots for disk-only evolution: shared env + explicit skill_path root."""
    params = params if isinstance(params, dict) else {}
    roots: list[str] = []
    seen: set[str] = set()
    for path in get_shared_agent_skills_dirs():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(key)
    skill_root = skills_root_from_skill_md_path(
        params.get("skill_path") or params.get("path")
    )
    if skill_root and skill_root not in seen:
        roots.append(skill_root)
    if roots:
        return roots
    return [str(p) for p in resolve_agent_registered_skill_dirs()]


def get_disk_evolution_store(skills_dirs: str | list[str] | None = None) -> EvolutionStore:
    """Build a disk-only EvolutionStore (no EvolutionRail / LLM)."""
    if skills_dirs is None:
        skills_dirs = [str(p) for p in resolve_agent_registered_skill_dirs()]
    return EvolutionStore(skills_dirs)


def resolve_subject(store: EvolutionStore, skill_name: str) -> dict[str, str]:
    resolver = getattr(store, "resolve_subject_payload", None)
    if callable(resolver):
        try:
            payload = resolver(skill_name)
        except Exception:
            logger.debug("[EvolutionVersion] could not resolve subject for '%s'", skill_name)
        else:
            if isinstance(payload, dict):
                kind = str(payload.get("kind") or "").strip()
                name = str(payload.get("name") or skill_name).strip() or skill_name
                if kind:
                    return {"kind": kind, "name": name}
    return {"kind": "skill", "name": skill_name}


def list_body_archive_versions(
    store: EvolutionStore,
    skill_name: str,
    *,
    subject_kind: str | None = None,
) -> list[str]:
    """List paired SemVer body archive filenames (newest-first)."""
    archive_service = EvolutionArchiveService(store=store)
    pairs = archive_service.list_pairs(skill_name, subject_kind=subject_kind)
    return [pair.skill_archive_name for pair in pairs]


def skill_md_fingerprint(skill_md_path: str | None) -> str | None:
    """Return sha256 of SKILL.md bytes, or None when unreadable."""
    if not skill_md_path:
        return None
    path = Path(str(skill_md_path))
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def validate_rebuild_skill_path(
    skill_path: str,
    *,
    skill_name: str,
) -> str:
    """Normalize skill_path; require SKILL.md and matching skill directory name."""
    try:
        resolved = Path(str(skill_path).strip()).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"invalid skill_path: {skill_path}") from exc
    if resolved.name != "SKILL.md":
        raise ValueError(f"skill_path must end with SKILL.md: resolve_path={resolved}")
    if resolved.parent.name != skill_name:
        raise ValueError(
            f"skill_path directory name must match skill name "
            f"(expected={skill_name}, resolve_path={resolved})"
        )
    return str(resolved)


def normalize_record_ids(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw if str(item).strip()]
        return values or None
    text = str(raw).strip()
    if not text:
        return None
    parts = [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]
    return parts or None


async def do_evolve_rollback(
    store: EvolutionStore,
    skill_name: str,
    version: str | None,
    *,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """Shared rollback used by slash and skills.evolution.rollback RPC.

    Returns:
        - ``{ok, rolled_back: False, name, versions}`` when version omitted (list)
        - ``{ok, rolled_back: True, name, version}`` on success
        - ``{ok: False, error}`` on failure
    """
    import asyncio

    if not store.skill_exists(skill_name):
        available = (
            "、".join(filter_visible_skill_names(store.list_skill_names()))
            or "（无可用 Skill）"
        )
        return {"ok": False, "error": f"未找到 Skill '{skill_name}'。当前可用：{available}"}

    subject = resolve_subject(store, skill_name)
    subject_kind = str(subject.get("kind") or "skill")
    validation_error = validate_evolution_skill(
        store, skill_name, require_skill_md=False
    )
    if validation_error is not None:
        return {"ok": False, "error": validation_error}

    archive_service = EvolutionArchiveService(store=store)
    pairs = archive_service.list_pairs(skill_name, subject_kind=subject_kind)
    if not pairs:
        return {"ok": False, "error": f"Skill '{skill_name}' 没有成对归档版本可回滚。"}

    body_versions = [pair.skill_archive_name for pair in pairs]
    short_versions = [pair.version for pair in pairs]

    if not version:
        return {
            "ok": True,
            "rolled_back": False,
            "name": skill_name,
            "versions": body_versions,
            "short_versions": short_versions,
            "pairs": pairs,
        }

    requested = archive_service.normalize_version(version)
    if requested is None:
        # Accept bare SKILL.vX.Y.Z.md filenames as well as short SemVer tokens.
        normalized_body = EvolutionStore.normalize_body_archive_name(version)
        if normalized_body and normalized_body in body_versions:
            requested = archive_service.normalize_version(
                normalized_body.replace("SKILL.", "").replace(".md", "")
            ) or normalized_body
        else:
            return {
                "ok": False,
                "error": (
                    f"版本 `{version}` 格式无效，请使用短版本号，"
                    f"例如 `{pairs[0].version}`。"
                ),
            }

    if requested == "latest":
        pair = pairs[0]
    else:
        pair = next((item for item in pairs if item.version == requested), None)
        if pair is None:
            # Match full body archive filename
            pair = None
            for item in pairs:
                if item.skill_archive_name in (version, requested):
                    pair = item
                    break
    if pair is None:
        hint = "、".join(f"`{v}`" for v in short_versions[:5])
        return {"ok": False, "error": f"版本 `{version}` 不存在或归档不成对。可用版本：{hint}"}

    try:
        restored = await asyncio.wait_for(
            archive_service.rollback_to_pair(
                skill_name,
                pair,
                subject_kind=subject_kind,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[EvolutionVersion] evolve_rollback timed out for skill=%s version=%s "
            "(filesystem may be partially updated)",
            skill_name,
            pair.version,
        )
        return {
            "ok": False,
            "error": (
                "回滚操作超时，文件系统可能处于部分完成状态"
                "（body / evolution log / 归档可能不一致）。"
                f"请检查 Skill '{skill_name}' 的 archive 目录后再重试。"
            ),
        }
    except Exception as exc:
        logger.warning("[EvolutionVersion] evolve_rollback failed: %s", exc)
        return {"ok": False, "error": f"回滚失败：{exc}"}

    if not restored:
        return {
            "ok": False,
            "error": f"回滚失败：无法将 Skill '{skill_name}' 回滚到 `{pair.version}`。",
        }
    return {
        "ok": True,
        "rolled_back": True,
        "name": skill_name,
        "version": pair.version,
        "body_version": pair.skill_archive_name,
    }


def make_rebuild_service(
    store: EvolutionStore,
    *,
    llm: Any = None,
    model: str | None = None,
    language: str = "cn",
) -> ExperienceRebuildService:
    """Build ExperienceRebuildService with LLM for changelog classification."""
    return ExperienceRebuildService(
        store=store,
        llm=llm,
        model=model,
        language=language,
    )


async def prepare_rebuild_followup(
    store: EvolutionStore,
    skill_name: str,
    *,
    user_intent: str | None = None,
    record_ids: Sequence[str] | None = None,
    min_score: float = 0.5,
    language: str = "cn",
    llm: Any = None,
    model: str | None = None,
    skill_md_path: str | None = None,
) -> dict[str, Any]:
    """Prepare rebuild context + followup prompt (does not clear live evolutions)."""
    subject = resolve_subject(store, skill_name)
    validation_error = validate_evolution_skill(
        store, skill_name, require_skill_md=False
    )
    if validation_error is not None:
        return {"ok": False, "error": validation_error, "result_type": "error"}

    rebuild_service = make_rebuild_service(
        store, llm=llm, model=model, language=language
    )
    try:
        rebuild_context = await rebuild_service.prepare_rebuild_context(
            subject,
            user_intent=user_intent,
            min_score=min_score,
            record_ids=record_ids,
        )
    except Exception as exc:
        logger.warning("[EvolutionVersion] prepare_rebuild failed: %s", exc)
        return {"ok": False, "error": f"重建失败：{exc}", "result_type": "error"}

    if rebuild_context is None:
        return {
            "ok": False,
            "error": f"Skill '{skill_name}' 未生成可执行的重建指令。",
            "result_type": "error",
        }

    archive_error = rebuild_context.get("archive_error")
    if archive_error is not None:
        return {
            "ok": False,
            "error": f"重建失败：无法归档 Skill '{skill_name}' 的旧版本：{archive_error}",
            "result_type": "error",
        }
    if not rebuild_context.get("archive_pair"):
        return {
            "ok": False,
            "error": f"重建失败：无法归档 Skill '{skill_name}' 的旧版本。",
            "result_type": "error",
        }

    if skill_md_path:
        rebuild_context["skill_md_path"] = skill_md_path
    else:
        skill_dir = store.resolve_skill_dir(skill_name, subject_kind=subject.get("kind"))
        if skill_dir is not None:
            skill_md = store.find_skill_md(skill_dir) or (skill_dir / "SKILL.md")
            rebuild_context["skill_md_path"] = str(skill_md)

    prompt = build_rebuild_command_prompt(
        subject=subject,
        user_intent=user_intent,
        rebuild_context=rebuild_context,
        language=language,
    )
    return {
        "ok": True,
        "result_type": "followup",
        "action": "run_rebuild_followup",
        "followup_prompt": prompt,
        "skill_name": skill_name,
        "rebuild_context": rebuild_context,
    }


async def finalize_rebuild_followup(
    store: EvolutionStore,
    rebuild_context: dict[str, Any],
    *,
    llm: Any = None,
    model: str | None = None,
    language: str = "cn",
) -> dict[str, Any]:
    """Bump SemVer, append changelog, clear live evolutions after successful rewrite."""
    rebuild_service = make_rebuild_service(
        store, llm=llm, model=model, language=language
    )
    try:
        cleared = await rebuild_service.complete_rebuild(rebuild_context)
    except Exception as exc:
        logger.warning("[EvolutionVersion] complete_rebuild failed: %s", exc)
        return {"ok": False, "cleared": False, "error": str(exc)}

    skill_name = str(rebuild_context.get("skill_name") or "").strip()
    new_version = None
    if skill_name:
        try:
            new_version = await store.resolve_current_version(
                skill_name,
                subject_kind=rebuild_context.get("subject_kind"),
            )
        except Exception:
            new_version = None
    return {
        "ok": bool(cleared),
        "cleared": bool(cleared),
        "name": skill_name,
        "new_version": new_version,
    }


def is_body_archive_name(archive_name: str) -> bool:
    return bool(_SEMVER_BODY_RE.match(str(archive_name or "").strip()))


__all__ = [
    "disk_only_evolution_skill_dirs",
    "do_evolve_rollback",
    "finalize_rebuild_followup",
    "get_disk_evolution_store",
    "is_body_archive_name",
    "list_body_archive_versions",
    "make_rebuild_service",
    "normalize_record_ids",
    "prepare_rebuild_followup",
    "resolve_subject",
    "safe_path_name",
    "skill_md_fingerprint",
    "skills_root_from_skill_md_path",
    "validate_rebuild_skill_path",
]
