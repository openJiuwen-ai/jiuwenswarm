# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue-fix facade that keeps GitCode issue handling out of core service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from openjiuwen.auto_harness.pipelines import META_EVOLVE_PIPELINE
from openjiuwen.core.foundation.llm import Model

from .gitcode_issue_client import GitCodeIssueClient
from .issue_matrix_store import IssueMatrixStore
from .issue_runner import GitCodeIssueRunner, IssueWatchOptions
from .issue_state_store import IssueStateStore


# Running 状态不允许删除
RUNNING_STATUSES = {"running", "task_created", "queued", "pending"}


class IssueFixService:
    """Coordinates GitCode issue ingestion as an auto-harness capability."""

    def __init__(
        self,
        *,
        task_store: Any,
        issue_state_store: IssueStateStore,
        issue_matrix_store: IssueMatrixStore,
        harness_service: Any,
        base_config_getter: Callable[[], Any],
        default_repo_url: str,
    ) -> None:
        self._task_store = task_store
        self._issue_state_store = issue_state_store
        self._issue_matrix_store = issue_matrix_store
        self._harness_service = harness_service
        self._base_config_getter = base_config_getter
        self._default_repo_url = default_repo_url

    async def handle(
        self,
        action: str,
        params: dict[str, Any],
        model: Optional[Model] = None,
    ) -> dict[str, Any]:
        """Dispatch issue-fix capability actions."""
        if action in {"process_once", "watch_once"}:
            return await self.process_gitcode_issues_once(params, model)
        if action in {"state_list", "list_states"}:
            return await self.list_gitcode_issue_states()
        if action == "delete":
            return await self.delete_issue_states(params)
        if action == "matrix":
            return await self.refresh_issue_matrix(params)
        return {"error": f"未知 issue-fix 操作: {action}"}

    @staticmethod
    def _parse_repo_identifier(repo: str) -> tuple[str, str]:
        """Parse owner/repo from a GitCode URL or owner/repo string."""
        raw = str(repo or "").strip()
        if not raw:
            return ("", "")
        cleaned = raw.rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        parts = [part for part in cleaned.split("/") if part]
        if len(parts) >= 2:
            return (parts[-2], parts[-1])
        return ("", "")

    def _resolve_target_repo(self, params: dict[str, Any]) -> tuple[str, str]:
        owner = str(params.get("owner") or "").strip()
        repo = str(params.get("repo_name") or "").strip()
        if owner and repo:
            return (owner, repo)
        repo_param = str(params.get("repo") or "").strip()
        if repo_param:
            parsed_owner, parsed_repo = self._parse_repo_identifier(repo_param)
            if parsed_owner and parsed_repo:
                return (parsed_owner, parsed_repo)
        base_config = self._base_config_getter()
        repo_url = (
            base_config.repo_url
            if base_config is not None and base_config.repo_url
            else self._default_repo_url
        )
        return self._parse_repo_identifier(repo_url)

    def _resolve_access_token(self, params: dict[str, Any]) -> str:
        token = str(params.get("access_token") or "").strip()
        if token:
            return token
        env_token = os.getenv("GITCODE_ACCESS_TOKEN")
        if env_token:
            return env_token.strip()
        base_config = self._base_config_getter()
        if base_config is not None:
            try:
                return str(base_config.resolve_gitcode_token() or "").strip()
            except Exception:
                pass
        return ""

    @staticmethod
    def _parse_string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
        if value is None:
            return default
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, list | tuple):
            return tuple(str(part).strip() for part in value if str(part).strip())
        return default

    @staticmethod
    def _parse_issue_numbers(value: Any) -> tuple[int, ...]:
        if value is None:
            return ()
        raw_parts: list[Any]
        if isinstance(value, str):
            raw_parts = [part.strip() for part in value.replace("，", ",").split(",")]
        elif isinstance(value, int):
            raw_parts = [value]
        elif isinstance(value, list | tuple):
            raw_parts = list(value)
        else:
            return ()

        numbers: list[int] = []
        for part in raw_parts:
            try:
                number = int(part)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in numbers:
                numbers.append(number)
        return tuple(numbers)

    async def process_gitcode_issues_once(
        self,
        params: dict[str, Any],
        model: Optional[Model] = None,
    ) -> dict[str, Any]:
        """Process GitCode issues once and create auto-harness tasks."""
        del model
        token = self._resolve_access_token(params)
        if not token:
            return {"error": "缺少 GitCode Access Token，请配置 gitcode.access_token 或 GITCODE_ACCESS_TOKEN"}

        owner, repo = self._resolve_target_repo(params)
        if not owner or not repo:
            return {"error": "无法解析 GitCode 仓库，请传入 repo=openJiuwen/jiuwenswarm"}

        try:
            max_issues = int(params.get("max_issues", 1))
        except (TypeError, ValueError):
            max_issues = 1
        max_issues = max(1, min(max_issues, 5))

        try:
            per_page = int(params.get("per_page", 20))
        except (TypeError, ValueError):
            per_page = 20
        per_page = max(1, min(per_page, 100))

        pipeline = str(params.get("pipeline") or META_EVOLVE_PIPELINE)
        issue_numbers = self._parse_issue_numbers(
            params.get("issue_numbers")
            or params.get("issues")
            or params.get("issue")
            or params.get("numbers")
        )
        if issue_numbers:
            max_issues = len(issue_numbers)
        try:
            start_interval_seconds = float(params.get("start_interval_seconds", 0) or 0)
        except (TypeError, ValueError):
            start_interval_seconds = 0.0
        if start_interval_seconds <= 0 and max_issues > 1:
            # openjiuwen auto_harness currently uses second-level timestamps
            # for some readonly worktree paths. Stagger immediate issue tasks
            # to avoid "worktree already exists" collisions.
            start_interval_seconds = 1.2
        options = IssueWatchOptions(
            owner=owner,
            repo=repo,
            issue_numbers=issue_numbers,
            labels=self._parse_string_tuple(params.get("labels"), ("auto-harness",)),
            exclude_labels=self._parse_string_tuple(
                params.get("exclude_labels"),
                ("blocked", "wontfix", "needs-discussion"),
            ),
            max_issues=max_issues,
            per_page=per_page,
            pipeline=pipeline,
            comment_on_start=bool(params.get("comment_on_start", False)),
            dry_run=bool(params.get("dry_run", False)),
            start_interval_seconds=start_interval_seconds,
            max_auto_difficulty=str(params.get("max_auto_difficulty") or "medium"),
        )
        client = GitCodeIssueClient(token=token)
        runner = GitCodeIssueRunner(
            client=client,
            state_store=self._issue_state_store,
            harness_service=self._harness_service,
        )
        return await runner.process_issues_once(options)

    async def list_gitcode_issue_states(self) -> dict[str, Any]:
        issues = []
        for issue in self._issue_state_store.list():
            enriched = dict(issue)
            task_id = str(enriched.get("task_id") or "")
            if task_id:
                task = self._task_store.get_task(task_id)
                if task is not None:
                    task_status = str(task.get("status") or "")
                    enriched["task_status"] = task_status
                    enriched["progress"] = await self._task_store.summarize_task_progress(task)
                    if enriched["progress"].get("pr_url"):
                        enriched["pr_url"] = enriched["progress"]["pr_url"]

                    # 轻量 reconcile：TaskStore 已是终态但 IssueState 仍显示进行中，
                    # 同步 IssueState 反映真实状态（不检查 PR，留待 fix reconcile 处理）
                    issue_status = str(issue.get("status") or "")
                    if issue_status in {"running", "task_created"} \
                        and task_status in {"success", "failed", "cancelled"}:
                        new_status = "completed_without_pr" if task_status == "success" else task_status
                        await self._issue_state_store.update(
                            issue.get("owner", ""),
                            issue.get("repo", ""),
                            issue.get("number", 0),
                            {"status": new_status, "task_status": task_status},
                        )
                        enriched["status"] = new_status
                else:
                    # 任务已不存在（被删除），标记以清理
                    enriched["task_status"] = "task_deleted"
                    enriched["reason"] = "任务记录已删除"
            issues.append(enriched)
        return {"issues": issues}

    async def delete_issue_states(self, params: dict[str, Any]) -> dict[str, Any]:
        """删除 issue 处理记录和运行日志，running 状态不允许删除。"""
        deleted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        issue_numbers = self._parse_issue_numbers(params.get("issue_numbers"))
        delete_completed = bool(params.get("delete_completed", False))
        delete_failed = bool(params.get("delete_failed", False))

        # 获取所有 issue 记录
        all_issues = self._issue_state_store.list()

        # 确定要删除的 issue
        targets: list[dict[str, Any]] = []
        if issue_numbers:
            # 指定编号删除
            for issue in all_issues:
                number = int(issue.get("number") or 0)
                if number in issue_numbers:
                    targets.append(issue)
        if delete_completed:
            # 删除所有已完成的记录
            terminal_statuses = {"success", "pr_created", "completed", "completed_without_pr"}
            for issue in all_issues:
                status = str(issue.get("status") or "")
                if status in terminal_statuses and issue not in targets:
                    targets.append(issue)
        if delete_failed:
            # 删除所有失败的记录
            failed_statuses = {"failed", "cancelled"}
            for issue in all_issues:
                status = str(issue.get("status") or "")
                if status in failed_statuses and issue not in targets:
                    targets.append(issue)

        # 执行删除
        for issue in targets:
            number = int(issue.get("number") or 0)
            owner = str(issue.get("owner") or "")
            repo = str(issue.get("repo") or "")
            task_id = str(issue.get("task_id") or "")
            status = str(issue.get("status") or "")

            # 检查是否正在运行
            if status in RUNNING_STATUSES:
                # 如果 task_id 引用的任务已不存在，说明状态是残留的，允许删除
                if task_id and self._task_store.get_task(task_id) is None:
                    pass
                else:
                    rejected.append({
                        "issue": number,
                        "reason": f"任务正在执行中 (status: {status})，运行中的任务不支持删除",
                    })
                    continue

            # 检查任务状态（如果有 task_id）
            if task_id:
                task = self._task_store.get_task(task_id)
                if task and str(task.get("status") or "") in RUNNING_STATUSES:
                    rejected.append({
                        "issue": number,
                        "reason": f"任务正在执行中，运行中的任务不支持删除",
                    })
                    continue

            # 删除 IssueStateStore 记录
            try:
                await self._issue_state_store.delete(owner, repo, number)
            except Exception:
                pass  # Best effort

            # 删除 TaskStore 任务记录
            if task_id:
                try:
                    await self._task_store.delete_task(task_id)
                except Exception:
                    pass  # Best effort

            deleted.append({
                "issue": number,
                "task_id": task_id if task_id else None,
            })

        return {"deleted": deleted, "rejected": rejected}

    async def refresh_issue_matrix(self, params: dict[str, Any]) -> dict[str, Any]:
        """Refresh issue matrix with incremental updates.

        Fetches all open issues, compares with cached matrix, and updates:
        - New issues: analyze and add to matrix
        - Closed issues: remove from matrix
        - Updated issues: re-analyze if content changed

        If force_refresh=False and cache exists, returns cache directly (no API call).
        Labels filter is applied on cached/API results without forcing refresh.
        """
        token = self._resolve_access_token(params)
        if not token:
            return {"error": "缺少 GitCode Access Token，请配置 gitcode.access_token 或 GITCODE_ACCESS_TOKEN"}

        owner, repo = self._resolve_target_repo(params)
        if not owner or not repo:
            return {"error": "无法解析 GitCode 仓库，请传入 repo=openJiuwen/jiuwenswarm"}

        force_refresh = bool(params.get("force_refresh", False))
        page = int(params.get("page", 1) or 1)
        page = max(1, page)
        per_page = int(params.get("per_page", 50) or 50)
        per_page = max(1, min(per_page, 100))

        # Parse labels filter (comma-separated, lowercase for matching)
        labels_filter = self._parse_string_tuple(params.get("labels"), ())
        labels_filter_lower = {label.lower() for label in labels_filter}

        # Load cached matrix
        cached_data = self._issue_matrix_store.load(owner, repo)
        cached_matrix = cached_data.get("matrix", [])

        # If not forcing refresh and cache exists, return cache directly (with label filtering)
        if not force_refresh and cached_matrix:
            # Apply labels filter on cached results
            filtered_matrix = cached_matrix
            if labels_filter_lower:
                filtered_matrix = [
                    entry for entry in cached_matrix
                    if any(label.lower() in labels_filter_lower for label in (entry.get("labels") or []))
                ]

            # Build statistics from filtered cache
            difficulty_counts: dict[str, int] = {}
            for entry in filtered_matrix:
                diff = entry.get("difficulty", "unclear")
                difficulty_counts[diff] = difficulty_counts.get(diff, 0) + 1

            # Paginate filtered results
            total = len(filtered_matrix)
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_matrix = filtered_matrix[start_idx:end_idx]

            return {
                "owner": owner,
                "repo": repo,
                "total": total,
                "added": 0,
                "removed": 0,
                "updated": 0,
                "difficulty_counts": difficulty_counts,
                "matrix": page_matrix,
                "has_more": end_idx < total,
                "page": page,
                "per_page": per_page,
                "cached": True,
                "labels_filter": list(labels_filter) if labels_filter else [],
            }

        # Force refresh: call API and update cache
        client = GitCodeIssueClient(token=token)

        # Fetch all open issues from GitCode
        api_issues = client.list_all_issues(owner=owner, repo=repo, state="open")

        # Build cached issue dict for comparison
        cached_by_number: dict[int, dict[str, Any]] = {
            entry.get("number"): entry for entry in cached_matrix if entry.get("number")
        }

        # Three-set comparison
        api_numbers = set(api_issues.keys())
        cached_numbers = set(cached_by_number.keys())

        added_numbers = api_numbers - cached_numbers
        removed_numbers = cached_numbers - api_numbers
        common_numbers = api_numbers & cached_numbers

        # Process additions
        added_entries: list[dict[str, Any]] = []
        for number in added_numbers:
            issue = api_issues[number]
            assessment = GitCodeIssueRunner.assess_issue_difficulty(issue)
            entry = {
                "number": number,
                "title": issue.title,
                "body": issue.body[:200] if issue.body else "",  # Truncate for storage
                "labels": list(issue.labels),
                "difficulty": assessment.get("level", "unclear"),
                "updated_at": str(issue.raw.get("updated_at") or ""),
                "first_seen": _now_iso(),
                "last_analyzed": _now_iso(),
            }
            added_entries.append(entry)

        # Process updates (check for content changes)
        updated_entries: list[dict[str, Any]] = []
        for number in common_numbers:
            issue = api_issues[number]
            cached_entry = cached_by_number[number]

            # Check if content changed (title, labels, or updated_at)
            if force_refresh or _has_content_change(issue, cached_entry):
                assessment = GitCodeIssueRunner.assess_issue_difficulty(issue)
                cached_entry["title"] = issue.title
                cached_entry["body"] = issue.body[:200] if issue.body else ""
                cached_entry["labels"] = list(issue.labels)
                cached_entry["difficulty"] = assessment.get("level", "unclear")
                cached_entry["updated_at"] = str(issue.raw.get("updated_at") or "")
                cached_entry["last_analyzed"] = _now_iso()
                updated_entries.append(cached_entry)

        # Build new matrix
        new_matrix: list[dict[str, Any]] = []
        for number in api_numbers:
            if number in added_numbers:
                # Find the added entry
                for entry in added_entries:
                    if entry.get("number") == number:
                        new_matrix.append(entry)
                        break
            elif number in common_numbers:
                # Use cached entry (possibly updated)
                new_matrix.append(cached_by_number[number])

        # Sort by updated_at descending (most recent first)
        new_matrix.sort(key=lambda e: e.get("updated_at") or "", reverse=True)

        # Save updated matrix (full matrix, no label filtering in storage)
        new_data = {
            "total_open": len(new_matrix),
            "matrix": new_matrix,
        }
        self._issue_matrix_store.save(owner, repo, new_data)

        # Apply labels filter on fresh results (after saving to cache)
        filtered_matrix = new_matrix
        if labels_filter_lower:
            filtered_matrix = [
                entry for entry in new_matrix
                if any(label.lower() in labels_filter_lower for label in (entry.get("labels") or []))
            ]

        # Build statistics from filtered results
        difficulty_counts: dict[str, int] = {}
        for entry in filtered_matrix:
            diff = entry.get("difficulty", "unclear")
            difficulty_counts[diff] = difficulty_counts.get(diff, 0) + 1

        # Paginate filtered results
        total = len(filtered_matrix)
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_matrix = filtered_matrix[start_idx:end_idx]

        return {
            "owner": owner,
            "repo": repo,
            "total": total,
            "added": len(added_numbers),
            "removed": len(removed_numbers),
            "updated": len(updated_entries),
            "difficulty_counts": difficulty_counts,
            "matrix": page_matrix,
            "has_more": end_idx < total,
            "page": page,
            "per_page": per_page,
            "cached": False,
            "labels_filter": list(labels_filter) if labels_filter else [],
        }


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _has_content_change(issue: Any, cached: dict[str, Any]) -> bool:
    """Check if issue content has changed since last analysis."""
    if issue.title != cached.get("title"):
        return True
    cached_labels = set(cached.get("labels") or [])
    current_labels = set(issue.labels)
    if cached_labels != current_labels:
        return True
    # Check updated_at if available
    cached_updated = cached.get("updated_at") or ""
    current_updated = str(issue.raw.get("updated_at") or "")
    if current_updated and cached_updated and current_updated != cached_updated:
        return True
    return False
