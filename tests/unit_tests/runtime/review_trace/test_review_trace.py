from __future__ import annotations

import json
from pathlib import Path

from jiuwenavatar.server.runtime.review_trace.integration import (
    committer_review_trace_base_dir,
    should_collect_committer_review_trace,
)
from jiuwenavatar.server.runtime.review_trace.redaction import redact_sensitive_values
from jiuwenavatar.server.runtime.review_trace.store import CommitterReviewTraceStore


def _executed_review() -> dict:
    return {
        "execution_id": "review-1",
        "messages": ["https://gitcode.com/openJiuwen/agent-core/pull/1910"],
        "steps": [{
            "kind": "tool",
            "detail": {
                "tool_name": "bash",
                "call_args": {"command": "python scripts/code_review_runner.py collect --pr https://gitcode.com/openJiuwen/agent-core/pull/1910"},
                "call_result": {"success": True},
            },
        }],
    }


def test_collection_scope_requires_committer_and_enabled(monkeypatch) -> None:
    monkeypatch.setenv("COMMITTER_REVIEW_TRACE_ENABLED", "true")
    assert should_collect_committer_review_trace("committer") is True
    assert should_collect_committer_review_trace("developer") is False
    assert should_collect_committer_review_trace("") is False
    monkeypatch.setenv("COMMITTER_REVIEW_TRACE_ENABLED", "false")
    assert should_collect_committer_review_trace("committer") is False


def test_store_writes_only_review_trace_by_default(tmp_path: Path) -> None:
    store = CommitterReviewTraceStore(
        tmp_path,
        review_traces_dir=tmp_path / "default" / "review_traces",
        write_review_trace=True,
        save_raw=False,
        require_pr_review=True,
    )
    store.save(_executed_review())
    traces = list((tmp_path / "default" / "review_traces").glob("*.json"))
    assert len(traces) == 1
    assert not (tmp_path / "default" / "raw").exists()
    assert json.loads(traces[0].read_text(encoding="utf-8"))["skill"] == "dev-reviewer"


def test_store_ignores_pr_mentions_without_review_execution(tmp_path: Path) -> None:
    store = CommitterReviewTraceStore(
        tmp_path,
        review_traces_dir=tmp_path / "default" / "review_traces",
        write_review_trace=True,
        save_raw=False,
        require_pr_review=True,
    )
    store.save({"execution_id": "mention", "messages": ["https://gitcode.com/a/b/pull/1"]})
    assert list((tmp_path / "default" / "review_traces").glob("*.json")) == []


def test_collect_merge_request_wins_over_stale_pull_url(tmp_path: Path) -> None:
    trajectory = {
        "execution_id": "review-1325",
        "messages": [
            "old context https://gitcode.com/openJiuwen/agent-core/pull/1910",
            "please review https://gitcode.com/openJiuwen/agent-core/issues/1325",
        ],
        "steps": [{
            "kind": "tool",
            "detail": {
                "tool_name": "bash",
                "call_args": {
                    "command": (
                        "python scripts/code_review_runner.py collect --pr "
                        "https://gitcode.com/openJiuwen/agent-core/merge_requests/1325"
                    )
                },
                "call_result": {"success": True},
            },
        }],
    }
    store = CommitterReviewTraceStore(
        tmp_path,
        review_traces_dir=tmp_path / "default" / "review_traces",
        write_review_trace=True,
        save_raw=False,
        require_pr_review=True,
    )
    store.save(trajectory)
    traces = list((tmp_path / "default" / "review_traces").glob("*.json"))
    assert len(traces) == 1
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["case_id"] == "real_pr_openJiuwen_agent-core_1325"
    assert trace["task"]["pr_url"] == (
        "https://gitcode.com/openJiuwen/agent-core/merge_requests/1325"
    )
    assert "1910" not in traces[0].name


def test_stale_history_is_not_used_when_current_request_has_no_pr(tmp_path: Path) -> None:
    trajectory = {
        "execution_id": "stale-only",
        "steps": [{
            "kind": "llm",
            "detail": {"messages": [
                {"role": "user", "content": "review https://gitcode.com/a/b/pull/1910"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "now inspect https://gitcode.com/a/b/issues/1325"},
            ]},
        }],
    }
    store = CommitterReviewTraceStore(
        tmp_path,
        review_traces_dir=tmp_path / "default" / "review_traces",
        write_review_trace=True,
        save_raw=False,
        require_pr_review=True,
    )
    store.save(trajectory)
    assert list((tmp_path / "default" / "review_traces").glob("*.json")) == []


