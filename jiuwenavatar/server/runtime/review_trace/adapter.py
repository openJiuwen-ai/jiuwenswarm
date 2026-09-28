"""Convert generic openjiuwen trajectories into Committer review traces."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .json_io import sha256_json

PR_URL_RE = re.compile(
    r"https://gitcode\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/"
    r"(?P<kind>pull|merge_requests)/(?P<number>\d+)"
)
LOCATION_RE = re.compile(r"(?P<path>[\w./-]+\.[A-Za-z0-9]+):(?P<line>\d+(?:-\d+)?)")
COMMENT_ID_RE = re.compile(r"\b((?:CR|MF|SF|NH)-\d{1,3})\b")
RESULT_JSON_PATH_RE = re.compile(r"(?P<path>[A-Za-z]:\\[^\r\n\"']*?result\.json|/[^ \r\n\"']*?result\.json)")


def trajectory_to_review_trace(
    trajectory: dict[str, Any],
    *,
    case_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build a first-pass Committer review_trace from a generic trajectory.

    The adapter is intentionally conservative: unknown fields are left empty
    instead of guessed. Later Rail work can enrich the same trace shape.
    """

    text = _stringify(trajectory)
    tool_calls = list(_iter_tool_calls(trajectory))
    activity = _extract_activity(tool_calls)
    message_texts = _extract_message_texts(trajectory)
    activity["review_texts"].extend(message_texts)
    if _contains_any("\n".join(message_texts), ["dev-reviewer"]):
        activity["dev_reviewer_loaded"] = True
    # Do not infer API activity from arbitrary conversation text. Loaded Skill
    # and reference files contain literal examples such as ``--dry-run`` and
    # ``post-comments``; treating those strings as execution evidence can turn
    # an interrupted review into a false successful run. Only parsed tool calls
    # in ``_apply_command_activity`` may establish comment/dry-run success.
    pr_url = _resolve_pr_url(trajectory, activity)
    resolved_case_id = case_id or _case_id_from_pr_url(pr_url)
    findings = _extract_findings(trajectory, text, activity)
    inline_findings = [
        item for item in findings if str(item.get("bucket") or "").lower() in {"must_fix", "should_fix"}
    ]
    used_dev_reviewer = activity["dev_reviewer_loaded"] or activity["dev_reviewer_runner_used"]
    coding_task_called = any(call["tool_name"] == "coding_task" for call in tool_calls)
    post_status = (
        "execute_success"
        if activity["execute_success"]
        else ("dry_run_success" if activity["dry_run_success"] else "")
    )
    api_success = activity["dry_run_success"] or activity["execute_success"]
    api_called = activity["gitcode_comment_called"]

    trace = {
        "trace_id": trace_id or f"{resolved_case_id or 'review'}_trace",
        "case_id": resolved_case_id,
        "skill": "dev-reviewer" if used_dev_reviewer else "",
        "avatar": "Committer",
        "task": {
            "scope": "open_pr_line_review",
            "pr_url": pr_url,
            "coding_task_called": coding_task_called,
        },
        "pr_metadata": _extract_pr_metadata(trajectory, pr_url, activity),
        "evidence_alignment": _extract_evidence_alignment(trajectory, pr_url, activity),
        "runner_steps": {
            "collect": {"status": "done" if activity["collect_done"] else ""},
            "init_review": {"status": "done" if activity["init_review_done"] else ""},
            "resolve_positions": {
                "status": "done"
                if activity["resolve_positions_done"]
                and (not inline_findings or all(f.get("position_resolved") for f in inline_findings))
                else ""
            },
            "report": {"status": "done" if activity["report_done"] else ""},
            "post_comments": {"status": post_status},
        },
        "findings": findings,
        "gitcode_api": {
            "called": api_called,
            "success": api_success,
            "mode": "execute" if activity["execute_used"] else ("dry_run" if activity["dry_run_used"] else ""),
            "dry_run": activity["dry_run_used"],
            "execute_used": activity["execute_used"],
            "dry_run_comment_ids_null": activity["dry_run_comment_ids_null"],
        },
        "final_response": {
            "reported_api_result": activity["reported_api_result"],
            "discussion_summary_only": False,
            "claimed_success": api_success,
        },
        "redaction": {"secrets_removed": True, "private_paths_removed": True},
    }
    return trace


def _extract_pr_metadata(data: dict[str, Any], pr_url: str, activity: dict[str, Any]) -> dict[str, Any]:
    metadata = activity.get("pr_metadata") or _find_mapping_with_keys(data, {"state", "head", "base"}) or {}
    return {
        "state": metadata.get("state") or _find_value(data, "state") or "",
        "repo": _repo_from_url(pr_url),
        "number": _pr_number_from_url(pr_url),
        "base_sha": metadata.get("base_sha") or _find_value(data, "base_sha") or metadata.get("base") or "",
        "head_sha": metadata.get("head_sha") or _find_value(data, "head_sha") or metadata.get("head") or "",
        "base": metadata.get("base") or "",
        "head": metadata.get("head") or "",
    }


