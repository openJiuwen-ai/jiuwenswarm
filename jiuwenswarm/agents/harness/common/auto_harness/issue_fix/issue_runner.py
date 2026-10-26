# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""GitCode issue ingestion and auto-harness task orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from openjiuwen.auto_harness.pipelines import META_EVOLVE_PIPELINE

from .code_rules import format_code_rules_prompt
from .gitcode_issue_client import GitCodeIssue, GitCodeIssueClient
from .issue_state_store import IssueStateStore
from .task_factory import build_issue_fix_task

logger = logging.getLogger(__name__)


class AutoHarnessTaskService(Protocol):
    async def run_task(
        self,
        query: str,
        model: Any = None,
        pipeline: Optional[str] = None,
        optimization_task: Any = None,
        repo_url: str = "",
    ) -> dict[str, Any]:
        ...

    async def get_scheduled_task_status(self, task_id: str) -> Optional[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class IssueWatchOptions:
    owner: str
    repo: str
    issue_numbers: tuple[int, ...] = ()
    labels: tuple[str, ...] = ("auto-harness",)
    exclude_labels: tuple[str, ...] = ("blocked", "wontfix", "needs-discussion")
    max_issues: int = 1
    per_page: int = 20
    pipeline: str = META_EVOLVE_PIPELINE
    comment_on_start: bool = False
    dry_run: bool = False
    start_interval_seconds: float = 0.0
    max_auto_difficulty: str = "medium"


class GitCodeIssueRunner:
    """Turns eligible GitCode issues into one-time auto-harness tasks."""

    TERMINAL_RECORD_STATUSES = {"pr_created", "completed", "skipped", "needs_human"}
    IN_FLIGHT_RECORD_STATUSES = {"running", "task_created"}
    RECONCILE_RECORD_STATUSES = IN_FLIGHT_RECORD_STATUSES | {"completed_without_pr", "failed", "skipped"}
    _DIFFICULTY_ORDER = {"low": 1, "medium": 2, "high": 3, "unclear": 4}

    def __init__(
        self,
        *,
        client: GitCodeIssueClient,
        state_store: IssueStateStore,
        harness_service: AutoHarnessTaskService,
    ) -> None:
        self._client = client
        self._state_store = state_store
        self._harness_service = harness_service

    @staticmethod
    def build_issue_fix_query(issue: GitCodeIssue, *, owner: str, repo: str) -> str:
        issue_url = issue.html_url or f"https://gitcode.com/{owner}/{repo}/issues/{issue.number}"
        labels = ", ".join(issue.labels) if issue.labels else "(none)"
        code_rules = format_code_rules_prompt()
        return (
            f"请自动修复 GitCode Issue #{issue.number}。\n\n"
            f"仓库: {owner}/{repo}\n"
            f"Issue URL: {issue_url}\n"
            f"标题: {issue.title}\n"
            f"标签: {labels}\n\n"
            f"Issue 内容:\n{issue.body or '(empty)'}\n\n"
            "执行要求:\n"
            "1. 只修改与该 issue 直接相关的代码和测试。\n"
            "2. 优先补充或更新能够复现问题的测试。\n"
            "3. 运行仓库现有的相关测试或最小可行验证命令。\n"
            f"4. 为该 issue 单独提交一个 PR，PR 标题包含 issue #{issue.number}。\n"
            f"5. PR 正文必须写明修改方案、验证结果、风险和回滚建议，"
            f"并包含“对应 Issue: #{issue.number}”和单独一行“Closes #{issue.number}”，"
            "便于 committer 审核并让 GitCode 自动关联 issue。\n"
            "6. 如果 issue 信息不足以安全修复，请停止并在日志里说明缺失信息。\n"
            "7. 严禁把本 issue 修复任务扩展成 auto-harness 框架能力建设、"
            "调度器改造、IssueStateStore 等流程优化；除非 issue 内容明确要求修改这些文件。\n"
            "8. 如果发现需要改进 auto-harness 流程才能完成自动化，请在日志中记录为后续建议，"
            "本 PR 仍必须只包含当前 issue 的产品代码和测试修复。\n"
            "9. 修复前必须提取 issue 中点名的文件、函数或方法；最终 diff 必须命中这些目标。"
            "如果已有分支/提交修复的是相似但非同一目标，禁止复用。\n"
            "10. PR 文案必须基于最终 diff 撰写，函数名、文件名和验证结论不能写成中间尝试内容。\n"
            "11. 测试代码不得直接访问以单下划线开头的受保护或私有成员；"
            "12. 测试代码也需要满足编程规范约束；"
            "如果 issue 指向受保护或私有方法，必须通过公开 API、用户可观察行为"
            "或模块级公共函数间接验证。\n\n"
            f"{code_rules}"
        )

    @staticmethod
    def _normalized_label_set(issue: GitCodeIssue) -> set[str]:
        return {label.strip().lower() for label in issue.labels if label.strip()}

    @staticmethod
    def _format_pull_request_info(pulls: list[dict[str, Any]]) -> str:
        """Format PR info for display."""
        if not pulls:
            return ""
        info_parts: list[str] = []
        for pull in pulls[:3]:  # Show max 3 PRs
            state = str(pull.get("state") or pull.get("status") or "unknown").lower()
            url = str(pull.get("html_url") or pull.get("url") or "")
            number = pull.get("number") or pull.get("id") or ""
            merged = bool(pull.get("merged") or pull.get("merge_status") == "merged")
            status = "merged" if merged or state == "merged" else state
            if url:
                info_parts.append(f"PR #{number} ({status}): {url}")
        return "; ".join(info_parts)

    @classmethod
    def assess_issue_difficulty(cls, issue: GitCodeIssue) -> dict[str, Any]:
        """Conservative, explainable issue-fix difficulty assessment.

        The goal is not to perfectly estimate engineering effort. It is to avoid
        handing poorly scoped or high-blast-radius work to automation.
        """
        title = issue.title or ""
        body = issue.body or ""
        labels = cls._normalized_label_set(issue)
        text = f"{title}\n{body}"
        lower = text.lower()
        reasons: list[str] = []
        score = 0

        body_len = len(body.strip())
        if body_len < 80:
            score += 4
            reasons.append("issue 描述过短，缺少可执行细节")
        elif body_len < 220:
            score += 1
            reasons.append("issue 描述较短，可能需要额外判断")

        unclear_markers = (
            "待补充",
            "不清楚",
            "无法复现",
            "偶现",
            "看情况",
            "需要讨论",
            "tbd",
            "todo",
            "not sure",
            "unclear",
            "unknown",
        )
        if any(marker in lower for marker in unclear_markers):
            score += 4
            reasons.append("描述包含不确定/待讨论信号")

        high_impact_markers = (
            "架构",
            "重构",
            "安全",
            "漏洞",
            "权限",
            "认证",
            "鉴权",
            "协议",
            "兼容",
            "迁移",
            "数据库",
            "并发",
            "性能优化",
            "token消耗",
            "全局",
            "framework",
            "architecture",
            "refactor",
            "security",
            "auth",
            "permission",
            "migration",
            "concurrency",
            "performance",
        )
        impact_hits = [marker for marker in high_impact_markers if marker in lower]
        if impact_hits:
            score += min(5, len(impact_hits) * 2)
            reasons.append("疑似影响面较大: " + ", ".join(impact_hits[:4]))

        complex_markers = (
            "设计思路",
            "新增",
            "支持",
            "多端",
            "前端配置",
            "后端",
            "api",
            "sdk",
            "mcp",
            "大模型",
            "上下文",
            "feature",
            "frontend",
            "backend",
            "protocol",
            "integration",
        )
        complex_hits = [marker for marker in complex_markers if marker in lower]
        if len(complex_hits) >= 3:
            score += 2
            reasons.append("涉及多个功能点/模块: " + ", ".join(complex_hits[:4]))

        if "feature" in labels or "enhancement" in labels:
            score += 2
            reasons.append("feature 类 issue，默认比 bug 修复风险更高")
        if labels.intersection({"bug", "good-first-issue", "easy"}):
            score -= 1
        if labels.intersection({"needs-discussion", "blocked", "wontfix"}):
            score += 5
            reasons.append("存在阻塞/需讨论标签")

        mentioned_files = re.findall(
            r"(?:^|[\s`\"'（(])((?:jiuwenswarm|tests|openjiuwen|src|packages)/[^\s`\"'，,。)）]+)",
            text,
        )
        if len(set(mentioned_files)) >= 5:
            score += 2
            reasons.append(f"描述涉及较多文件路径: {len(set(mentioned_files))} 个")

        if score >= 7:
            level = "high"
        elif score >= 3:
            level = "medium"
        else:
            level = "low"
        if body_len < 80 or any(marker in lower for marker in ("不清楚", "unclear", "unknown", "待补充")):
            level = "unclear" if score >= 4 else level
        if not reasons:
            reasons.append("描述较明确，影响面看起来可控")
        return {
            "level": level,
            "score": score,
            "reasons": reasons,
            "needs_human": cls._DIFFICULTY_ORDER.get(level, 99) > cls._DIFFICULTY_ORDER["medium"],
        }

    @classmethod
    def _is_difficulty_within_limit(cls, assessment: dict[str, Any], max_level: str) -> bool:
        level = str(assessment.get("level") or "unclear")
        return cls._DIFFICULTY_ORDER.get(level, 99) <= cls._DIFFICULTY_ORDER.get(max_level, 2)

    @staticmethod
    def _extract_pull_request_number_from_url(url: str) -> int | None:
        match = re.search(r"/(?:pulls|pull_requests|merge_requests)/(\d+)(?:\D|$)", url)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @classmethod
    def _build_pull_request_record_from_url(cls, url: str) -> dict[str, Any] | None:
        url = url.strip()
        if not url.startswith("https://gitcode.com/"):
            return None
        if "/merge_requests/new?" in url:
            return None
        if not re.search(r"/(?:pulls|pull_requests|merge_requests)/\d+", url):
            return None
        result: dict[str, Any] = {"html_url": url, "url": url}
        number = cls._extract_pull_request_number_from_url(url)
        if number is not None:
            result["number"] = number
        return result

    @classmethod
    def _find_pull_request_in_task_log(cls, task: Optional[dict[str, Any]]) -> dict[str, Any] | None:
        """Find a real PR/MR URL in the latest scheduled task log."""
        if not task:
            return None
        history = task.get("execution_history") or []
        for record in sorted(
            history,
            key=lambda item: item.get("completed_at") or item.get("started_at") or "",
            reverse=True,
        ):
            log_path = Path(str(record.get("log_path") or ""))
            if not log_path.exists():
                continue
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text_parts = [
                    str(entry.get("content") or ""),
                    str(entry.get("message") or ""),
                ]
                text_parts.extend(str(item) for item in entry.get("messages") or [])
                for text in text_parts:
                    for url in re.findall(r"https://gitcode\.com/[^\s)>\"]+", text):
                        pr = cls._build_pull_request_record_from_url(url)
                        if pr:
                            return pr
        return None

    async def _record_skipped_issue(
        self,
        options: IssueWatchOptions,
        issue: GitCodeIssue,
        reason: str,
    ) -> dict[str, Any]:
        await self._state_store.update(
            options.owner,
            options.repo,
            issue.number,
            {
                "status": "skipped",
                "title": issue.title,
                "reason": reason,
            },
        )
        return {"issue": issue.number, "status": "skipped", "reason": reason}

    async def _record_issue_needs_human(
        self,
        options: IssueWatchOptions,
        issue: GitCodeIssue,
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        reason = "需要人工修复: " + "；".join(assessment.get("reasons") or [])
        await self._state_store.update(
            options.owner,
            options.repo,
            issue.number,
            {
                "status": "needs_human",
                "title": issue.title,
                "reason": reason,
                "difficulty": assessment,
                "human_label": "needs-human",
            },
        )
        return {
            "issue": issue.number,
            "status": "needs_human",
            "reason": reason,
            "difficulty": assessment,
            "human_label": "needs-human",
        }

    async def reconcile(self, options: IssueWatchOptions) -> list[dict[str, Any]]:
        """Refresh stored issue records using task status and linked PRs."""

        results: list[dict[str, Any]] = []
        for record in self._state_store.list():
            if record.get("owner") != options.owner or record.get("repo") != options.repo:
                continue
            status = str(record.get("status") or "")
            if status not in self.RECONCILE_RECORD_STATUSES:
                continue
            number = int(record.get("number") or 0)
            if number <= 0:
                continue

            # 对于已跳过记录，检查是否已在 GitCode 上关闭
            if status == "skipped":
                try:
                    issue = self._client.get_issue(owner=options.owner, repo=options.repo, number=number)
                    if issue.raw.get("state") == "closed":
                        updated = await self._state_store.update(
                            options.owner, options.repo, number,
                            {"status": "skipped", "reason": "issue已关闭"},
                        )
                        results.append(updated)
                except Exception:
                    logger.warning(
                        f"[GitCodeIssueRunner] get issue failed, issue number: {number}"
                    )
                    pass
                continue

            task_id = str(record.get("task_id") or "")
            task = await self._harness_service.get_scheduled_task_status(task_id) if task_id else None
            task_status = str((task or {}).get("status") or "")
            pulls = self._client.list_issue_pull_requests(owner=options.owner, repo=options.repo, number=number)
            fallback_pr = self._find_pull_request_in_task_log(task)
            if pulls or fallback_pr:
                first = pulls[0] if pulls else (fallback_pr or {})
                updated = await self._state_store.update(
                    options.owner,
                    options.repo,
                    number,
                    {
                        "status": "pr_created",
                        "task_status": task_status,
                        "pr_source": "gitcode_issue_links" if pulls else "auto_harness_log",
                        "pr_number": first.get("number") or first.get("id"),
                        "pr_url": first.get("html_url") or first.get("url"),
                    },
                )
                results.append(updated)
            elif task_status in {"success", "failed", "cancelled"}:
                updated = await self._state_store.update(
                    options.owner,
                    options.repo,
                    number,
                    {
                        "status": "completed_without_pr" if task_status == "success" else task_status,
                        "task_status": task_status,
                    },
                )
                results.append(updated)
            elif task_id and task is None:
                # 任务已被删除（日志/TaskStore 记录已清除），清理 issue 状态
                await self._state_store.delete(options.owner, options.repo, number)
                results.append({"deleted": True, "number": number})
        return results

    async def process_issues_once(self, options: IssueWatchOptions) -> dict[str, Any]:
        """Fetch eligible issues and create one-time auto-harness tasks."""

        reconciled = await self.reconcile(options)
        started: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        if options.issue_numbers:
            issues = []
            for number in options.issue_numbers:
                issue = self._client.get_issue(owner=options.owner, repo=options.repo, number=number)
                if issue.raw.get("state") == "closed":
                    skipped.append(await self._record_skipped_issue(options, issue, "issue已关闭"))
                else:
                    issues.append(issue)
        else:
            issues = self._client.list_issues(
                owner=options.owner,
                repo=options.repo,
                state="open",
                labels=list(options.labels),
                per_page=options.per_page,
            )
        required_labels = {label.lower() for label in options.labels if label}
        blocked_labels = {label.lower() for label in options.exclude_labels if label}
        max_issues = len(options.issue_numbers) if options.issue_numbers else options.max_issues

        for index, issue in enumerate(issues):
            if len(started) >= max_issues:
                break
            if issue.number <= 0:
                continue

            labels = self._normalized_label_set(issue)
            if not options.issue_numbers and required_labels and not required_labels.issubset(labels):
                skipped.append(await self._record_skipped_issue(options, issue, "missing required labels"))
                continue
            if blocked_labels.intersection(labels):
                skipped.append(await self._record_skipped_issue(options, issue, "excluded label present"))
                continue

            previous = self._state_store.get(options.owner, options.repo, issue.number)
            previous_status = str((previous or {}).get("status") or "")
            if previous_status in self.TERMINAL_RECORD_STATUSES | self.IN_FLIGHT_RECORD_STATUSES:
                skipped.append({"issue": issue.number, "status": "skipped", "reason": previous_status})
                continue
            if previous_status == "completed_without_pr":
                skipped.append({"issue": issue.number, "status": "skipped", "reason": previous_status})
                continue

            pulls = self._client.list_issue_pull_requests(
                owner=options.owner,
                repo=options.repo,
                number=issue.number,
            )

            if pulls:
                pr_info = self._format_pull_request_info(pulls)
                skipped.append(await self._record_skipped_issue(
                    options,
                    issue,
                    f"issue 已有关联 PR: {pr_info}",
                ))
                continue

            assessment = self.assess_issue_difficulty(issue)
            if not self._is_difficulty_within_limit(assessment, options.max_auto_difficulty):
                skipped.append(await self._record_issue_needs_human(options, issue, assessment))
                continue

            query = self.build_issue_fix_query(issue, owner=options.owner, repo=options.repo)
            repo_url = f"https://gitcode.com/{options.owner}/{options.repo}.git"
            task = build_issue_fix_task(issue.number, query)
            if options.dry_run:
                started.append({
                    "issue": issue.number,
                    "status": "dry_run",
                    "query": query,
                    "difficulty": assessment,
                })
                continue

            record = await self._state_store.update(
                options.owner,
                options.repo,
                issue.number,
                {
                    "status": "running",
                    "title": issue.title,
                    "issue_url": issue.html_url,
                    "labels": list(issue.labels),
                    "pipeline": options.pipeline,
                    "difficulty": assessment,
                },
            )
            result = await self._harness_service.run_task(
                query,
                pipeline=options.pipeline,
                optimization_task=task,
                repo_url=repo_url,
            )
            updates = {
                "status": "task_created",
                "task_id": result.get("task_id"),
                "task_message": result.get("message"),
            }
            if result.get("error"):
                updates.update({"status": "failed", "last_error": result.get("error")})
            record = await self._state_store.update(options.owner, options.repo, issue.number, updates)
            if options.comment_on_start and not result.get("error"):
                self._client.create_issue_comment(
                    owner=options.owner,
                    repo=options.repo,
                    number=issue.number,
                    body=f"auto-harness 已开始处理该 issue，任务 ID: {result.get('task_id')}",
                )
            started.append(record)
            if (
                options.start_interval_seconds > 0
                and index < len(issues) - 1
                and len(started) < max_issues
            ):
                await asyncio.sleep(options.start_interval_seconds)

        return {
            "owner": options.owner,
            "repo": options.repo,
            "fetched": len(issues),
            "started": started,
            "skipped": skipped,
            "reconciled": reconciled,
        }