def test_reading_previous_review_is_not_a_new_review(tmp_path: Path) -> None:
    trajectory = {
        "execution_id": "read-old-review",
        "messages": ["review https://gitcode.com/openJiuwen/agent-core/merge_requests/1325"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "skill_tool",
                    "call_args": {"skill_name": "dev-reviewer"},
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "read_file",
                    "call_args": {"file_path": "D:/repo/doc/pr1325/review/result.json"},
                    "call_result": {
                        "success": True,
                        "data": {"content": '{"findings":{"must_fix":[{"id":"MF-001","location":"a.py:1"}]}}'},
                    },
                },
            },
        ],
    }
    store = CommitterReviewTraceStore(
        tmp_path,
        review_traces_dir=tmp_path / "default" / "review_traces",
        write_review_trace=True,
        save_raw=False,
        require_pr_review=True,
    )
    store.save(trajectory)
    assert list((tmp_path / "default" / "review_traces").glob("*.json")) == []


def test_location_alone_does_not_prove_resolve_positions() -> None:
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {"command": "python code_review_runner.py collect --pr https://gitcode.com/a/b/pull/1"},
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "write_file",
                    "call_args": {
                        "file_path": "D:/repo/result.json",
                        "content": '{"findings":{"must_fix":[{"id":"MF-001","location":"a.py:1"}]}}',
                    },
                    "call_result": {"success": True},
                },
            },
        ],
    }
    trace = trajectory_to_review_trace(trajectory)
    assert trace["findings"][0]["position"] is None
    assert trace["findings"][0]["position_resolved"] is False
    assert trace["runner_steps"]["resolve_positions"]["status"] == ""


def test_null_position_stays_unresolved_after_resolve_command() -> None:
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    result = {
        "findings": {
            "must_fix": [],
            "should_fix": [
                {"id": "SF-1", "location": "a.py:10", "position": None}
            ],
            "nice_to_have": [],
        }
    }
    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {
                        "command": (
                            "python code_review_runner.py collect --pr "
                            "https://gitcode.com/a/b/pull/1"
                        )
                    },
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "write_file",
                    "call_args": {
                        "file_path": "D:/repo/result.json",
                        "content": json.dumps(result),
                    },
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {
                        "command": "python code_review_runner.py resolve-positions --number 1"
                    },
                    "call_result": {"success": True},
                },
            },
        ],
    }

    trace = trajectory_to_review_trace(trajectory)

    assert trace["findings"][0]["position"] is None
    assert trace["findings"][0]["position_resolved"] is False
    assert trace["runner_steps"]["resolve_positions"]["status"] == ""


def test_runner_post_comments_defaults_to_dry_run() -> None:
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {"command": "python code_review_runner.py collect --pr https://gitcode.com/a/b/pull/1"},
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {"command": "python code_review_runner.py post-comments --number 1"},
                    "call_result": {
                        "success": True,
                        "data": {
                            "content": json.dumps({
                                "ok": True,
                                "dry_run": True,
                                "results": [{"id": "MF-001", "status": "dry_run"}],
                            })
                        },
                    },
                },
            },
        ],
    }
    trace = trajectory_to_review_trace(trajectory)
    assert trace["runner_steps"]["post_comments"]["status"] == "dry_run_success"
    assert trace["gitcode_api"]["mode"] == "dry_run"
    assert trace["gitcode_api"]["execute_used"] is False


def test_runner_post_comments_skipped_duplicate_counts_as_posted() -> None:
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    result = {
        "findings": {
            "must_fix": [
                {"id": "MF-001", "location": "a.py:1", "position": 1},
                {"id": "MF-002", "location": "a.py:2", "position": 2},
            ],
            "should_fix": [],
            "nice_to_have": [],
        }
    }
    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {"command": "python code_review_runner.py collect --pr https://gitcode.com/a/b/pull/1"},
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "write_file",
                    "call_args": {"file_path": "D:/result.json", "content": json.dumps(result)},
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {"command": "python code_review_runner.py post-comments --number 1 --execute"},
                    "call_result": {
                        "success": True,
                        "data": {
                            "content": json.dumps(
                                {
                                    "ok": True,
                                    "dry_run": False,
                                    "results": [
                                        {"id": "MF-001", "status": "posted"},
                                        {"id": "MF-002", "status": "skipped_duplicate"},
                                    ],
                                }
                            )
                        },
                    },
                },
            },
        ],
    }
    trace = trajectory_to_review_trace(trajectory)
    assert trace["runner_steps"]["post_comments"]["status"] == "execute_success"
    # A fully deduplicated run still counts as commented evidence.
    assert trace["findings"][0]["comment_posted"] is True
    assert trace["findings"][1]["comment_posted"] is True