def _extract_evidence_alignment(data: dict[str, Any], pr_url: str, activity: dict[str, Any]) -> dict[str, Any]:
    metadata = activity.get("pr_metadata") or {}
    diff_hash = _find_value(data, "diff_hash") or ""
    files_hash = _find_value(data, "files_hash") or ""
    base_sha = _find_value(data, "base_sha") or metadata.get("base_sha") or metadata.get("base") or ""
    head_sha = _find_value(data, "head_sha") or metadata.get("head_sha") or metadata.get("head") or ""
    evidence_source = {
        "pr_url": pr_url,
        "collect": activity.get("collect_commands"),
        "artifacts": activity.get("artifacts"),
        "changed_files": activity.get("changed_files"),
    }
    aligned = bool(diff_hash and files_hash and base_sha and head_sha) or bool(
        pr_url and activity.get("collect_done") and activity.get("pr_metadata")
    )
    return {
        "source": "committer_session",
        "aligned": aligned,
        "pr_url": pr_url,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_hash": diff_hash or sha256_json({"trajectory_diff_source": evidence_source}),
        "files_hash": files_hash or sha256_json({"trajectory_files_source": evidence_source}),
        # collected_at: prefer a value present in the trajectory. If none, use a
        # deterministic hash of the evidence source instead of the wall clock, so
        # re-importing the same trajectory yields a stable trace_hash (the prior
        # utc_now_iso() fallback made every re-import produce a different hash).
        "collected_at": _find_value(data, "collected_at")
        or sha256_json({"collected_at_source": evidence_source}),
    }


def _extract_findings(data: Any, text: str, activity: dict[str, Any]) -> list[dict[str, Any]]:
    result_json = activity.get("review_result")
    if isinstance(result_json, dict) and isinstance(result_json.get("findings"), dict):
        # A parsed review result.json is authoritative: its finding buckets ARE
        # the review's findings, even when empty (a PR may legitimately have no
        # inline findings). We must NOT fall through to text-scanning, because the
        # conversation history carries loaded SKILL.md / reference-doc examples
        # whose paths are not real findings.
        return _findings_from_review_result(result_json, activity)

    explicit = _find_value(data, "findings")
    if isinstance(explicit, list):
        return [item for item in explicit if isinstance(item, dict)]
    if isinstance(explicit, dict):
        findings = _findings_from_review_result({"findings": explicit}, activity)
        if findings:
            return findings

    findings = _findings_from_review_texts(activity.get("review_texts") or [], activity)
    if findings:
        return findings

    # Never scan the full trajectory: loaded SKILL.md files and reference docs
    # contain example paths that are not review findings.
    return []


def _findings_from_review_texts(texts: list[str], activity: dict[str, Any]) -> list[dict[str, Any]]:
    scoped_text = "\n".join(text for text in texts if text)
    if not scoped_text:
        return []
    findings = []
    for index, match in enumerate(LOCATION_RE.finditer(scoped_text), start=1):
        # Judge bucket and comment status from the text *around* this finding,
        # not the whole scoped_text. A single "Must Fix" anywhere used to flip
        # every text-scanned finding to must_fix; the local window keeps each
        # finding's classification independent.
        start = max(0, match.start() - 200)
        end = min(len(scoped_text), match.end() + 200)
        local_text = scoped_text[start:end]
        findings.append(
            {
                "id": f"CR-{index:03d}",
                "bucket": "must_fix" if _contains_any(local_text, ["Must Fix", "必须修复"]) else "should_fix",
                "location": match.group(0),
                # A source line in prose is not proof that GitCode resolved an
                # inline-comment position. Keep this conservative unless
                # structured review output provides a concrete position.
                "position_resolved": False,
                "comment_posted": _contains_any(local_text, ["posted", "已提交", "dry_run_success"]),
            }
        )
    return findings[:20]


