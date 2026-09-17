from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, TYPE_CHECKING

from shared.rich_compat import Console, Panel

from .prompts import GROUP_DISCOVERY_PROMPT, GROUP_MERGE_PROMPT, SKILL_ASSIGNMENT_PROMPT
from .schema import SKILL_DESCRIPTION_MAX_LENGTH

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()


def _builder_attr(builder: "TreeBuilder", name: str):
    return getattr(builder, name)


def _builder_call(builder: "TreeBuilder", name: str, *args, **kwargs):
    return getattr(builder, name)(*args, **kwargs)


class TreeGroupingEngine:
    """Owns discovery, assignment, recovery, and batching for tree grouping."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    @staticmethod
    def sorted_skills(skills: list[dict]) -> list[dict]:
        return sorted(skills, key=lambda item: (str(item.get("id", "")), str(item.get("name", ""))))

    @staticmethod
    def iter_group_items(groups: dict):
        ordered_ids = sorted(str(group_id) for group_id in groups.keys())
        return ((group_id, groups[group_id]) for group_id in ordered_ids)

    def sampling_seed(self, parent_context: Optional[dict], skills_count: int) -> int:
        builder = self._builder
        parent = parent_context or {}
        payload = "|".join(
            [
                str(_builder_attr(builder, "_discovery_seed")),
                str(parent.get("name", "")),
                str(parent.get("description", "")),
                str(skills_count),
            ]
        )
        return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8], 16)

    def format_skills_list(self, skills: list[dict]) -> str:
        rows: list[str] = []
        for entry in self.sorted_skills(skills):
            skill_id = str(entry.get("id", "")).strip()
            skill_name = str(entry.get("name", skill_id)).strip() or skill_id
            raw_description = str(entry.get("description", "") or "")
            description = raw_description[:SKILL_DESCRIPTION_MAX_LENGTH]
            if len(raw_description) > SKILL_DESCRIPTION_MAX_LENGTH:
                description = description.rstrip() + "..."
            rows.append(f"- {skill_id}: {skill_name}")
            if description:
                rows.append(f"  {description}")
        return "\n".join(rows)

    def build_groups_from_assignments(self, groups: dict, assignments: dict) -> dict:
        grouped_skill_ids: dict[str, list[str]] = {}
        for skill_id, group_id in assignments.items():
            grouped_skill_ids.setdefault(group_id, []).append(skill_id)

        result: dict[str, dict] = {}
        for group_id, payload in self.iter_group_items(groups):
            skill_ids = grouped_skill_ids.get(group_id, [])
            skill_ids = sorted(skill_ids)
            if not skill_ids:
                continue
            result[group_id] = {
                "name": payload.get("name", group_id),
                "description": payload.get("description", ""),
                "select_when": payload.get("select_when", ""),
                "dont_select_when": payload.get("dont_select_when", ""),
                "skill_ids": skill_ids,
            }
            children = payload.get("children")
            if children:
                result[group_id]["children"] = children
        return result

    def classify_skills(self, skills: list[dict], groups: dict, verbose: bool = False) -> dict:
        ordered = self.sorted_skills(skills)
        manager_config = _builder_attr(self._builder, "_manager_config")
        cfg_cap = int(getattr(manager_config.build, "classify_batch_cap", 20) or 20)
        batch_size = min(_builder_call(self._builder, "_auto_batch_size"), max(1, cfg_cap))
        if len(ordered) <= batch_size:
            return self.classify_skills_single(ordered, groups, verbose=verbose)
        return self.batched_classify_skills(ordered, groups, batch_size=batch_size, verbose=verbose)

    def classify_skills_single(
        self,
        skills: list[dict],
        groups: dict,
        verbose: bool = False,
        is_retry: bool = False,
    ) -> dict:
        del verbose
        group_lines = []
        for group_id, payload in self.iter_group_items(groups):
            group_lines.append(f"- {group_id}: {payload.get('name', group_id)}")
            description = str(payload.get("description", "") or "").strip()
            select_when = str(payload.get("select_when", "") or "").strip()
            dont_select_when = str(payload.get("dont_select_when", "") or "").strip()
            if description:
                group_lines.append(f"  Description: {description}")
            if select_when:
                group_lines.append(f"  Select when: {select_when}")
            if dont_select_when:
                group_lines.append(f"  Don't select when: {dont_select_when}")
        prompt = SKILL_ASSIGNMENT_PROMPT.format(
            groups_list="\n".join(group_lines),
            skills_list=self.format_skills_list(skills),
        )
        response = _builder_call(self._builder, "_call_llm_json", prompt, is_retry=is_retry)
        raw_assignments = response.get("assignments", {}) if isinstance(response, dict) else {}
        valid_groups = {str(group_id) for group_id in groups.keys()}
        valid_skill_ids = {str(item.get("id", "")).strip() for item in skills}

        cleaned: dict[str, str] = {}
        for skill_id, group_id in raw_assignments.items():
            skill_key = str(skill_id).strip()
            group_key = self._normalize_group_id(group_id, valid_groups)
            if skill_key in valid_skill_ids and group_key in valid_groups:
                cleaned[skill_key] = group_key
        return cleaned

    def batched_classify_skills(
        self,
        skills: list[dict],
        groups: dict,
        *,
        batch_size: int,
        verbose: bool = False,
    ) -> dict:
        del verbose
        partitions = self._partition_skills(skills, batch_size)
        assignments: dict[str, str] = {}
        max_workers = self._nested_pool_size(len(partitions))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            jobs = self._submit_assignment_jobs(executor, partitions, groups)
            for future in as_completed(jobs):
                try:
                    assignments.update(future.result())
                except Exception as exc:
                    console.print(f"[red]Classification batch failed: {exc}[/red]")
                    raise RuntimeError(f"Skill classification batch failed: {exc}") from exc
        return assignments

    @staticmethod
    def _partition_skills(skills: list[dict], batch_size: int) -> list[list[dict]]:
        windows: list[list[dict]] = []
        cursor = 0
        while cursor < len(skills):
            windows.append(skills[cursor:cursor + batch_size])
            cursor += batch_size
        return windows

    def _submit_assignment_jobs(self, executor, partitions: list[list[dict]], groups: dict):
        futures = []
        for batch in partitions:
            futures.append(executor.submit(self.classify_skills_single, batch, groups))
        return futures

    def validate_and_recover(
        self,
        skills: list[dict],
        groups: dict,
        assignments: dict,
        verbose: bool = False,
    ) -> dict:
        del verbose
        expected = {str(item.get("id", "")).strip() for item in skills}
        assigned = set(assignments.keys())
        missing = expected - assigned
        if not missing:
            return assignments

        if not assignments:
            console.print(
                Panel(
                    f"[bold red]Classification produced no usable assignments for {len(skills)} skills.[/bold red]",
                    title="[bold red]Classification Failed[/bold red]",
                    border_style="red",
                )
            )
            return assignments

        if len(missing) <= max(1, len(skills) // 2):
            retry_inputs = [item for item in self.sorted_skills(skills) if str(item.get("id", "")).strip() in missing]
            if retry_inputs:
                retry_result = self.classify_skills_single(retry_inputs, groups, is_retry=True)
                assignments = {**assignments, **retry_result}
                missing = expected - set(assignments.keys())

        if not missing:
            return assignments

        fallback_group_id = self._largest_group_id(groups, assignments)
        for skill_id in sorted(missing):
            assignments[skill_id] = fallback_group_id

        if missing:
            console.print(
                Panel(
                    f"[bold red]{len(missing)}/{len(skills)} skills were placed into fallback group "
                    f"'{fallback_group_id}'.[/bold red]",
                    title="[bold red]Classification Recovery[/bold red]",
                    border_style="red",
                )
            )
        return assignments

    def discover_groups(self, skills: list[dict], parent_context: Optional[dict], verbose: bool = False) -> dict:
        del verbose
        builder = self._builder
        context_section = self._render_context(parent_context)
        prompt = GROUP_DISCOVERY_PROMPT.format(
            count=len(skills),
            context_section=context_section,
            skills_list=self.format_skills_list(skills),
        )
        response = _builder_call(builder, "_call_llm_json", prompt)
        groups = response.get("groups", {}) if isinstance(response, dict) else {}
        normalized: dict[str, dict[str, str]] = {}
        for group_id, payload in self.iter_group_items(groups):
            normalized[str(group_id)] = {
                "name": payload.get("name", group_id),
                "description": payload.get("description", ""),
                "select_when": payload.get("select_when", ""),
                "dont_select_when": payload.get("dont_select_when", ""),
            }
        return normalized

    def merge_group_definitions(self, all_group_defs: list[dict], verbose: bool = False) -> dict:
        if verbose:
            console.print(f"[cyan]    Consolidating {len(all_group_defs)} discovery passes[/cyan]")
        prompt = GROUP_MERGE_PROMPT.format(
            all_groups=self._render_group_definition_samples(all_group_defs),
        )
        response = _builder_call(self._builder, "_call_llm_json", prompt)
        canonical_groups = response.get("canonical_groups", {}) if isinstance(response, dict) else {}
        merged: dict[str, dict[str, str]] = {}
        for group_id, payload in self.iter_group_items(canonical_groups):
            merged[str(group_id)] = {
                "name": payload.get("name", group_id),
                "description": payload.get("description", ""),
                "select_when": payload.get("select_when", ""),
                "dont_select_when": payload.get("dont_select_when", ""),
            }
        return merged

    def split_skills(self, skills: list[dict], parent_context: Optional[dict], verbose: bool = False) -> dict:
        batch_size = _builder_call(self._builder, "_auto_batch_size")
        if len(skills) <= batch_size:
            return self.split_skills_single(skills, parent_context, verbose=verbose)
        return self.batched_split_skills(skills, parent_context, batch_size=batch_size, verbose=verbose)

    def split_skills_single(self, skills: list[dict], parent_context: Optional[dict], verbose: bool = False) -> dict:
        groups = self.discover_groups(skills, parent_context, verbose=verbose)
        if not groups:
            return {}
        assignments = self.classify_skills(skills, groups, verbose=verbose)
        assignments = self.validate_and_recover(skills, groups, assignments, verbose=verbose)
        return self.build_groups_from_assignments(groups, assignments)

    def batched_split_skills(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        *,
        batch_size: int,
        verbose: bool = False,
    ) -> dict:
        if verbose:
            console.print(f"[cyan]  Sampling {len(skills)} skills for grouped discovery[/cyan]")

        discovery_groups = self._discover_from_samples(skills, parent_context, batch_size=batch_size, verbose=verbose)
        if not discovery_groups:
            return {}
        assignments = self.classify_skills(skills, discovery_groups, verbose=verbose)
        assignments = self.validate_and_recover(skills, discovery_groups, assignments, verbose=verbose)
        return self.build_groups_from_assignments(discovery_groups, assignments)

    def _discover_from_samples(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        *,
        batch_size: int,
        verbose: bool,
    ) -> dict:
        sampled_batches = self._sample_batches(skills, parent_context, batch_size)
        discovered: list[dict] = []

        if len(sampled_batches) == 1:
            return self.discover_groups(sampled_batches[0], parent_context, verbose=verbose)

        max_workers = self._nested_pool_size(len(sampled_batches))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.discover_groups, batch, parent_context, verbose): index
                for index, batch in enumerate(sampled_batches)
            }
            collected: list[tuple[int, dict]] = []
            for future in as_completed(future_map):
                try:
                    payload = future.result()
                except Exception as exc:
                    console.print(f"[red]Discovery batch failed: {exc}[/red]")
                    raise RuntimeError(f"Skill group discovery batch failed: {exc}") from exc
                else:
                    if payload:
                        collected.append((future_map[future], payload))
            collected.sort(key=lambda item: item[0])
            discovered = [payload for _, payload in collected]

        if not discovered:
            return {}
        if len(discovered) == 1:
            return discovered[0]
        return self.merge_group_definitions(discovered, verbose=verbose)

    def _sample_batches(self, skills: list[dict], parent_context: Optional[dict], batch_size: int) -> list[list[dict]]:
        ordered = self.sorted_skills(skills)
        if len(ordered) <= batch_size:
            return [ordered]

        shuffled = list(ordered)
        rng = random.Random(self.sampling_seed(parent_context, len(skills)))
        rng.shuffle(shuffled)

        batches = [shuffled[index:index + batch_size] for index in range(0, len(shuffled), batch_size)]
        return batches[:min(5, len(batches))]

    def _nested_pool_size(self, total_jobs: int) -> int:
        builder_workers = int(getattr(self._builder, "max_workers", 1) or 1)
        return max(1, min(int(total_jobs), max(2, builder_workers)))

    @staticmethod
    def _normalize_group_id(group_id: object, valid_groups: set[str]) -> str:
        candidate = str(group_id).strip()
        normalized = candidate.lower().replace("_", "-")
        return normalized if normalized in valid_groups else candidate

    @staticmethod
    def _largest_group_id(groups: dict, assignments: dict) -> str:
        counts: dict[str, int] = {str(group_id): 0 for group_id in groups.keys()}
        for group_id in assignments.values():
            counts[str(group_id)] = counts.get(str(group_id), 0) + 1
        return max(counts.items(), key=lambda item: (item[1], item[0]))[0]

    @staticmethod
    def _render_context(parent_context: Optional[dict]) -> str:
        if not parent_context:
            return "## Scope\nCreate the best top-level capability buckets for this skill set."
        parent_name = str(parent_context.get("name", "")).strip()
        parent_description = str(parent_context.get("description", "")).strip()
        return (
            "## Scope\n"
            f'These groups sit under "{parent_name}".\n'
            f"Parent description: {parent_description}\n"
            "Keep the children mutually distinct and locally coherent."
        )

    def _render_group_definition_samples(self, all_group_defs: list[dict]) -> str:
        sections: list[str] = []
        for index, group_defs in enumerate(all_group_defs, start=1):
            lines = [f"### Discovery Pass {index}"]
            for group_id, payload in self.iter_group_items(group_defs):
                lines.append(f"- {group_id}: {payload.get('name', group_id)}")
                description = str(payload.get("description", "")).strip()
                if description:
                    lines.append(f"  Description: {description}")
                select_when = str(payload.get("select_when", "")).strip()
                if select_when:
                    lines.append(f"  Select when: {select_when}")
                dont_select_when = str(payload.get("dont_select_when", "")).strip()
                if dont_select_when:
                    lines.append(f"  Don't select when: {dont_select_when}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)