def test_single_digit_finding_id_is_linked_to_direct_comment() -> None:
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    result = {"findings": {"must_fix": [], "should_fix": [
        {"id": "SF-1", "location": "a.py:1", "position": 1}
    ], "nice_to_have": []}}
    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {"kind": "tool", "detail": {
                "tool_name": "bash",
                "call_args": {"command": "python code_review_runner.py collect --pr https://gitcode.com/a/b/pull/1"},
                "call_result": {"success": True},
            }},
            {"kind": "tool", "detail": {
                "tool_name": "write_file",
                "call_args": {"file_path": "D:/result.json", "content": json.dumps(result)},
                "call_result": {"success": True},
            }},
            {"kind": "tool", "detail": {
                "tool_name": "bash",
                "call_args": {"command": "python pr_commenter.py --number 1 --comment-file D:/SF-1.md --path a.py --position 1"},
                "call_result": {"success": True, "data": {"content": '{"success":true,"comment_id":"abc"}'}},
            }},
        ],
    }
    trace = trajectory_to_review_trace(trajectory)
    assert trace["findings"][0]["comment_posted"] is True
    assert trace["gitcode_api"]["mode"] == "execute"


def test_default_runtime_location_is_user_data_not_repository(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert committer_review_trace_base_dir(avatar_id="avatar/1") == (
        tmp_path / ".jiuwenavatar" / "review_traces" / "avatar_1"
    )


def test_redaction_masks_credentials_and_home() -> None:
    value = {"token": "secret-value", "path": str(Path.home() / "private"), "text": "Bearer abc.def"}
    redacted = redact_sensitive_values(value)
    assert redacted["token"] == "******"
    assert "<USER_HOME>" in redacted["path"]
    assert redacted["text"] == "Bearer ******"


def test_text_findings_use_local_bucket_context() -> None:
    """Text-scan findings must classify bucket from local context, not the whole text.

    When the review text contains both a "Must Fix" and a "Should Fix" section,
    a finding scanned from each section must land in its own bucket. The prior
    global check flipped every text-scan finding to one bucket based on a
    single "Must Fix" anywhere in the text.
    """
    from jiuwenavatar.server.runtime.review_trace.adapter import trajectory_to_review_trace

    # No result.json write -> the text-scan fallback path runs.
    # Two findings in two sections, each near its own severity header, far
    # enough apart that the 200-char local window keeps them isolated.
    trajectory = {
        "messages": ["https://gitcode.com/a/b/pull/1"],
        "steps": [
            {
                "kind": "tool",
                "detail": {
                    "tool_name": "bash",
                    "call_args": {
                        "command": (
                            "python code_review_runner.py collect --pr "
                            "https://gitcode.com/a/b/pull/1"
                        )
                    },
                    "call_result": {"success": True},
                },
            },
            {
                "kind": "llm",
                "detail": {
                    "messages": [
                        {"role": "assistant", "content": (
                            "Must Fix\n"
                            "auth.py:12 leaks the token. "
                            + ("padding " * 60)
                            + "end of critical section."
                        )},
                        {"role": "assistant", "content": (
                            "Should Fix\n"
                            "style.py:5 has a long line. "
                            + ("padding " * 60)
                            + "end of minor section."
                        )},
                    ]
                },
            },
        ],
    }

    trace = trajectory_to_review_trace(trajectory)
    by_location = {f["location"]: f["bucket"] for f in trace["findings"]}
    assert by_location.get("auth.py:12") == "must_fix"
    assert by_location.get("style.py:5") == "should_fix"


def test_kindless_tool_step_with_tool_name_is_collected() -> None:
    """A step without a ``kind`` field but carrying ``detail.tool_name`` is a
    real tool call in older/edge-case trajectories and must still be collected.
    Gating only on ``kind == "tool"`` would drop it. A kind-less step with no
    tool_name must NOT be collected (it is not a tool call).
    """
    from jiuwenavatar.server.runtime.review_trace.adapter import _iter_tool_calls

    data = {
        "steps": [
            # Kind-less, no tool_name: not a tool call, must be skipped.
            {"detail": {"marker": "synthetic"}},
            # Kind-less but carries a tool_name: real call, must be collected.
            {"detail": {"tool_name": "bash", "call_args": {"command": "ls"}, "call_result": {"success": True}}},
            # Normal kind=tool step: collected.
            {"kind": "tool", "detail": {"tool_name": "powershell"}},
        ]
    }
    calls = _iter_tool_calls(data)
    tool_names = [c["tool_name"] for c in calls]
    assert tool_names == ["bash", "powershell"]
    assert all(c.get("_source") == "top_level_step" for c in calls)


def test_empty_old_string_does_not_overwrite_existing_content() -> None:
    """An edit_file with old_string="" must not overwrite existing content.

    openjiuwen's edit_file rejects "file exists with empty old_string"; the
    adapter's virtual file state must mirror that: when current content
    already exists, the edit is recorded as a limitation and the current state
    is kept. Previously the code unconditionally set updated = new_string,
    clobbering real content (e.g. an accumulated result.json) and corrupting
    the downstream review_result reload.
    """
    from jiuwenavatar.server.runtime.review_trace.adapter import _apply_file_write_activity

    activity = {"file_states": {"result.json": '{"findings": {"must_fix": [{"id": "MF-1"}]}}'}, "edit_limitations": []}
    _apply_file_write_activity(
        activity,
        {"file_path": "result.json", "old_string": "", "new_string": "clobber"},
    )
    # Current content preserved, not overwritten by "clobber".
    assert activity["file_states"]["result.json"] == '{"findings": {"must_fix": [{"id": "MF-1"}]}}'
    # Limitation recorded.
    assert any(
        lim["reason"] == "empty_old_string_on_nonempty_content" for lim in activity["edit_limitations"]
    )


def test_empty_old_string_creates_content_when_empty() -> None:
    """An edit_file with old_string="" against empty content creates the file.

    This is the valid half of the empty-old_string path (new file creation),
    and must keep working after the non-empty guard is added.
    """
    from jiuwenavatar.server.runtime.review_trace.adapter import _apply_file_write_activity

    activity = {"file_states": {}, "edit_limitations": []}
    _apply_file_write_activity(
        activity,
        {"file_path": "result.json", "old_string": "", "new_string": '{"findings": {}}'},
    )
    assert activity["file_states"]["result.json"] == '{"findings": {}}'
    assert activity["edit_limitations"] == []


def test_pr_commenter_successful_call_count_dry_run_and_execute() -> None:
    """successful_comment_calls increments once per successful pr_commenter call.

    A successful dry run (success + --dry-run + [DRY RUN]) and a successful
    execute (success + no --dry-run) each count one successful call. A failed
    call (not success) counts none. This locks the shared dry_run_ok/execute_ok
    condition used by dry_run_success, execute_success and successful_comment_calls.
    """
    from jiuwenavatar.server.runtime.review_trace.adapter import _apply_command_activity

    def run(command: str, result_text: str, success: bool) -> dict:
        activity = {
            "dev_reviewer_loaded": False, "dev_reviewer_runner_used": False,
            "collect_done": False, "init_review_done": False, "report_done": False,
            "resolve_positions_done": False, "validate_comments_done": False,
            "render_comments_done": False, "dry_run_used": False, "dry_run_success": False,
            "dry_run_comment_ids_null": False, "execute_used": False, "execute_success": False,
            "gitcode_comment_called": False, "reported_api_result": False,
            "commented_finding_ids": set(), "successful_comment_calls": 0,
            "collect_commands": [], "artifacts": [], "changed_files": [],
            "review_texts": [], "pr_metadata": {}, "review_result": {},
            "file_states": {}, "edit_limitations": [], "context_artifact": None, "diff_artifact": None,
        }
        _apply_command_activity(activity, command, {"success": success, "data": {"content": result_text}}, result_text)
        return activity

    dry_ok = run("pr_commenter.py --number 1 --dry-run", "[DRY RUN] posted", True)
    assert dry_ok["dry_run_success"] is True
    assert dry_ok["execute_success"] is False
    assert dry_ok["successful_comment_calls"] == 1

    execute_ok = run("pr_commenter.py --number 1", "comment_id: abc", True)
    assert execute_ok["dry_run_success"] is False
    assert execute_ok["execute_success"] is True
    assert execute_ok["successful_comment_calls"] == 1

    failed = run("pr_commenter.py --number 1 --dry-run", "error", False)
    assert failed["dry_run_success"] is False
    assert failed["execute_success"] is False
    assert failed["successful_comment_calls"] == 0