def _findings_from_review_result(result: dict[str, Any], activity: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("findings")
    if not isinstance(raw, dict):
        return []
    findings: list[dict[str, Any]] = []
    commented_ids = set(activity.get("commented_finding_ids") or [])
    successful_comment_calls = int(activity.get("successful_comment_calls") or 0)
    result_finding_count = sum(
        len(items)
        for items in (raw.get("must_fix"), raw.get("should_fix"), raw.get("nice_to_have"))
        if isinstance(items, list)
    )
    # Some real runs do not include CR-* ids in the submitted comment body.
    # When every finding has a corresponding successful comment call, the call
    # count is stronger evidence than a partial id set (which otherwise marks
    # only the one comment whose free text happened to contain its id).
    all_findings_commented = bool(
        result_finding_count and successful_comment_calls >= result_finding_count
    )
    post_done = bool(activity.get("dry_run_success") or activity.get("execute_success"))
    for bucket, items in (
        ("must_fix", raw.get("must_fix")),
        ("should_fix", raw.get("should_fix")),
        ("nice_to_have", raw.get("nice_to_have")),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            finding_id = str(item.get("id") or f"CR-{len(findings) + 1:03d}")
            location = _normalize_location(item.get("location"))
            position = item.get("position")
            resolved_position = position
            if not location:
                # Real result.json files use both schemas:
                #   location / path+position / file+line.
                # Treat file+line as a concrete source location instead of
                # silently discarding it and creating a false hard-gate failure.
                file_path = item.get("path") or item.get("file")
                line = position if position not in (None, "") else item.get("line")
                if file_path and line not in (None, ""):
                    location = f"{file_path}:{line}"
                    resolved_position = line
            finding = dict(item)
            finding["id"] = finding_id
            finding["bucket"] = bucket
            finding["location"] = location
            finding["position"] = resolved_position
            # A source ``location`` is not sufficient evidence for GitCode's
            # separately resolved inline-comment ``position``.
            finding["position_resolved"] = _valid_position(resolved_position)
            finding["comment_posted"] = bool(
                post_done
                and (all_findings_commented or not commented_ids or finding_id in commented_ids)
            )
            findings.append(finding)
    return findings


def _normalize_location(value: Any) -> str:
    if isinstance(value, dict):
        file_path = str(value.get("file") or value.get("path") or "").strip()
        line = value.get("line") or value.get("position")
        if file_path and line not in (None, ""):
            return f"{file_path}:{line}"
        return file_path
    return str(value or "")


def _extract_activity(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "dev_reviewer_loaded": False,
        "dev_reviewer_runner_used": False,
        "collect_done": False,
        "init_review_done": False,
        "report_done": False,
        "resolve_positions_done": False,
        "validate_comments_done": False,
        "render_comments_done": False,
        "dry_run_used": False,
        "dry_run_success": False,
        "dry_run_comment_ids_null": False,
        "execute_used": False,
        "execute_success": False,
        "gitcode_comment_called": False,
        "reported_api_result": False,
        "commented_finding_ids": set(),
        "successful_comment_calls": 0,
        "collect_commands": [],
        "artifacts": [],
        "changed_files": [],
        "review_texts": [],
        "pr_metadata": {},
        "review_result": {},
        # A2: virtual file-state recovery for write_file → edit_file sequences.
        "file_states": {},
        "edit_limitations": [],
        # A1: artifact-evidence fallback for collect/diff when the runner
        # command is unreachable (only appears in conversation history).
        "context_artifact": None,
        "diff_artifact": None,
    }

    for call in tool_calls:
        tool_name = call["tool_name"]
        args = call["call_args"]
        result = call["call_result"]
        args_text = _stringify(args)
        result_text = _stringify(result)
        command = str(args.get("command") or "") if isinstance(args, dict) else args_text

        if tool_name == "skill_tool" and "dev-reviewer" in args_text:
            activity["dev_reviewer_loaded"] = True

        if tool_name == "coding_task":
            activity["review_texts"].append(result_text)
            # A coding_task invocation is execution evidence, not merely chat
            # text.  Mark dev-reviewer as used only when the executed task card
            # explicitly asks for that skill.
            if "dev-reviewer" in args_text:
                activity["dev_reviewer_loaded"] = True
                activity["dev_reviewer_runner_used"] = True

        if tool_name in {"bash", "powershell"}:
            _apply_command_activity(activity, command, result, result_text)

        if tool_name in {"write_file", "edit_file"}:
            _apply_file_write_activity(activity, args)

        # Track read_file of context.json / pr.diff as collect/diff evidence.
        if tool_name == "read_file":
            _record_read_file_evidence(activity, args, result)

    # A1: strong-evidence fallback. If collect was not recognized from a runner
    # command (the call only lives in conversation history and has no top-level
    # step), recover collect_done from a context.json read whose PR matches this
    # trajectory and a non-empty pr.diff read. PR metadata (base/head/changed_files)
    # is recovered from the same context evidence regardless of whether collect was
    # already recognized, so evidence alignment does not depend on base/head being
    # present in the raw trajectory.
    if not activity["collect_done"]:
        _apply_artifact_evidence_fallback(activity)
    _merge_context_pr_metadata(activity)

    # A2: re-extract review_result from the *final* result.json state, so later
    # edit_file corrections (e.g. CR-004 location → stdio_client.py:94) are used.
    _reload_review_result_from_final_state(activity)

    return activity


def _record_read_file_evidence(activity: dict[str, Any], args: Any, result: Any) -> None:
    """Remember read_file calls that produce collect/diff evidence.

    The fallback keys on actual file reads (not on a final answer claiming
    "collected"): a context.json whose bound PR matches the trajectory, and a
    non-empty pr.diff. This is command-agnostic and survives a collect call
    that lives only in conversation history.
    """

    if not isinstance(args, dict):
        return
    file_path = str(args.get("file_path") or "").replace("\\", "/").lower()
    content = _content_from_result(result)
    if "context.json" in file_path and content:
        context = _parse_numbered_json(content)
        if isinstance(context, dict):
            activity["context_artifact"] = {
                "path": str(args.get("file_path") or ""),
                "context": context,
                "content": content,
            }
    if "pr.diff" in file_path and content and content.strip():
        activity["diff_artifact"] = {
            "path": str(args.get("file_path") or ""),
            "content": content,
            "nonempty": True,
        }


def _apply_artifact_evidence_fallback(activity: dict[str, Any]) -> None:
    """Recover collect_done from context.json + pr.diff reads.

    Triggered only when no runner ``collect`` command was recognized. The bar is
    deliberately high (both a context.json bound to the same PR *and* a non-empty
    pr.diff), so a model merely *claiming* it collected is not enough. PR metadata
    is merged separately by :func:`_merge_context_pr_metadata`.
    """

    ctx_art = activity.get("context_artifact")
    diff_art = activity.get("diff_artifact")
    if not isinstance(ctx_art, dict) or not isinstance(diff_art, dict):
        return
    if not diff_art.get("nonempty"):
        return
    context = ctx_art.get("context") or {}
    pr_url = str(context.get("pr") or "")
    # The context must bind to a real GitCode PR to count as collect evidence.
    if not pr_url or "gitcode.com" not in pr_url:
        return
    activity["collect_done"] = True
    activity["collect_commands"].append(f"fallback:context.json+pr.diff ({pr_url})")
    activity["artifacts"].append(ctx_art.get("path") or "context.json")
    activity["artifacts"].append(diff_art.get("path") or "pr.diff")
    activity["review_texts"].append(ctx_art.get("content") or "")


def _merge_context_pr_metadata(activity: dict[str, Any]) -> None:
    """Recover base/head/changed_files from a context.json read, if present.

    Runs whether or not collect was recognized by a command: the same worktree
    context.json carries the PR URL, base/head and changed_files. Without this,
    evidence alignment would fail whenever the raw trajectory has no top-level
    base_sha/head_sha/diff_hash fields (the common case for real trajectories).
    """

    ctx_art = activity.get("context_artifact")
    if not isinstance(ctx_art, dict):
        return
    context = ctx_art.get("context") or {}
    pr_url = str(context.get("pr") or "")
    if pr_url and "gitcode.com" not in pr_url:
        return
    changed = context.get("changed_files")
    if isinstance(changed, list) and changed and not activity.get("changed_files"):
        activity["changed_files"] = [str(f) for f in changed]
    meta = activity.get("pr_metadata") if isinstance(activity.get("pr_metadata"), dict) else {}
    meta = dict(meta)
    if context.get("base") and not meta.get("base"):
        meta["base"] = context.get("base")
    if context.get("head") and not meta.get("head"):
        meta["head"] = context.get("head")
    if pr_url and not meta.get("pr_url"):
        meta["pr_url"] = pr_url
    if context.get("state") and not meta.get("state"):
        meta["state"] = context.get("state")
    if meta:
        activity["pr_metadata"] = meta


def _reload_review_result_from_final_state(activity: dict[str, Any]) -> None:
    """Re-derive review_result from the final result.json file state.

    write_file establishes result.json; later edit_file calls correct finding
    locations/positions. The initial write may carry a stale location (e.g.
    ``(architecture)``) that a later edit changes to a concrete path:line. We
    parse the *final* accumulated state so findings reflect the last edit.
    """

    final_state = activity.get("file_states", {}).get("result.json")
    if not isinstance(final_state, str) or not final_state.strip():
        return
    parsed = _json_loads(final_state)
    if isinstance(parsed, dict) and "findings" in parsed:
        activity["review_result"] = parsed
        summary = parsed.get("summary") or {}
        if isinstance(summary, dict) and isinstance(summary.get("affected_files"), list):
            activity["changed_files"] = summary["affected_files"]


_RUNNER_SUBCOMMANDS = (
    "collect",
    "init-review",
    "resolve-positions",
    "validate-comments",
    "render-comments",
    "post-comments",
    "report",
)
# code_review_runner.py may be invoked via an absolute path wrapped in quotes
# (e.g. `python "C:\path\code_review_runner.py" collect ...`); each pattern
# tolerates an optional closing quote and whitespace between the module and
# the subcommand. Built with an explicit loop so the literal tuple of
# subcommands does not trip the multi-clause comprehension check.
_RUNNER_SUBCOMMAND_RE: dict[str, re.Pattern[str]] = {}
for _sub in _RUNNER_SUBCOMMANDS:
    _RUNNER_SUBCOMMAND_RE[_sub] = re.compile(
        rf"code_review_runner\.py[\"']*\s+{re.escape(_sub)}\b"
    )
_RUNNER_HELP_RE = re.compile(r"(--help\b|-h\b|\bhelp\b)", re.IGNORECASE)


def _matches_runner_subcommand(command: str, sub: str) -> bool:
    """Match a code_review_runner.py subcommand, tolerating quoted absolute paths.

    Excludes --help / -h invocations so that a help call (which still contains
    the subcommand name and exits successfully) is not mistaken for the real step.
    """

    pattern = _RUNNER_SUBCOMMAND_RE.get(sub)
    if not pattern or not pattern.search(command):
        return False
    return not _RUNNER_HELP_RE.search(command)


def _apply_command_activity(activity: dict[str, Any], command: str, result: Any, result_text: str) -> None:
    success = _call_succeeded(result)
    if "code_review_runner.py" in command:
        activity["dev_reviewer_runner_used"] = True
    if _matches_runner_subcommand(command, "collect"):
        activity["collect_commands"].append(command)
        activity["collect_done"] = activity["collect_done"] or success
        artifact = _content_from_result(result)
        if artifact:
            activity["artifacts"].append(artifact)
            activity["review_texts"].append(artifact)
            _maybe_load_review_result_from_text(activity, artifact)
    if _matches_runner_subcommand(command, "init-review"):
        activity["init_review_done"] = activity["init_review_done"] or success
        artifact = _content_from_result(result)
        if artifact:
            activity["artifacts"].append(artifact)
            activity["review_texts"].append(artifact)
            _maybe_load_review_result_from_text(activity, artifact)
    if _matches_runner_subcommand(command, "resolve-positions"):
        activity["resolve_positions_done"] = activity["resolve_positions_done"] or success
    if _matches_runner_subcommand(command, "validate-comments"):
        activity["validate_comments_done"] = activity["validate_comments_done"] or success
    if _matches_runner_subcommand(command, "render-comments"):
        activity["render_comments_done"] = activity["render_comments_done"] or success
    if _matches_runner_subcommand(command, "post-comments"):
        activity["gitcode_comment_called"] = True
        execute = "--execute" in command
        activity["execute_used"] = activity["execute_used"] or execute
        activity["dry_run_used"] = activity["dry_run_used"] or not execute
        payload = _json_from_result_content(result)
        payload_ok = not isinstance(payload, dict) or payload.get("ok") is not False
        post_succeeded = success and payload_ok
        if execute:
            activity["execute_success"] = activity["execute_success"] or post_succeeded
        else:
            activity["dry_run_success"] = activity["dry_run_success"] or post_succeeded
        if isinstance(payload, dict):
            activity["reported_api_result"] = True
            results = payload.get("results") if isinstance(payload.get("results"), list) else []
            successful = [
                item for item in results
                # skipped_existing / skipped_duplicate mean the comment is
                # already on the PR (id or content dedup), so they count as
                # commented evidence for the all_findings_commented gate.
                if isinstance(item, dict) and item.get("status") in {
                    "dry_run", "posted", "skipped_existing", "skipped_duplicate",
                }
            ]
            activity["successful_comment_calls"] += len(successful)
            for item in successful:
                finding_id = str(item.get("id") or "")
                if COMMENT_ID_RE.fullmatch(finding_id):
                    activity["commented_finding_ids"].add(finding_id)
            if not execute and payload.get("dry_run") is True:
                activity["dry_run_comment_ids_null"] = True
    if _matches_runner_subcommand(command, "report"):
        # The runner exits non-zero when review gate is not PASS, but still writes review.md.
        activity["report_done"] = True
        artifact = _content_from_result(result)
        if artifact:
            activity["artifacts"].append(artifact)
            activity["review_texts"].append(artifact)
            _maybe_load_review_result_from_text(activity, artifact)
    if "pr_creator.py" in command:
        pr_payload = _json_from_result_content(result)
        if isinstance(pr_payload, dict):
            activity["pr_metadata"] = pr_payload
    if "pr_commenter.py" in command and "--number" in command:
        activity["gitcode_comment_called"] = True
        is_dry_run = "--dry-run" in command
        if is_dry_run:
            activity["dry_run_used"] = True
        else:
            # pr_commenter.py executes by default; it has no --execute flag.
            # Omitting --dry-run is positive evidence of a GitCode write.
            activity["execute_used"] = True
        if "comment_id" in result_text or "[DRY RUN]" in result_text:
            activity["reported_api_result"] = True
        # One successful pr_commenter call: either a dry run that printed
        # [DRY RUN], or an execute (no --dry-run). Both must additionally have
        # succeeded. Collapse the repeated condition so each boolean expr stays
        # within the clause limit and the three consumers share one definition.
        dry_run_ok = success and is_dry_run and "[DRY RUN]" in result_text
        execute_ok = success and not is_dry_run
        if dry_run_ok:
            activity["dry_run_success"] = True
        if execute_ok:
            activity["execute_success"] = True
        if dry_run_ok or execute_ok:
            activity["successful_comment_calls"] += 1
        if '"comment_id": null' in result_text or "'comment_id': None" in result_text:
            activity["dry_run_comment_ids_null"] = True
        activity["commented_finding_ids"].update(COMMENT_ID_RE.findall(command))
        activity["commented_finding_ids"].update(COMMENT_ID_RE.findall(result_text))
        activity["review_texts"].append(command)
        activity["review_texts"].append(result_text)
        _maybe_load_review_result_from_text(activity, result_text)


def _apply_file_write_activity(activity: dict[str, Any], args: Any) -> None:
    """Maintain the virtual file state for write_file and edit_file calls.

    write_file establishes a file's content; edit_file mutates it via
    old_string→new_string replacement. We track the *final* accumulated state per
    path so downstream extraction (e.g. result.json findings) reflects the last
    edit rather than the initial write. If an edit cannot be applied (old_string
    not present), we record an explicit adapter limitation instead of silently
    keeping stale content.
    """

    if not isinstance(args, dict):
        return
    file_path = str(args.get("file_path") or "")
    if not file_path:
        return
    key = file_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    states = activity.setdefault("file_states", {})

    if "old_string" in args and "new_string" in args:
        # edit_file (and any edit API exposing old_string/new_string).
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        current = states.get(key, "") or activity.get("_initial_content", {}).get(key, "")
        if not isinstance(current, str) or not isinstance(old_string, str) or not isinstance(new_string, str):
            states[key] = current if isinstance(current, str) else ""
            return
        if old_string == "" and new_string:
            # An empty old_string creates a new file against empty/None content.
            # openjiuwen's edit_file rejects "file exists with empty old_string",
            # so when current content already exists we must NOT overwrite it —
            # record the limitation and keep the current state instead.
            if current:
                activity.setdefault("edit_limitations", []).append(
                    {
                        "file": file_path,
                        "reason": "empty_old_string_on_nonempty_content",
                    }
                )
                return
            updated = new_string
        elif old_string in current:
            updated = current.replace(old_string, new_string, 1)
        else:
            # Edit cannot be applied — record the limitation, keep current state.
            activity.setdefault("edit_limitations", []).append(
                {
                    "file": file_path,
                    "reason": "old_string_not_found",
                    "old_string_preview": old_string[:80],
                }
            )
            return
        states[key] = updated
        # If this path is result.json, the final-state reload handles review_result.
        if key == "result.json":
            activity["review_result"] = {}  # force reload from final state
        return

    content = args.get("content")
    if isinstance(content, str):
        states[key] = content
        # Immediate review_result load is kept for back-compat with write-only flows
        # (no later edit); _reload_review_result_from_final_state re-reads it.
        if key == "result.json":
            parsed = _json_loads(content)
            if isinstance(parsed, dict) and "findings" in parsed:
                activity["review_result"] = parsed
                summary = parsed.get("summary") or {}
                if isinstance(summary, dict) and isinstance(summary.get("affected_files"), list):
                    activity["changed_files"] = summary["affected_files"]


def _parse_numbered_json(content: str) -> Any:
    """Parse a JSON object from a ``cat -n`` style numbered listing.

    read_file results are rendered as ``"     1\t<line>"``; we strip the
    leading ``\\s*\\d+\\t`` prefix from every line before parsing. Returns the
    parsed JSON or None on failure — never raises.
    """

    if not isinstance(content, str) or not content.strip():
        return None

    cleaned_lines: list[str] = []
    for line in content.split("\n"):
        match = re.match(r"\s*\d+\t(.*)$", line)
        cleaned_lines.append(match.group(1) if match else line)
    parsed = _json_loads("\n".join(cleaned_lines))
    return parsed


def _maybe_load_review_result_from_text(activity: dict[str, Any], text: str) -> None:
    if activity.get("review_result"):
        return
    for match in RESULT_JSON_PATH_RE.finditer(text):
        path = Path(match.group("path"))
        if not path.exists() or not path.is_file():
            continue
        parsed = _json_loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(parsed, dict) or "findings" not in parsed:
            continue
        activity["review_result"] = parsed
        summary = parsed.get("summary") or {}
        if isinstance(summary, dict) and isinstance(summary.get("affected_files"), list):
            activity["changed_files"] = summary["affected_files"]
        activity["artifacts"].append(str(path))
        return


def _iter_tool_calls(data: Any) -> list[dict[str, Any]]:
    """Collect every tool call that actually happened in a trajectory.

    Real openjiuwen trajectories record tool calls in two places, and both must
    be read or the adapter silently drops evidence:

    1. Top-level ``kind=tool`` steps — ``detail.tool_name`` / ``call_args`` /
       ``call_result``. These capture *executed* calls. The ``tool_call_id``
       field is present but consistently ``None`` here, so the steps cannot be
       linked back to a specific assistant tool_call id.

    2. Inside every ``kind=llm`` step, ``detail.messages`` carries the full
       conversation history. Assistant messages carry ``tool_calls`` (OpenAI
       shape ``{id, function: {name, arguments}}``) and the matching
       ``role=tool`` messages carry ``tool_call_id`` + ``content``. The history
       is cumulative across consecutive llm steps, so the *same* tool_call id
       repeats — it must be deduplicated by id, not counted multiple times.

    Some calls (e.g. ``code_review_runner.py collect``) appear *only* in the
    conversation history and have no top-level ``kind=tool`` step at all. The
    strong-evidence fallback in :func:`_apply_artifact_evidence_fallback`
    recovers collect/diff evidence from ``read_file`` calls when the command
    itself is unreachable, so collect recognition does not depend solely on
    matching the command string.
    """

    by_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_sigs: set[str] = set()

    def _emit(call: dict[str, Any], *, source: str) -> None:
        tool_name = str(call.get("tool_name") or "")
        if not tool_name:
            return
        tcid = call.get("tool_call_id")
        if isinstance(tcid, str) and tcid:
            if tcid in seen_ids:
                return
            seen_ids.add(tcid)
            call["_source"] = source
            ordered.append(call)
            by_id[tcid] = call
            return
        # No id: dedupe by (tool_name, canonical args).
        sig = _call_signature(tool_name, call.get("call_args"))
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        call["_source"] = source
        ordered.append(call)

    # Pass 1: top-level tool steps (authoritative for executed calls + results).
    # A step is a tool call only when it carries a tool_name. ``kind=="tool"`` is
    # the normal shape, but older/edge-case trajectories may omit ``kind`` while
    # still recording a real call in ``detail.tool_name`` — keep that path, just
    # gate it on tool_name explicitly so a kind-less non-tool step (e.g. a
    # synthetic marker) is never mistaken for one.
    for step in data.get("steps") or [] if isinstance(data, dict) else []:
        if not isinstance(step, dict):
            continue
        detail = step.get("detail")
        if not isinstance(detail, dict):
            continue
        tool_name = str(detail.get("tool_name") or "")
        if not tool_name:
            continue
        if step.get("kind") == "tool" or "kind" not in step:
            _emit(
                {
                    "tool_name": tool_name,
                    "tool_call_id": detail.get("tool_call_id"),
                    "call_args": _decode_json_if_needed(detail.get("call_args")),
                    "call_result": _decode_json_if_needed(detail.get("call_result")),
                },
                source="top_level_step",
            )

    # Pass 2: recover tool calls from llm-step conversation history + response.
    for step in data.get("steps") or [] if isinstance(data, dict) else []:
        if not isinstance(step, dict) or step.get("kind") != "llm":
            continue
        detail = step.get("detail")
        if not isinstance(detail, dict):
            continue
        # Build a tool_call_id -> result-content map from role=tool messages.
        tool_results: dict[str, str] = {}
        messages = detail.get("messages") or []
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "tool":
                    continue
                tcid = str(message.get("tool_call_id") or "")
                content = message.get("content")
                if tcid and isinstance(content, str):
                    tool_results[tcid] = content
        # Emit each assistant tool_call once, paired with its result content.
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tcs = message.get("tool_calls") or []
            if not isinstance(tcs, list):
                continue
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                tool_name = str(fn.get("name") or "")
                if not tool_name:
                    continue
                tcid = str(tc.get("id") or "")
                args = _decode_json_if_needed(fn.get("arguments"))
                result_content = tool_results.get(tcid, "")
                _emit(
                    {
                        "tool_name": tool_name,
                        "tool_call_id": tcid or None,
                        "call_args": args,
                        "call_result": _synthesize_tool_result(result_content),
                    },
                    source="llm_message_history",
                )
        # Also honor the current turn's response.tool_calls (the assistant's
        # latest request) — paired with a tool result only if present above.
        response = detail.get("response")
        if isinstance(response, dict) and isinstance(response.get("tool_calls"), list):
            for tc in response["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                tool_name = str(fn.get("name") or "")
                if not tool_name:
                    continue
                tcid = str(tc.get("id") or "")
                args = _decode_json_if_needed(fn.get("arguments"))
                result_content = tool_results.get(tcid, "")
                _emit(
                    {
                        "tool_name": tool_name,
                        "tool_call_id": tcid or None,
                        "call_args": args,
                        "call_result": _synthesize_tool_result(result_content),
                    },
                    source="llm_response",
                )

    return ordered


def _call_signature(tool_name: str, call_args: Any) -> str:
    """Stable dedup signature for a tool call without an id."""
    import json as _json

    try:
        encoded = _json.dumps(call_args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = str(call_args)
    return f"{tool_name}|{encoded}"


def _synthesize_tool_result(content: str) -> dict[str, Any]:
    """Wrap a raw ``role=tool`` content string into the result shape the adapter reads.

    Top-level tool steps carry structured ``{success, data:{content}}`` results,
    but a tool call recovered from conversation history only has the raw tool
    message content string. We never *invent* success: if the content is empty
    we leave ``success`` unset (treated as not-succeeded); when content exists we
    mark ``success=True`` so downstream ``_call_succeeded`` / ``_content_from_result``
    helpers can read it. The strong-evidence fallback (not this synthesis) is what
    authoritatively proves collect/diff succeeded when a command was unreachable.
    """

    if not isinstance(content, str) or not content.strip():
        return {"data": {"content": ""}}
    return {"success": True, "data": {"content": content}}


def _extract_message_texts(data: Any) -> list[str]:
    """Collect review-relevant text from the conversation.

    Real trajectories keep the conversation inside each llm step's
    ``detail.messages`` (no top-level ``messages`` key), so we walk every llm
    step's messages in addition to a top-level ``messages`` list if present.
    """

    texts: list[str] = []
    if not isinstance(data, dict):
        return texts
    for source in (data.get("messages"),):
        if isinstance(source, list):
            for message in source:
                if isinstance(message, str):
                    texts.append(message)
                elif (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(message.get("content"), str)
                ):
                    texts.append(message.get("content") or "")
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("kind") != "llm":
            continue
        detail = step.get("detail")
        if not isinstance(detail, dict):
            continue
        messages = detail.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, str):
                texts.append(message)
            elif (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and isinstance(message.get("content"), str)
            ):
                texts.append(message.get("content") or "")
    return texts


def _extract_latest_user_text(data: Any) -> str:
    """Return only the latest user request, excluding tool/skill/history text."""

    user_texts: list[str] = []
    if not isinstance(data, dict):
        return ""
    top_messages = data.get("messages")
    if isinstance(top_messages, list):
        for message in top_messages:
            if isinstance(message, str):
                user_texts.append(message)
            elif (
                isinstance(message, dict)
                and message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ):
                user_texts.append(message["content"])
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("kind") != "llm":
            continue
        detail = step.get("detail")
        messages = detail.get("messages") if isinstance(detail, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ):
                user_texts.append(message["content"])
    return user_texts[-1] if user_texts else ""


def _resolve_pr_url(data: dict[str, Any], activity: dict[str, Any]) -> str:
    """Resolve the reviewed PR from authoritative current-run evidence.

    Priority is context.json, structured PR metadata, the executed collect
    command, then the latest user request.  Deliberately never scan the whole
    trajectory: skill documents, memory and older turns may contain stale PRs.
    """

    context_artifact = activity.get("context_artifact")
    if isinstance(context_artifact, dict):
        context = context_artifact.get("context")
        if isinstance(context, dict):
            url = _first_pr_url(str(context.get("pr") or ""))
            if url:
                return url

    metadata = activity.get("pr_metadata")
    if isinstance(metadata, dict):
        for key in ("pr_url", "web_url", "url"):
            url = _first_pr_url(str(metadata.get(key) or ""))
            if url:
                return url

    for command in reversed(activity.get("collect_commands") or []):
        url = _first_pr_url(str(command))
        if url:
            return url

    return _first_pr_url(_extract_latest_user_text(data))


def _decode_json_if_needed(value: Any) -> Any:
    if isinstance(value, str):
        parsed = _json_loads(value)
        return parsed if parsed is not None else value
    return value


def _json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _call_succeeded(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("success") is True:
            return True
        data = result.get("data")
        if isinstance(data, dict) and data.get("success") is True:
            return True
    return False


def _content_from_result(result: Any) -> str:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            return str(data.get("content") or "")
        return str(result.get("content") or "")
    return ""


def _json_from_result_content(result: Any) -> Any:
    content = _content_from_result(result)
    return _json_loads(content) if content else None


def _valid_location(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(LOCATION_RE.fullmatch(text))


def _valid_position(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) > 0
    return False


def _stringify(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_stringify(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_stringify(item) for item in value)
    return str(value)


def _contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def _first_pr_url(text: str) -> str:
    match = PR_URL_RE.search(text)
    return match.group(0) if match else ""


def _repo_from_url(pr_url: str) -> str:
    match = PR_URL_RE.search(pr_url)
    return f"{match.group('owner')}/{match.group('repo')}" if match else ""


def _pr_number_from_url(pr_url: str) -> int | None:
    match = PR_URL_RE.search(pr_url)
    return int(match.group("number")) if match else None


def _case_id_from_pr_url(pr_url: str) -> str:
    match = PR_URL_RE.fullmatch(pr_url)
    if not match:
        return ""
    safe = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        f"real_pr_{match.group('owner')}_{match.group('repo')}_{match.group('number')}",
    )
    return safe.strip("._")


def _find_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find_value(item, key)
            if found not in (None, "", [], {}):
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_value(item, key)
            if found not in (None, "", [], {}):
                return found
    return None


def _find_mapping_with_keys(value: Any, keys: set[str]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if keys.intersection(value):
            return value
        for item in value.values():
            found = _find_mapping_with_keys(item, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_mapping_with_keys(item, keys)
            if found is not None:
                return found
    return None
