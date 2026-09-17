import asyncio
from pathlib import Path

from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.gitcode_issue_client import GitCodeIssue
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.issue_runner import (
    GitCodeIssueRunner,
    IssueWatchOptions,
)
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.issue_state_store import IssueStateStore
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.run_progress import (
    enrich_issue_fix_progress,
    infer_issue_fix_skipped_stages,
)
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.task_factory import (
    build_issue_fix_task,
)
from jiuwenswarm.agents.harness.common.auto_harness.run_log_status import determine_pipeline_status_from_log
from jiuwenswarm.agents.harness.common.auto_harness.task_store import TaskStore


def _issue(number: int, title: str, body: str, labels: tuple[str, ...] = ("bug",)) -> GitCodeIssue:
    return GitCodeIssue(
        number=number,
        title=title,
        body=body,
        html_url=f"https://gitcode.com/openJiuwen/jiuwenswarm/issues/{number}",
        labels=labels,
        raw={},
    )


class _FakeClient:
    def __init__(self, issues):
        self._issues = {issue.number: issue for issue in issues}

    def get_issue(self, *, owner, repo, number):
        return self._issues[number]

    def list_issues(self, **_kwargs):
        return list(self._issues.values())

    @staticmethod
    def list_issue_pull_requests(**_kwargs):
        return []

    @staticmethod
    def list_pull_requests(**_kwargs):
        return []


class _FakeHarnessService:
    def __init__(self):
        self.queries = []
        self.optimization_tasks = []

    async def run_task(self, query: str, model=None, pipeline=None, optimization_task=None, repo_url=""):
        self.queries.append(query)
        self.optimization_tasks.append(optimization_task)
        return {"task_id": "sch_fake", "message": "started"}

    async def get_scheduled_task_status(self, task_id: str):
        return None


def test_assess_issue_difficulty_marks_unclear_or_high_for_human():
    unclear = _issue(1, "偶现问题", "待补充，暂时不清楚如何复现", ("bug",))
    result = GitCodeIssueRunner.assess_issue_difficulty(unclear)
    assert result["level"] == "unclear"
    assert result["needs_human"] is True

    high = _issue(
        2,
        "MCP 支持前端配置",
        "新增 MCP 前端配置、后端 API、协议兼容和多模块集成，需要设计思路。",
        ("feature", "sig/jiuwenclaw"),
    )
    result = GitCodeIssueRunner.assess_issue_difficulty(high)
    assert result["level"] in {"high", "unclear"}


def test_process_issues_once_skips_hard_issue_as_needs_human(tmp_path: Path):
    hard_issue = _issue(
        494,
        "MCP 支持前端配置",
        "新增 MCP 前端配置、后端 API、协议兼容和多模块集成，需要设计思路。",
        ("feature", "sig/jiuwenclaw"),
    )
    service = _FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=_FakeClient([hard_issue]),
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = asyncio.run(
        runner.process_issues_once(
            IssueWatchOptions(
                owner="openJiuwen",
                repo="jiuwenswarm",
                issue_numbers=(494,),
                labels=(),
                max_auto_difficulty="medium",
            )
        )
    )

    assert result["started"] == []
    assert result["skipped"][0]["status"] == "needs_human"
    assert result["skipped"][0]["human_label"] == "needs-human"
    assert service.queries == []


def test_process_issues_once_starts_medium_or_lower_issue(tmp_path: Path):
    easy_issue = _issue(
        1266,
        "InstanceLock.release Windows NameError",
        "在 Windows 下调用 InstanceLock.release 会出现 NameError。复现步骤明确，只需修复异常变量名并补充单测。",
        ("bug", "sig/jiuwenclaw"),
    )
    service = _FakeHarnessService()
    runner = GitCodeIssueRunner(
        client=_FakeClient([easy_issue]),
        state_store=IssueStateStore(tmp_path),
        harness_service=service,
    )

    result = asyncio.run(
        runner.process_issues_once(
            IssueWatchOptions(
                owner="openJiuwen",
                repo="jiuwenswarm",
                issue_numbers=(1266,),
                labels=(),
                max_auto_difficulty="medium",
            )
        )
    )

    assert result["started"][0]["task_id"] == "sch_fake"
    assert "GitCode Issue #1266" in service.queries[0]
    assert service.optimization_tasks[0].issue_ref == "#1266"


