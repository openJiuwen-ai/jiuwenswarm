from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from dataclasses import dataclass
from pathlib import Path
import re
import threading
from time import perf_counter
from typing import Optional

try:
    from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError, OpenAI
except ModuleNotFoundError:
    APIConnectionError = APIError = APITimeoutError = AuthenticationError = None
    OpenAI = None

from shared.rich_compat import BarColumn, Console, Panel, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from indexing.scanners import create_scanner

from .schema import (
    DEFAULT_TREE_OUTPUT_PATH,
    DynamicTreeConfig,
    SKILL_DESCRIPTION_MAX_LENGTH,
    Skill,
    TreeManagerConfig,
    TreeNode,
)
from .expansion import TreeExpansionEngine as ExternalTreeExpansionEngine
from .grouping import TreeGroupingEngine
from .llm_runtime import TreeLLMRuntime as ExternalTreeLLMRuntime
from .prompts import (
    GROUP_MERGE_PROMPT,
    GROUP_DISCOVERY_PROMPT,
    SKILL_ASSIGNMENT_PROMPT,
    EQUIVALENCE_GROUPING_PROMPT,
    SKILL_PROFILE_PROMPT,
)
from .preset_writer import TreePresetWriter as ExternalTreePresetWriter
from .repair import TreeRepairEngine as ExternalTreeRepairEngine
from .types import ChildGroup as ExternalChildGroup, QueuedNode as ExternalQueuedNode

console = Console()


_QueuedNode = ExternalQueuedNode
_ChildGroup = ExternalChildGroup
_TreeLLMRuntime = ExternalTreeLLMRuntime
_TreePresetWriter = ExternalTreePresetWriter
_TreeExpansionEngine = ExternalTreeExpansionEngine
_TreeRepairEngine = ExternalTreeRepairEngine


@dataclass(frozen=True)
class BuildTreeOptions:
    skills_dir: Path | str | None = None
    output_path: Path | str | None = None
    config: DynamicTreeConfig | None = None
    manager_config: TreeManagerConfig | None = None
    client: OpenAI | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    llm_seed: int | None = None
    max_workers: int | None = None
    verbose: bool = False
    show_tree: bool = True
    generate_html: bool = True
    display_skills_dir: Path | str | None = None
    item_type: str = "skill"
    skill_entries: list[dict] | None = None


_BUILD_TREE_POSITIONAL_FIELDS = (
    "skills_dir",
    "output_path",
    "config",
    "manager_config",
    "client",
    "model",
    "api_key",
    "base_url",
    "llm_seed",
    "max_workers",
    "verbose",
    "show_tree",
    "generate_html",
    "display_skills_dir",
    "item_type",
    "skill_entries",
)


