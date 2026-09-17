# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.gitcode_issue_client import GitCodeIssue
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.issue_runner import (
    GitCodeIssueRunner,
    IssueWatchOptions,
)
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.issue_state_store import IssueStateStore


class FakeGitCodeClient:
    def __init__(self, issues: list[GitCodeIssue], pulls: dict[int, list[dict[str, Any]]] | None = None):
        self.issues = issues
        self.pulls = pulls or {}
        self.comments: list[dict[str, Any]] = []

    def list_issues(self, **_kwargs):
        return self.issues

    def get_issue(self, *, number: int, **_kwargs):
        for issue in self.issues:
            if issue.number == number:
                return issue
        raise RuntimeError(f"missing issue #{number}")

    def list_issue_pull_requests(self, *, number: int, **_kwargs):
        return self.pulls.get(number, [])

    @staticmethod
    def list_pull_requests(**_kwargs):
        return []

    def create_issue_comment(self, **kwargs):
        self.comments.append(kwargs)
        return {"id": len(self.comments)}


class FakeHarnessService:
    def __init__(self):
        self.queries: list[str] = []
        self.optimization_tasks: list[Any] = []
        self.tasks: dict[str, dict[str, Any]] = {}

    async def run_task(self, query: str, model=None, pipeline=None, optimization_task=None, repo_url=""):
        self.queries.append(query)
        self.optimization_tasks.append(optimization_task)
        task_id = f"sch_{len(self.queries):03d}"
        self.tasks[task_id] = {"task_id": task_id, "status": "running", "pipeline": pipeline}
        return {"task_id": task_id, "status": "running", "message": "started"}

    async def get_scheduled_task_status(self, task_id: str):
        return self.tasks.get(task_id)


def _issue(number: int, labels: tuple[str, ...] = ("auto-harness",)) -> GitCodeIssue:
    return GitCodeIssue(
        number=number,
        title=f"Issue {number}",
        body=(
            "please fix a clearly scoped bug in the local command parser. "
            "The failure is reproducible with the existing unit test and "
            "the expected change is limited to one implementation file and "
            "one matching test file."
        ),
        html_url=f"https://gitcode.com/openJiuwen/jiuwenswarm/issues/{number}",
        labels=labels,
        raw={},
    )


@pytest.mark.asyncio
async def test_process_issues_once_creates_one_task_for_eligible_issue(tmp_path: Path):
    client = FakeGitCodeClient([_issue(1)])
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(IssueWatchOptions(owner="openJiuwen", repo="jiuwenswarm"))

    assert result["fetched"] == 1
    assert result["started"][0]["task_id"] == "sch_001"
    assert "Issue #1" in service.queries[0]
    assert service.optimization_tasks[0].issue_ref == "#1"


@pytest.mark.asyncio
async def test_process_issues_once_skips_issue_with_existing_open_pr(tmp_path: Path):
    client = FakeGitCodeClient(
        [_issue(2)],
        pulls={
            2: [
                {
                    "number": 9,
                    "state": "open",
                    "html_url": "https://gitcode.com/openJiuwen/jiuwenswarm/pull/9"
                }
            ]
        }
    )
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(IssueWatchOptions(owner="openJiuwen", repo="jiuwenswarm"))

    assert result["started"] == []
    assert "issue 已有关联 PR" in result["skipped"][0]["reason"]
    assert service.queries == []


@pytest.mark.asyncio
async def test_process_issues_once_dry_run_does_not_create_task(tmp_path: Path):
    client = FakeGitCodeClient([_issue(3)])
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(IssueWatchOptions(owner="openJiuwen", repo="jiuwenswarm", dry_run=True))

    assert result["started"][0]["status"] == "dry_run"
    assert service.queries == []


@pytest.mark.asyncio
async def test_process_issues_once_does_not_duplicate_existing_task(tmp_path: Path):
    state = IssueStateStore(tmp_path)
    await state.update("openJiuwen", "jiuwenswarm", 4, {"status": "task_created", "task_id": "sch_old"})
    client = FakeGitCodeClient([_issue(4)])
    service = FakeHarnessService()
    service.tasks["sch_old"] = {"task_id": "sch_old", "status": "running"}
    runner = GitCodeIssueRunner(client=client, state_store=state, harness_service=service)

    result = await runner.process_issues_once(IssueWatchOptions(owner="openJiuwen", repo="jiuwenswarm"))

    assert result["started"] == []
    assert result["skipped"][0]["reason"] == "task_created"


@pytest.mark.asyncio
async def test_process_issues_once_accepts_explicit_issue_numbers_without_required_label(tmp_path: Path):
    client = FakeGitCodeClient([
        _issue(10, labels=("bug",)),
        _issue(11, labels=("bug", "priority")),
    ])
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(
        IssueWatchOptions(
            owner="openJiuwen",
            repo="jiuwenswarm",
            issue_numbers=(10, 11),
            labels=("auto-harness",),
        )
    )

    assert result["fetched"] == 2
    assert [item["number"] for item in result["started"]] == [10, 11]
    assert "Issue #10" in service.queries[0]
    assert "Issue #11" in service.queries[1]
    assert [task.issue_ref for task in service.optimization_tasks] == ["#10", "#11"]
    assert "PR 正文必须写明修改方案、验证结果、风险和回滚建议" in service.queries[0]


@pytest.mark.asyncio
async def test_process_issues_once_staggers_multiple_started_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.auto_harness.issue_fix.issue_runner.asyncio.sleep",
        fake_sleep,
    )
    client = FakeGitCodeClient([
        _issue(20, labels=("bug",)),
        _issue(21, labels=("bug",)),
    ])
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(
        IssueWatchOptions(
            owner="openJiuwen",
            repo="jiuwenswarm",
            issue_numbers=(20, 21),
            start_interval_seconds=1.2,
        )
    )

    assert [item["number"] for item in result["started"]] == [20, 21]
    assert sleeps == [1.2]


@pytest.mark.asyncio
async def test_process_issues_once_skips_issue_with_merged_pr(tmp_path: Path):
    """list_issue_pull_requests returns merged PRs → issue should be skipped."""
    client = FakeGitCodeClient(
        [_issue(12, labels=("bug",))],
        pulls={
            12: [
                {
                    "number": 20,
                    "state": "merged",
                    "html_url": "https://gitcode.com/openJiuwen/jiuwenswarm/pull/20",
                }
            ]
        },
    )
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(
        IssueWatchOptions(
            owner="openJiuwen",
            repo="jiuwenswarm",
            issue_numbers=(12,),
        )
    )

    assert result["started"] == []
    assert "issue 已有关联 PR" in result["skipped"][0]["reason"]
    assert service.queries == []


@pytest.mark.asyncio
async def test_process_issues_once_skips_explicit_issue_with_existing_open_pr(tmp_path: Path):
    client = FakeGitCodeClient(
        [_issue(12, labels=("bug",))],
        pulls={
            12: [
                {
                    "number": 20,
                    "state": "open",
                    "html_url": "https://gitcode.com/openJiuwen/jiuwenswarm/pull/20"
                }
            ]
        }
    )
    service = FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=client,
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = await runner.process_issues_once(
        IssueWatchOptions(
            owner="openJiuwen",
            repo="jiuwenswarm",
            issue_numbers=(12,),
        )
    )

    assert result["started"] == []
    assert "issue 已有关联 PR" in result["skipped"][0]["reason"]
    assert service.queries == []