def test_issue_fix_query_includes_repository_code_rules():
    issue = _issue(
        1277,
        "修复一个明确的小问题",
        "复现步骤明确，修改范围限定在一个函数和一个单测。",
        ("bug",),
    )

    query = GitCodeIssueRunner.build_issue_fix_query(
        issue,
        owner="openJiuwen",
        repo="jiuwenswarm",
    )

    assert "编程规范约束（必须遵守仓库 code_rule.txt）" in query
    assert "G.ERR.07 避免抑制或忽略异常" in query
    assert "G.EDV.04 禁止使用subprocess模块中的shell=True选项" in query
    assert "测试代码不得直接访问以单下划线开头的受保护或私有成员" in query
    assert "必须通过公开 API、用户可观察行为或模块级公共函数间接验证" in query


def test_task_progress_extracts_pr_and_failure_code(tmp_path: Path):
    logs = [
        {
            "event_type": "harness.message",
            "pipeline": "meta_evolve_pipeline",
            "stages": [{"slot": "implement"}, {"slot": "verify"}, {"slot": "publish"}],
        },
        {"event_type": "harness.stage_result", "stage": "implement", "status": "success"},
        {
            "event_type": "harness.stage_result",
            "stage": "publish",
            "status": "failed",
            "error": "GitCode PR creation failed: HTTP 400 Bad Request",
            "messages": [
                "PR 发布诊断: http_status=400",
                "PR 已创建: https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/2379",
            ],
        },
    ]

    store = TaskStore(tmp_path)
    store.register_run_log_status_extension(
        skipped_stage_inferer=infer_issue_fix_skipped_stages,
        progress_enricher=enrich_issue_fix_progress,
    )
    progress = store.summarize_progress_from_logs(logs)

    assert progress["failed_stage"] == "publish"
    assert progress["failure_code"] == "pr_api_failed"
    assert progress["pr_url"].endswith("/merge_requests/2379")


def test_issue_fix_task_factory_preserves_issue_ref():
    task = build_issue_fix_task(1272, "请自动修复 GitCode Issue #1272。\n标题: demo")

    assert task.topic == "fix-issue-1272"
    assert task.issue_ref == "#1272"


def test_pipeline_status_accepts_structured_skipped_stages(tmp_path: Path):
    log_path = tmp_path / "log.json"
    log_path.write_text(
        "\n".join([
            '{"event_type":"harness.message","pipeline":"meta_evolve_pipeline",'
            '"stages":[{"slot":"assess"},{"slot":"plan"},{"slot":"implement"}]}',
            '{"event_type":"harness.stage_result","stage":"assess","status":"skipped"}',
            '{"event_type":"harness.stage_result","stage":"plan","status":"skipped"}',
            '{"event_type":"harness.stage_result","stage":"implement","status":"success"}',
        ]),
        encoding="utf-8",
    )

    assert determine_pipeline_status_from_log(
        log_path,
        skipped_stage_inferers=[infer_issue_fix_skipped_stages],
    ) == {"failed": False, "error": ""}


def test_pipeline_status_accepts_legacy_issue_fix_skip_message(tmp_path: Path):
    log_path = tmp_path / "legacy-log.json"
    log_path.write_text(
        "\n".join([
            '{"event_type":"harness.message","pipeline":"meta_evolve_pipeline",'
            '"stages":[{"slot":"assess"},{"slot":"plan"},{"slot":"implement"}]}',
            '{"event_type":"harness.message","content":"检测到显式 GitCode issue 修复任务，'
            '跳过 assess/plan，直接进入实现/验证/提交/发布流程。"}',
            '{"event_type":"harness.stage_result","stage":"implement","status":"success"}',
        ]),
        encoding="utf-8",
    )

    assert determine_pipeline_status_from_log(
        log_path,
        skipped_stage_inferers=[infer_issue_fix_skipped_stages],
    ) == {"failed": False, "error": ""}