class TreeBuilder:
    """
    Unified tree builder with auto-selection and node splitting.

    Features:
    - Auto-selects build method based on skill count
    - Splits oversized nodes (> max_skills_per_node)
    - Simple tree visualization
    """

    # Token budget constants for auto batch size calculation
    PROMPT_OVERHEAD_TOKENS = 3000  # prompt template + instructions
    OUTPUT_RESERVE_TOKENS = 4000  # JSON response reserve
    AVG_TOKENS_PER_SKILL = 75  # average tokens per skill entry
    DEFAULT_CONTEXT_WINDOW = 128000  # fallback context window size
    DEFAULT_MAX_OUTPUT_TOKENS = 32768  # fallback max output tokens

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        output_path: Path | str | None = None,
        config: Optional[DynamicTreeConfig] = None,
        manager_config: TreeManagerConfig | None = None,
        client: OpenAI | None = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        llm_seed: int | None = None,
        max_workers: Optional[int] = None,
        display_skills_dir: Path | str | None = None,
        item_type: str = "skill",
        skill_entries: list[dict] | None = None,
    ):
        mcfg = manager_config or TreeManagerConfig()
        build_cfg = mcfg.build
        if skills_dir is None:
            raise ValueError("TreeBuilder requires a non-empty skills_dir")
        self.scanner = create_scanner(item_type, skills_dir, display_items_dir=display_skills_dir)
        self._skill_entries_override = (
            [dict(item) for item in (skill_entries or [])] if skill_entries is not None else None
        )
        default_tree_path = DEFAULT_TREE_OUTPUT_PATH
        self.output_path = Path(output_path) if output_path else default_tree_path
        self.config = config or DynamicTreeConfig(
            branching_factor=mcfg.branching_factor,
            max_depth=mcfg.max_depth,
            root_categories=mcfg.root_categories,
        )
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip()
        self._manager_config = mcfg
        self._llm_seed = llm_seed
        if not self.model:
            raise ValueError("TreeBuilder requires a non-empty llm model")
        if client is None and not self.api_key:
            raise ValueError("TreeBuilder requires a non-empty llm api key")
        self._client = (
            client
            if client is not None
            else (OpenAI(api_key=self.api_key, base_url=self.base_url) if OpenAI is not None else None)
        )
        self.max_workers = max_workers or build_cfg.max_workers
        self._postprocess_enabled = bool(build_cfg.postprocess_enabled)
        self._postprocess_max_passes = max(0, int(build_cfg.postprocess_max_passes))
        self._postprocess_min_skills = max(2, int(build_cfg.postprocess_min_skills))
        self._equiv_grouping_enabled = bool(build_cfg.equiv_grouping_enabled)
        self._discovery_seed = build_cfg.discovery_seed

        self._llm_calls = 0
        self._retry_calls = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_unknown = 0
        self._prompt_fingerprints: set[str] = set()
        self._leaf_skills = 0  # Skills that have reached leaf nodes
        self._counter_lock = threading.Lock()  # Protects _llm_calls, _leaf_skills, _consecutive_failures
        self._progress = None  # Rich progress bar
        self._progress_task = None
        self._batch_size_cache = None
        self._max_output_tokens_cache = None
        self._thread_local = threading.local()  # Per-thread truncation flag
        self._executor = None  # Shared executor, set in _build_tree
        self._llm_semaphore = threading.Semaphore(self.max_workers)  # Limit concurrent LLM calls
        self._consecutive_failures = 0
        self.max_consecutive_failures = 5
        self._llm_runtime = ExternalTreeLLMRuntime(self)
        self._preset_writer = ExternalTreePresetWriter(self)
        self._expansion_engine = ExternalTreeExpansionEngine(self)
        self._repair_engine = ExternalTreeRepairEngine(self)
        self._grouping_engine = TreeGroupingEngine(self)

    def _auto_batch_size(self) -> int:
        """Calculate batch size from model context window."""
        return self._llm_runtime.auto_batch_size()

    def _get_max_output_tokens(self) -> int:
        """Get max output tokens for the model, with caching."""
        return self._llm_runtime.get_max_output_tokens()

    def _merged_extra_body(self) -> dict:
        return self._llm_runtime.merged_extra_body()

    def _model_limits(self) -> tuple[int, int]:
        """Resolve model limits."""
        return self._llm_runtime.model_limits()

    def build(
        self,
        verbose: bool = False,
        show_tree: bool = True,
        generate_html: bool = True,
    ) -> dict:
        console.print(Panel.fit("[bold cyan]Building Capability Tree[/bold cyan]", border_style="cyan"))
        step1_start = perf_counter()
        skill_entries = self._load_skill_entries()
        console.print(f"[dim]Step 1 elapsed: {(perf_counter() - step1_start) * 1000.0:.2f} ms[/dim]")
        if not skill_entries:
            console.print("[red]No skills found.[/red]")
            return {}

        step1b_start = perf_counter()
        skill_entries = self._enrich_skill_profiles(skill_entries, verbose=verbose)
        console.print(f"[dim]Step 1b elapsed: {(perf_counter() - step1b_start) * 1000.0:.2f} ms[/dim]")
        step2_start = perf_counter()
        tree_root = self._build_tree(skill_entries, verbose)
        console.print(f"[dim]Step 2 elapsed: {(perf_counter() - step2_start) * 1000.0:.2f} ms[/dim]")
        step3_start = perf_counter()
        tree_dict = self._tree_to_dict(tree_root)
        preset_dict = self._emit_tree_artifacts(
            tree_dict,
            show_tree=show_tree,
            generate_html=generate_html,
        )
        console.print(f"[dim]Step 3 elapsed: {(perf_counter() - step3_start) * 1000.0:.2f} ms[/dim]")
        self._print_cache_stats()
        self._print_build_summary()
        return preset_dict

    def _load_skill_entries(self) -> list[dict]:
        console.print("\n[bold]Step 1: Scanning skills...[/bold]")
        if self._skill_entries_override is not None:
            skill_entries = [dict(item) for item in self._skill_entries_override]
        else:
            skill_entries = self.scanner.to_dict_list()
        if skill_entries:
            console.print(f"[green]Found {len(skill_entries)} skills[/green]")
        return skill_entries

    def _enrich_skill_profiles(self, skill_entries: list[dict], *, verbose: bool = False) -> list[dict]:
        if not self._skill_profiles_enabled:
            return list(skill_entries)

        console.print("\n[bold]Step 1b: Normalizing skill routing profiles...[/bold]")
        enriched: list[dict] = []
        ordered = self._sorted_skills(skill_entries)
        for batch in self._skill_profile_batches(ordered):
            profiles = self._generate_skill_profiles(batch, verbose=verbose)
            for skill in batch:
                enriched.append(self._apply_skill_profile(skill, profiles.get(str(skill.get("id") or "").strip())))
        if self._deterministic_prompts:
            return sorted(enriched, key=lambda item: (str(item.get("id", "")), str(item.get("name", ""))))
        return enriched

    def _skill_profile_batches(self, skills: list[dict]) -> list[list[dict]]:
        batch_size = min(self._skill_profile_batch_size, max(1, self._auto_batch_size()))
        return [skills[index:index + batch_size] for index in range(0, len(skills), batch_size)]

    def _generate_skill_profiles(self, skills: list[dict], *, verbose: bool = False) -> dict[str, dict[str, str]]:
        if not skills:
            return {}
        prompt = SKILL_PROFILE_PROMPT.format(
            skills_list=self._format_skill_profile_inputs(skills),
            description_limit=self._skill_profile_description_limit,
            rule_limit=self._skill_profile_rule_limit,
        )
        result = self._call_llm_json(prompt)
        raw_profiles = result.get("profiles", {}) if isinstance(result, dict) else {}
        if not isinstance(raw_profiles, dict):
            if verbose:
                console.print("[yellow]  Skill profile generation returned no profile mapping[/yellow]")
            return {}

        valid_ids = {str(skill.get("id") or "").strip() for skill in skills}
        profiles: dict[str, dict[str, str]] = {}
        for skill_id, payload in raw_profiles.items():
            normalized_id = str(skill_id or "").strip()
            if normalized_id not in valid_ids or not isinstance(payload, dict):
                continue
            profiles[normalized_id] = {
                "description": self._compact_profile_field(
                    payload.get("description"), limit=self._skill_profile_description_limit
                ),
                "select_when": self._compact_profile_field(
                    payload.get("select_when"), limit=self._skill_profile_rule_limit
                ),
                "dont_select_when": self._compact_profile_field(
                    payload.get("dont_select_when"), limit=self._skill_profile_rule_limit
                ),
            }
        return profiles

    def _format_skill_profile_inputs(self, skills: list[dict]) -> str:
        rows: list[str] = []
        for skill in self._sorted_skills(skills):
            skill_id = str(skill.get("id") or "").strip()
            name = str(skill.get("name") or skill_id).strip() or skill_id
            description = self._compact_profile_field(skill.get("description"), limit=600)
            content = self._compact_profile_field(skill.get("content"), limit=900)
            rows.append(f"- id: {skill_id}")
            rows.append(f"  name: {name}")
            if description:
                rows.append(f"  source_description: {description}")
            if content:
                rows.append(f"  source_content: {content}")
        return "\n".join(rows)

    def _apply_skill_profile(self, skill: dict, profile: dict[str, str] | None) -> dict:
        source_description = str(skill.get("source_description") or skill.get("description") or "").strip()
        if not profile:
            return self._with_fallback_skill_profile(skill)

        description = self._compact_profile_field(
            profile.get("description"), limit=self._skill_profile_description_limit
        )
        if not description:
            return self._with_fallback_skill_profile(skill)

        select_when = self._compact_profile_field(profile.get("select_when"), limit=self._skill_profile_rule_limit)
        dont_select_when = self._compact_profile_field(
            profile.get("dont_select_when"), limit=self._skill_profile_rule_limit
        )
        if not self._skill_profile_select_rules_enabled:
            select_when = ""
            dont_select_when = ""

        updated = dict(skill)
        updated["source_description"] = source_description
        updated["routing_description"] = description
        updated["select_when"] = select_when
        updated["dont_select_when"] = dont_select_when
        updated["description"] = self._compose_skill_routing_description(
            description=description,
            select_when=select_when,
            dont_select_when=dont_select_when,
        )
        return updated

    def _with_fallback_skill_profile(self, skill: dict) -> dict:
        source_description = str(skill.get("source_description") or skill.get("description") or "").strip()
        name = str(skill.get("name") or skill.get("id") or "").strip()
        fallback = self._compact_profile_field(source_description or name, limit=self._skill_profile_description_limit)
        updated = dict(skill)
        updated["source_description"] = source_description
        updated["routing_description"] = fallback
        updated.setdefault("select_when", "")
        updated.setdefault("dont_select_when", "")
        updated["description"] = self._compose_skill_routing_description(
            description=fallback,
            select_when=str(updated.get("select_when") or ""),
            dont_select_when=str(updated.get("dont_select_when") or ""),
        )
        return updated

    def _compose_skill_routing_description(
        self, *, description: str, select_when: str = "", dont_select_when: str = ""
    ) -> str:
        parts = [self._compact_profile_field(description, limit=self._skill_profile_description_limit)]
        if self._skill_profile_select_rules_enabled and select_when:
            parts.append(
                f"Select when: {self._compact_profile_field(select_when, limit=self._skill_profile_rule_limit)}"
            )
        if self._skill_profile_select_rules_enabled and dont_select_when:
            rule_text = self._compact_profile_field(dont_select_when, limit=self._skill_profile_rule_limit)
            parts.append(f"Don't select when: {rule_text}")
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _compact_profile_field(value: object, *, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[:max(0, limit - 3)].rstrip() + "..."

    def _emit_tree_artifacts(self, tree_dict: dict, *, show_tree: bool, generate_html: bool) -> dict:
        console.print("\n[bold]Step 3: Writing to file...[/bold]")
        preset_dict = self._tree_to_orchestrator_preset(tree_dict)
        self._write_yaml(preset_dict)
        if generate_html:
            from .visualizer import generate_html as gen_html

            html_path = self.output_path.with_suffix(".html")
            gen_html(tree_dict, html_path)
            console.print(f"[green]Generated HTML: {html_path}[/green]")
        if show_tree:
            console.print("\n[bold]Tree Structure:[/bold]")
            self._print_tree(tree_dict)
        return preset_dict

    def _print_build_summary(self) -> None:
        summary_lines = [f"[bold green]Done![/bold green] ({self._llm_calls} LLM calls)"]
        if self._cache_observability:
            summary_lines.extend(
                [
                    f"Cache hits/misses/unknown: {self._cache_hits}/{self._cache_misses}/{self._cache_unknown}",
                    f"Unique prompt fingerprints: {len(self._prompt_fingerprints)}",
                ]
            )
        summary_lines.append(f"Output: {self.output_path}")
        console.print(Panel.fit("\n".join(summary_lines), border_style="green"))

    def _build_tree(self, skills: list[dict], verbose: bool = False) -> TreeNode:
        console.print("\n[bold]Step 2: Building tree structure...[/bold]")
        root = TreeNode(id="root", name="Root", description="Skill capability tree root")
        self._leaf_skills = 0
        pending_nodes: deque[ExternalQueuedNode] = deque([ExternalQueuedNode(root, skills, 0, None)])
        active_jobs: dict = {}

        with self._tree_progress(total=len(skills)) as progress:
            self._progress = progress
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                self._executor = executor
                while pending_nodes or active_jobs:
                    self._submit_pending_nodes(pending_nodes, active_jobs, executor, verbose=verbose)
                    self._refresh_pending_metric(active_jobs)
                    if not active_jobs:
                        continue
                    self._harvest_finished_nodes(active_jobs, pending_nodes)

            self._progress = None
            self._executor = None

        self._repair_tree(root, source_skills=skills, verbose=verbose)
        return root

    def _tree_progress(self, *, total: int) -> Progress:
        status_text = (
            "("
            "{task.fields[leaf]}/{task.fields[total_count]} skills done, "
            "{task.fields[llm]} LLM, "
            "{task.fields[pending]} pending)"
        )
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn(status_text),
            console=console,
            transient=False,
        )
        self._progress_task = progress.add_task(
            "Building capability tree",
            total=total,
            pending=0,
            leaf=0,
            llm=0,
            total_count=total,
        )
        return progress

    def _submit_pending_nodes(
        self,
        pending_nodes: deque[ExternalQueuedNode],
        active_jobs: dict,
        executor: ThreadPoolExecutor,
        *,
        verbose: bool,
    ) -> None:
        while pending_nodes:
            job = pending_nodes.popleft()
            future = executor.submit(
                self._process_node,
                node=job.node,
                skills=job.skills,
                depth=job.depth,
                parent_context=job.parent_context,
                verbose=verbose,
            )
            active_jobs[future] = job

    def _refresh_pending_metric(self, active_jobs: dict) -> None:
        if self._progress is None or self._progress_task is None:
            return
        self._progress.update(self._progress_task, pending=len(active_jobs))

    def _harvest_finished_nodes(
        self,
        active_jobs: dict,
        pending_nodes: deque[ExternalQueuedNode],
    ) -> None:
        done, _ = wait(tuple(active_jobs.keys()), return_when=FIRST_COMPLETED)
        finished = sorted(done, key=lambda future: active_jobs[future].node.id)
        for future in finished:
            job = active_jobs.pop(future)
            try:
                child_groups = future.result()
            except Exception as exc:
                console.print(f"[red]Error processing {job.node.id}: {exc}[/red]")
                raise RuntimeError(f"Skill tree node processing failed for '{job.node.id}': {exc}") from exc
            else:
                for child_group in child_groups:
                    pending_nodes.append(self._queue_child_node(child_group, depth=job.depth + 1))

    def _repair_tree(self, root: TreeNode, *, source_skills: list[dict], verbose: bool) -> None:
        self._audit_tree(root, source_skills)
        if self._postprocess_enabled and self._postprocess_max_passes > 0:
            self._postprocess_tree(root, verbose)
        if self._equiv_grouping_enabled:
            self._normalize_to_equivalence_groups(root, verbose)
        self._audit_tree(root, source_skills)

    @staticmethod
    def _collect_leaf_skills(node: TreeNode) -> set:
        seen_ids: set[str] = set()
        frontier = [node]
        while frontier:
            current = frontier.pop()
            if current.children:
                frontier.extend(current.children)
                continue
            for skill in current.skills:
                seen_ids.add(skill.id)
        return seen_ids

    def _audit_tree(self, root: TreeNode, input_skills: list[dict]) -> None:
        expected_ids = {str(skill.get("id", "")).strip() for skill in input_skills}
        discovered_ids = self._collect_leaf_skills(root)
        missing_ids = sorted(skill_id for skill_id in expected_ids if skill_id and skill_id not in discovered_ids)
        if not missing_ids:
            return

        missing_lookup = {str(skill.get("id", "")).strip(): skill for skill in input_skills}
        recovered_payloads = [missing_lookup[skill_id] for skill_id in missing_ids if skill_id in missing_lookup]
        console.print(
            Panel(
                "\n".join(
                    [
                        "[bold red]Recovered "
                        f"{len(recovered_payloads)} missing skills after tree construction.[/bold red]",
                        "They have been attached under a fallback branch to preserve index completeness.",
                    ]
                ),
                title="[bold red]Tree Audit Recovery[/bold red]",
                border_style="red",
            )
        )

        fallback_branch = TreeNode(
            id="uncategorized",
            name="Uncategorized",
            description="Skills recovered by integrity audit after tree construction.",
            depth=1,
            parent_id=root.id,
        )
        self._assign_skills_to_leaf(fallback_branch, recovered_payloads)
        root.children.append(fallback_branch)

    @staticmethod
    def _queue_child_node(child_group: _ChildGroup, *, depth: int) -> _QueuedNode:
        """Convert a processed child subtree into a queued work item."""
        return _QueuedNode(
            node=child_group.node,
            skills=child_group.skills,
            depth=depth,
            parent_context={
                "name": child_group.node.name,
                "description": child_group.node.description,
            },
        )

    def _process_node(
        self,
        node: TreeNode,
        skills: list[dict],
        depth: int,
        parent_context: Optional[dict],
        verbose: bool = False,
    ) -> list[_ChildGroup]:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.process_node(
            node=node,
            skills=skills,
            depth=depth,
            parent_context=parent_context,
            verbose=verbose,
        )

    # =========================================================================
    # Two-phase classification: discover groups -> flat assignment
    # =========================================================================

    def _assign_skills_to_leaf(self, node: TreeNode, skills: list[dict]) -> None:
        """Assign skill dicts to a leaf node as Skill objects. Updates progress counter."""
        for skill_data in skills:
            node.skills.append(self._skill_from_data(skill_data, path=node.id))
        # Update leaf skills count (thread-safe)
        with self._counter_lock:
            self._leaf_skills += len(skills)
            if self._progress and self._progress_task is not None:
                self._progress.update(self._progress_task, leaf=self._leaf_skills, completed=self._leaf_skills)

    @staticmethod
    def _skill_from_data(skill_data: dict, *, path: str) -> Skill:
        """Materialize a Skill object from a skill dict."""
        return Skill(
            id=skill_data["id"],
            name=skill_data.get("name", skill_data["id"]),
            description=skill_data.get("description", ""),
            path=path,
            skill_path=skill_data.get("skill_path", ""),
            content=skill_data.get("content", ""),
            select_when=skill_data.get("select_when", ""),
            dont_select_when=skill_data.get("dont_select_when", ""),
            source_description=skill_data.get("source_description", ""),
            github_url=skill_data.get("github_url", ""),
            stars=skill_data.get("stars", 0),
            is_official=skill_data.get("is_official", False),
            author=skill_data.get("author", ""),
        )

    @staticmethod
    def _skill_to_data(skill: Skill) -> dict:
        """Convert a Skill object back into a mutable skill dict."""
        return skill.to_dict(include_content=True)

    def _root_group_definitions(self) -> dict[str, dict[str, str]]:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.root_group_definitions()

    def _create_child_node(
        self,
        *,
        parent: TreeNode,
        group_id: str,
        group_data: dict,
        depth: int,
    ) -> TreeNode:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.create_child_node(parent=parent, group_id=group_id, group_data=group_data, depth=depth)

    def _build_children_from_groups(
        self,
        node: TreeNode,
        skills: list[dict],
        groups: dict,
        depth: int,
        verbose: bool = False,
    ) -> list[_ChildGroup]:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.build_children_from_groups(node, skills, groups, depth, verbose)

    def _reassign_skills_to_children(
        self,
        unassigned_skills: list[dict],
        children_to_process: list[_ChildGroup],
    ) -> tuple[int, list[dict]]:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.reassign_skills_to_children(unassigned_skills, children_to_process)

    def _assign_unassigned_skills(
        self,
        *,
        node: TreeNode,
        all_skills: list[dict],
        remaining_skill_map: dict[str, dict],
        children_to_process: list[_ChildGroup],
        verbose: bool = False,
    ) -> None:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        expansion_engine.assign_unassigned_skills(
            node=node,
            all_skills=all_skills,
            remaining_skill_map=remaining_skill_map,
            children_to_process=children_to_process,
            verbose=verbose,
        )

    def _rewrite_node_label_after_singleton(
        self,
        node: TreeNode,
        children_to_process: list[_ChildGroup],
        verbose: bool = False,
    ) -> None:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        expansion_engine.rewrite_node_label_after_singleton(node, children_to_process, verbose)

    def _postprocess_tree(self, root: TreeNode, verbose: bool = False) -> None:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        repair_engine.postprocess_tree(root, verbose)

    def _postprocess_node(self, node: TreeNode, verbose: bool = False) -> int:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        return repair_engine.postprocess_node(node, verbose)

    def _rebalance_child_assignments(self, node: TreeNode, verbose: bool = False) -> int:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        return repair_engine.rebalance_child_assignments(node, verbose)

    def _collect_subtree_skill_locations(self, node: TreeNode) -> list[tuple[TreeNode, dict]]:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        return repair_engine.collect_subtree_skill_locations(node)

    def _collect_subtree_skill_dicts(self, node: TreeNode) -> list[dict]:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        return repair_engine.collect_subtree_skill_dicts(node)

    def _existing_child_groups(self, children: list[TreeNode]) -> list[_ChildGroup]:
        expansion_engine = getattr(self, "_expansion_engine", None) or _TreeExpansionEngine(self)
        return expansion_engine.existing_child_groups(children)

    def _choose_child_for_skill(self, skill_data: dict, children: list[TreeNode]) -> TreeNode:
        """Choose the best direct child for a skill, falling back to the largest subtree."""
        child_by_id = {child.id: child for child in children}
        groups = {
            child.id: {
                "name": child.name,
                "description": child.description,
                "select_when": child.select_when,
                "dont_select_when": child.dont_select_when,
            }
            for child in children
        }
        assignment = self._classify_skills_single(
            [skill_data],
            groups,
            verbose=False,
            is_retry=True,
        )
        child_id = assignment.get(str(skill_data.get("id", "")).strip())
        if child_id in child_by_id:
            return child_by_id[child_id]
        return max(children, key=lambda child: child.count_all_skills())

    def _insert_skill_into_subtree(self, node: TreeNode, skill_data: dict) -> None:
        """Insert a skill into the best-fitting location inside an existing subtree."""
        skill_id = str(skill_data.get("id", "")).strip()
        if not skill_id:
            return

        if node.is_leaf or not node.children:
            if any(skill.id == skill_id for skill in node.skills):
                return
            node.skills.append(self._skill_from_data(skill_data, path=node.id))
            return

        target_child = self._choose_child_for_skill(skill_data, node.children)
        self._insert_skill_into_subtree(target_child, skill_data)

    def _prune_empty_children(self, node: TreeNode) -> int:
        """Remove empty child subtrees after skill moves."""
        removed = 0
        kept_children: list[TreeNode] = []
        for child in node.children:
            removed += self._prune_empty_children(child)
            if child.children:
                if child.count_all_skills() <= 0:
                    removed += 1
                    continue
            elif not child.skills:
                removed += 1
                continue
            kept_children.append(child)
        node.children = kept_children
        return removed

    def _normalize_to_equivalence_groups(self, root: TreeNode, verbose: bool = False) -> None:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        repair_engine.normalize_to_equivalence_groups(root, verbose)

    @staticmethod
    def _is_second_leaf_node(node: TreeNode) -> bool:
        """Second-leaf node: has children and all children are leaf nodes."""
        return _TreeRepairEngine.is_second_leaf_node(node)

    def _split_second_leaf_node_into_equiv_groups(
        self,
        parent_node: TreeNode,
        second_leaf_node: TreeNode,
        verbose: bool = False,
    ) -> list[TreeNode]:
        repair_engine = getattr(self, "_repair_engine", None) or _TreeRepairEngine(self)
        return repair_engine.split_second_leaf_node_into_equiv_groups(parent_node, second_leaf_node, verbose)

    def _discover_equivalence_groups(
        self,
        second_leaf_node: TreeNode,
        leaf_children: list[TreeNode],
        verbose: bool = False,
    ) -> dict:
        """Ask LLM to partition second-leaf children into equivalence groups."""
        leaf_lines = []
        for leaf in leaf_children:
            sample_skill_ids = ", ".join(skill.id for skill in leaf.skills[:5]) or "(none)"
            leaf_lines.append(
                f"- id: {leaf.id}\n"
                f"  name: {leaf.name}\n"
                f"  description: {leaf.description or '(no description)'}\n"
                f"  select_when: {leaf.select_when or ''}\n"
                f"  dont_select_when: {leaf.dont_select_when or ''}\n"
                f"  sample_skill_ids: {sample_skill_ids}"
            )

        prompt = EQUIVALENCE_GROUPING_PROMPT.format(
            parent_id=second_leaf_node.id,
            parent_name=second_leaf_node.name,
            parent_description=second_leaf_node.description or "(no description)",
            leaf_nodes="\n".join(leaf_lines),
        )
        result = self._call_llm_json(prompt)
        groups = result.get("groups", {})
        if not isinstance(groups, dict):
            if verbose:
                console.print(f"[yellow]  Equivalence grouping failed for '{second_leaf_node.id}'[/yellow]")
            return {}
        return groups

    def _normalize_equivalence_groups(self, leaf_children: list[TreeNode], groups: dict) -> list[dict]:
        """
        Normalize and repair LLM equivalence groups.

        Guarantees:
        - Every original leaf appears in exactly one output group
        - Unknown leaf IDs are ignored
        - Empty groups are removed
        """
        leaf_map = {leaf.id: leaf for leaf in leaf_children}
        assigned: set[str] = set()
        normalized: list[dict] = []

        for group_id, group_data in self._iter_group_items(groups):
            if not isinstance(group_data, dict):
                continue
            raw_leaf_ids = group_data.get("leaf_ids", [])
            if not isinstance(raw_leaf_ids, list):
                raw_leaf_ids = []
            leaf_nodes = []
            for leaf_id in raw_leaf_ids:
                lid = str(leaf_id).strip()
                if not lid or lid in assigned:
                    continue
                leaf = leaf_map.get(lid)
                if leaf is None:
                    continue
                assigned.add(lid)
                leaf_nodes.append(leaf)
            if not leaf_nodes:
                continue
            normalized.append(
                {
                    "id": self._build_equivalence_group_id(
                        group_id=str(group_id).strip(),
                        group_name=str(group_data.get("name") or "").strip(),
                        fallback="equiv-group",
                    ),
                    "name": str(group_data.get("name") or group_id),
                    "description": str(group_data.get("description") or ""),
                    "select_when": str(group_data.get("select_when") or ""),
                    "dont_select_when": str(group_data.get("dont_select_when") or ""),
                    "leaf_nodes": leaf_nodes,
                }
            )

        # Recovery: assign missing leaves conservatively.
        missing = [leaf for leaf in leaf_children if leaf.id not in assigned]
        for leaf in missing:
            normalized.append(
                {
                    "id": f"equiv-{self._slug_term(leaf.id, fallback='leaf')}",
                    "name": leaf.name or leaf.id,
                    "description": leaf.description or "Equivalent capability group.",
                    "select_when": leaf.select_when,
                    "dont_select_when": leaf.dont_select_when,
                    "leaf_nodes": [leaf],
                }
            )

        # Keep deterministic order.
        for item in normalized:
            item["leaf_nodes"] = sorted(item["leaf_nodes"], key=lambda leaf: leaf.id)
        normalized.sort(key=lambda item: str(item.get("id", "")))
        return normalized

    def _build_equivalence_group_id(self, *, group_id: str, group_name: str, fallback: str) -> str:
        """
        Build a stable, readable node id for equivalence groups.

        LLMs often emit placeholder ids like G1/G2. We prefer semantic ids derived
        from the group name and only fall back to the raw id when it is informative.
        """
        raw_name = str(group_name or "").strip()
        raw_id = str(group_id or "").strip()
        generic_id = bool(re.fullmatch(r"g\d+(?:-\d+)?", raw_id.lower()))

        if raw_name:
            return self._slug_term(raw_name, fallback=fallback)
        if raw_id and not generic_id:
            return self._slug_term(raw_id, fallback=fallback)
        return self._slug_term(fallback, fallback="equiv-group")

    def _sorted_skills(self, skills: list[dict]) -> list[dict]:
        return self._grouping_engine.sorted_skills(skills)

    def _iter_group_items(self, groups: dict):
        return self._grouping_engine.iter_group_items(groups)

    def _normalize_prompt_for_fingerprint(self, prompt: str) -> str:
        """Normalize prompt text to keep fingerprint stable across runs."""
        return self._llm_runtime.normalize_prompt_for_fingerprint(prompt)

    def _prompt_fingerprint(self, prompt: str) -> str:
        """Compute deterministic prompt fingerprint."""
        return self._llm_runtime.prompt_fingerprint(prompt)

    def _sampling_seed(self, parent_context: Optional[dict], skills_count: int) -> int:
        return self._grouping_engine.sampling_seed(parent_context, skills_count)

    def _extract_cache_hit(self, response) -> Optional[bool]:
        """Best-effort extraction of cache hit status from response metadata."""
        return self._llm_runtime.extract_cache_hit(response)

    def _extract_cache_hit_from_mapping(self, mapping: dict) -> Optional[bool]:
        """Parse cache hit from a mapping (recursively over nested dicts)."""
        return self._llm_runtime.extract_cache_hit_from_mapping(mapping)

    def _record_cache_observation(self, cache_hit: Optional[bool]) -> None:
        """Aggregate cache hit/miss counters."""
        self._llm_runtime.record_cache_observation(cache_hit)

    def _print_cache_stats(self) -> None:
        """Print cache observability metrics for intuitive build feedback."""
        self._llm_runtime.print_cache_stats()

    def _build_groups_from_assignments(self, groups: dict, assignments: dict) -> dict:
        return self._grouping_engine.build_groups_from_assignments(groups, assignments)

    def _classify_skills(self, skills: list[dict], groups: dict, verbose: bool = False) -> dict:
        return self._grouping_engine.classify_skills(skills, groups, verbose=verbose)

    def _classify_skills_single(
        self,
        skills: list[dict],
        groups: dict,
        verbose: bool = False,
        is_retry: bool = False,
    ) -> dict:
        return self._grouping_engine.classify_skills_single(
            skills,
            groups,
            verbose=verbose,
            is_retry=is_retry,
        )

    def _batched_classify_skills(
        self,
        skills: list[dict],
        groups: dict,
        batch_size: int,
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.batched_classify_skills(
            skills,
            groups,
            batch_size=batch_size,
            verbose=verbose,
        )

    def _validate_and_recover(
        self,
        skills: list[dict],
        groups: dict,
        assignments: dict,
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.validate_and_recover(
            skills,
            groups,
            assignments,
            verbose=verbose,
        )

    def _discover_groups(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.discover_groups(skills, parent_context, verbose=verbose)

    def _merge_group_definitions(self, all_group_defs: list[dict], verbose: bool = False) -> dict:
        return self._grouping_engine.merge_group_definitions(all_group_defs, verbose=verbose)

    def _split_skills(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.split_skills(skills, parent_context, verbose=verbose)

    def _split_skills_single(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.split_skills_single(skills, parent_context, verbose=verbose)

    def _batched_split_skills(
        self,
        skills: list[dict],
        parent_context: Optional[dict],
        batch_size: int,
        verbose: bool = False,
    ) -> dict:
        return self._grouping_engine.batched_split_skills(
            skills,
            parent_context,
            batch_size=batch_size,
            verbose=verbose,
        )

    def _call_llm(self, prompt: str, is_retry: bool = False, retry_left: int | None = None) -> str:
        """Call LLM and return response."""
        return self._llm_runtime.call_llm(prompt, is_retry=is_retry, retry_left=retry_left)

    def _call_llm_json(self, prompt: str, max_retries: int = 3, is_retry: bool = False) -> dict:
        """Call LLM expecting a JSON dict response, with retry on format errors."""
        return self._llm_runtime.call_llm_json(prompt, max_retries=max_retries, is_retry=is_retry)

    def _format_skills_list(self, skills: list[dict]) -> str:
        return self._grouping_engine.format_skills_list(skills)

    def _tree_to_dict(self, tree: TreeNode) -> dict:
        writer = getattr(self, "_preset_writer", None)
        if writer is None:
            writer = _TreePresetWriter(self)
        converted = writer.tree_to_dict(tree)
        return dict(converted)

    def _tree_to_orchestrator_preset(self, tree_dict: dict) -> dict:
        return self._preset_writer.tree_to_orchestrator_preset(tree_dict)

    def _flatten_capability_tree(self, tree: dict) -> list[dict]:
        return self._preset_writer.flatten_capability_tree(tree)

    def _rename_leaf_nodes(self, nodes: list[dict]) -> list[dict]:
        return self._preset_writer.rename_leaf_nodes(nodes)

    def _compact_leaf_cid_seed(self, *, worker_id: str, display_name: str, old_term: str) -> str:
        preset_writer = getattr(self, "_preset_writer", None)
        if preset_writer is None:
            preset_writer = _TreePresetWriter(self)
        return preset_writer.compact_leaf_cid_seed(
            worker_id=worker_id,
            display_name=display_name,
            old_term=old_term,
        )

    def _cid_term(self, value: str, fallback: str = "Node") -> str:
        preset_writer = getattr(self, "_preset_writer", None)
        if preset_writer is None:
            preset_writer = _TreePresetWriter(self)
        return preset_writer.cid_term(value, fallback=fallback)

    @staticmethod
    def _build_routing_policy(nodes: list[dict]) -> str:
        root_entries = sorted(
            [item for item in nodes if "." not in str(item.get("cid", ""))], key=lambda item: str(item.get("cid", ""))
        )
        lines = [
            "Route by descending the node tree one level at a time.",
            "Treat a user request as potentially multi-step unless the latest observation already fully "
            "satisfies every explicit requirement.",
            "Prefer leaves whose descriptions best match the next unmet sub-problem in the user request.",
            "After a worker returns, check whether unmet requirements still remain; if they do, continue "
            "routing instead of finishing early.",
            "Do not jump to User.Final after a single worker call when the user asked for multiple actions, "
            "dependencies, or deliverables.",
            "Use worker observations as intermediate state: one skill may gather facts or create prerequisites "
            "for a later skill.",
            "When multiple branches overlap, use the child descriptions as the local decision surface.",
            "Choose User.Final only when the latest observation set is sufficient to answer the whole user "
            "request, not just one subtask.",
        ]
        for item in root_entries:
            lines.append(f"If the request matches '{item['cid']}', continue under that branch.")
        return "\n".join(f"- {line}" for line in lines)

    def _build_tree_sketch(self, nodes: list[dict]) -> str:
        return self._preset_writer.build_tree_sketch(nodes)

    def _slug_term(self, value: str, fallback: str = "node") -> str:
        preset_writer = getattr(self, "_preset_writer", None)
        if preset_writer is None:
            preset_writer = _TreePresetWriter(self)
        return preset_writer.slug_term(value, fallback=fallback)

    @staticmethod
    def _join_cid(parent: str, child: str) -> str:
        return _TreePresetWriter.join_cid(parent, child)

    @staticmethod
    def _parent_cid(cid: str) -> str:
        return _TreePresetWriter.parent_cid(cid)

    def _unique_child_cid(self, parent_cid: str, segment: str, used: set[str]) -> str:
        return self._preset_writer.unique_child_cid(parent_cid, segment, used)

    def _extract_keywords(self, *values: str, limit: int = 8) -> list[str]:
        return self._preset_writer.extract_keywords(*values, limit=limit)

    def _node_to_dict(self, node: TreeNode) -> dict:
        writer = getattr(self, "_preset_writer", None)
        if writer is None:
            writer = _TreePresetWriter(self)
        payload = writer.node_to_dict(node)
        return payload.copy()

    def _print_tree(self, tree_dict: dict) -> None:
        """Print tree structure using rich (supports arbitrary depth)."""
        self._preset_writer.print_tree(tree_dict)

    def _add_node_to_rich_tree(self, parent_branch, node_dict: dict) -> None:
        """Recursively add nodes to rich tree."""
        self._preset_writer.add_node_to_rich_tree(parent_branch, node_dict)

    def _count_skills_in_dict(self, node_dict: dict) -> int:
        """Recursively count skills in a node dict."""
        return self._preset_writer.count_skills_in_dict(node_dict)


# Convenience function
def build_tree(*args, options: BuildTreeOptions | None = None, **kwargs) -> dict:
    """Build capability tree."""
    if options is not None and (args or kwargs):
        raise TypeError("build_tree accepts either options or direct arguments, not both")
    if options is None:
        if len(args) > len(_BUILD_TREE_POSITIONAL_FIELDS):
            raise TypeError(f"build_tree expected at most {len(_BUILD_TREE_POSITIONAL_FIELDS)} positional arguments")
        for field_name, value in zip(_BUILD_TREE_POSITIONAL_FIELDS, args):
            if field_name in kwargs:
                raise TypeError(f"build_tree got multiple values for argument '{field_name}'")
            kwargs[field_name] = value
        options = BuildTreeOptions(**kwargs)

    builder = TreeBuilder(
        options.skills_dir,
        options.output_path,
        config=options.config,
        manager_config=options.manager_config,
        client=options.client,
        model=options.model,
        api_key=options.api_key,
        base_url=options.base_url,
        llm_seed=options.llm_seed,
        max_workers=options.max_workers,
        display_skills_dir=options.display_skills_dir,
        item_type=options.item_type,
        skill_entries=options.skill_entries,
    )
    return builder.build(
        verbose=options.verbose,
        show_tree=options.show_tree,
        generate_html=options.generate_html,
    )
