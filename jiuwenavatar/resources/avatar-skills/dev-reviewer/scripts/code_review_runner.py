#!/usr/bin/env python3
"""PR code review helper.

Workflow:
1. collect               - collect PR diff, issue text, changed files, and repo context
2. init-review           - create doc/<module>/review/result.json draft
3. lint (optional)       - run lint/typecheck/format tools; collect lint_results.json
4. security-scan (opt.)  - secret/config/dependency scan; optional merge into result.json
5. performance-evidence  - generate measurement template and suggested benchmark commands
6. resolve-positions     - sync GitCode line-comment position from location + pr.diff
7. validate-comments     - validate Must/Should findings before GitCode comments
8. render-comments       - render one Markdown file per GitCode comment
9. post-comments         - dry-run or explicitly post rendered GitCode comments
10. report               - validate result.json, render Markdown body into review.md, exit PASS/FAIL

This runner does not perform unit tests. It is for code review only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from gitcode_diff_position import sync_finding_positions  # noqa: E402
from review_automation import (  # noqa: E402
    build_performance_evidence,
    merge_security_scan_into_result,
    run_lint,
    run_security_scan,
)
from review_git_scope import (  # noqa: E402
    COLLECT_FATAL_ERRORS,
    collect_git_snapshot,
    file_set_differs,
    local_git_diff,
    resolve_fetch_method,
)
from review_schema_validator import validate_review_result  # noqa: E402


REVIEW_MD = "review.md"
RESULT_JSON = "result.json"
COMMENTS_DIR = "comments"
COMMENTS_MANIFEST = "manifest.json"
COMMENT_SIGNATURE_PREFIX = "dev-reviewer:"
# Align with skills/dev-reviewer/SKILL.md: agent outer limit 60s per runner subcommand.
SUBPROCESS_TIMEOUT_SEC = 60
FETCH_TIMEOUT_SEC = 55
FETCH_FIRST_CANDIDATE_TIMEOUT_SEC = 25
FETCH_FALLBACK_CANDIDATE_TIMEOUT_SEC = 10
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
REPORT_BODY_NOTE = "<!-- 以下正文由 report 根据 doc/<module>/review/result.json 自动生成，请勿手改 -->"
DISCUSSION_LOCATIONS = {"(architecture)", "(documentation)"}

# Repo-local review standards discovery (docs/zh/开发实践/目标仓AI检视Skill加载方案.md).
# Primary convention: the agent-workspace skills directory, at the repo root or
# under a single-level package dir (e.g. jiuwenswarm/resources/agent/workspace/
# skills/<name>/SKILL.md). Platform project-skill directories are scanned for
# compatibility. AGENTS.md / CLAUDE.md are recorded as supplementary context.
REVIEW_SKILL_SLOT_GLOBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "resources-workspace-skills",
        (
            "resources/agent/workspace/skills/*/SKILL.md",
            "*/resources/agent/workspace/skills/*/SKILL.md",
        ),
    ),
    (
        "platform-project-skills",
        (
            ".claude/skills/*/SKILL.md",
            ".cursor/skills/*/SKILL.md",
            ".codex/skills/*/SKILL.md",
        ),
    ),
)
REPO_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
SKILL_REFERENCES_CAP = 50


def module_doc_dir(repo_root: Path, module: str) -> Path:
    name = module.strip().strip("/\\")
    if not name or name in {".", ".."}:
        raise SystemExit("--module must be a non-empty module name (doc/<module>/).")
    doc_root = (repo_root.resolve() / "doc").resolve()
    out = (doc_root / name).resolve()
    try:
        out.relative_to(doc_root)
    except ValueError as exc:
        raise SystemExit("--module must resolve under doc/<module>/.") from exc
    return out


def module_review_dir(repo_root: Path, module: str) -> Path:
    """Temporary evidence directory: doc/<module>/review/."""
    return module_doc_dir(repo_root, module) / "review"


def module_review_md_path(repo_root: Path, module: str) -> Path:
    """Primary review deliverable: doc/<module>/review.md."""
    return module_doc_dir(repo_root, module) / REVIEW_MD


def add_review_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--module",
        default="",
        help="Module name under doc/. Default temp dir: <repo-root>/doc/<module>/review/; report always writes review.md to doc/<module>/review.md when --module is set.",
    )
    parser.add_argument(
        "--repo-root",
        default="",
        help="Repository root containing doc/<module>/ (defaults to --repo on collect).",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Override review temp/evidence directory only (default: doc/<module>/review/ under repo-root). Does not change report review.md path when --module is set.",
    )


def resolve_out_dir(ns: argparse.Namespace) -> Path:
    if getattr(ns, "out_dir", ""):
        out = Path(ns.out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        return out

    module = (getattr(ns, "module", None) or "").strip()
    repo_root = (getattr(ns, "repo_root", None) or getattr(ns, "repo", None) or ".").strip()
    if module:
        out = module_review_dir(Path(repo_root), module)
        out.mkdir(parents=True, exist_ok=True)
        return out

    raise SystemExit(
        "Missing review output path: pass --module and --repo-root (or --repo on collect), or --out-dir."
    )


def run(cmd: list[str], cwd: Path | None = None, timeout: int = SUBPROCESS_TIMEOUT_SEC) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        cmd_str = " ".join(exc.cmd) if exc.cmd else " ".join(cmd)
        return 124, "", f"TimeoutExpired after {timeout}s: {cmd_str}"
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse_review_frontmatter(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def result_json_path(out_dir: Path) -> Path:
    return out_dir / RESULT_JSON


def compose_review_md_body(body_lines: list[str]) -> str:
    return "\n".join(body_lines).rstrip() + "\n"


def load_review_data(out_dir: Path, review_md_path: Path, context: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    limitations: list[str] = []
    review = read_json(result_json_path(out_dir))
    if review:
        return review, limitations

    review = parse_review_frontmatter(review_md_path)
    if review:
        limitations.append("Loaded legacy review.md frontmatter; report will write review/result.json.")
        return review, limitations

    legacy = read_json(out_dir / "review_result.json")
    if legacy:
        limitations.append("Loaded legacy review_result.json; report will write review/result.json.")
        return legacy, limitations

    review = default_review_result(context)
    limitations.append("review/result.json was missing or invalid.")
    return review, limitations


def resolve_review_md_path(ns: argparse.Namespace, out_dir: Path) -> Path:
    module = (getattr(ns, "module", None) or "").strip()
    repo_root = (getattr(ns, "repo_root", None) or getattr(ns, "repo", None) or ".").strip()
    if module:
        return module_review_md_path(Path(repo_root), module)
    return out_dir.parent / REVIEW_MD


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def is_gitcode_url(value: str) -> bool:
    return "gitcode." in value.lower() or "gitcode.com" in value.lower()


def looks_like_login_required(text: str) -> bool:
    sample = text[:5000].lower()
    return "<html" in sample and any(marker in sample for marker in ("login", "sign in", "signin", "登录", "登陆"))


def gitcode_token_help() -> str:
    return "GitCode requires an access token. Rerun collect with --gitcode-token <TOKEN> or set GITCODE_TOKEN before starting."


def add_gitcode_access_token(url: str, token: str) -> str:
    if not token or not is_gitcode_url(url):
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "access_token" for key, _ in query):
        query.append(("access_token", token))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def fetch_text(url: str, timeout: int = FETCH_TIMEOUT_SEC, token: str = "") -> tuple[bool, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 pr-agent-review/1.0",
        "Accept": "text/plain,text/html,*/*",
    }
    if token and not is_gitcode_url(url):
        headers["Authorization"] = f"Bearer {token}"
        headers["PRIVATE-TOKEN"] = token
    request_url = add_gitcode_access_token(url, token)
    req = urllib.request.Request(request_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return True, resp.read().decode(charset, errors="replace"), ""
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403} and is_gitcode_url(url):
            return False, "", f"HTTP {exc.code} while fetching GitCode URL. {gitcode_token_help()}"
        return False, "", f"HTTP {exc.code} while fetching {url}"
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"


def changed_files_from_diff(diff_text: str) -> list[str]:
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                if path != "/dev/null":
                    files.add(path)
        elif line.startswith("+++ b/"):
            files.add(line[6:])
    return sorted(files)


def changed_files_from_status(status_text: str) -> list[str]:
    files: set[str] = set()
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1].strip()
        if path:
            files.add(path.strip('"').replace("\\", "/"))
    return sorted(files)


def fetch_pr_diff(pr: str, token: str = "") -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not is_url(pr):
        return "", ["PR input is not a URL; remote diff fetch skipped."]
    if is_gitcode_url(pr) and not token:
        warnings.append("No GitCode access token was provided. Public PRs may still work; private/login-required PRs require --gitcode-token or GITCODE_TOKEN.")
    base = pr.rstrip("/")
    candidates = [base + ".diff", base + ".patch", base + "?format=diff", base + "?format=patch"]
    deadline = time.monotonic() + FETCH_TIMEOUT_SEC
    for index, url in enumerate(candidates):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            warnings.append("Remote diff fetch time budget exhausted before all fallback URLs were tried.")
            break
        candidate_timeout = FETCH_FIRST_CANDIDATE_TIMEOUT_SEC if index == 0 else FETCH_FALLBACK_CANDIDATE_TIMEOUT_SEC
        ok, text, err = fetch_text(url, timeout=max(1, int(min(candidate_timeout, remaining))), token=token)
        if ok and ("diff --git " in text or text.startswith("From ")):
            return text, warnings
        if ok and is_gitcode_url(url) and looks_like_login_required(text):
            warnings.append(gitcode_token_help())
            continue
        warnings.append(err or f"No diff found at {url}")
    return "", warnings


def read_issue(issue: str, token: str = "") -> tuple[str, str, list[str]]:
    if not issue:
        return "", "", []
    path = Path(issue)
    if path.exists():
        return path.read_text(encoding="utf-8-sig", errors="replace"), str(path), []
    if is_url(issue):
        ok, text, err = fetch_text(issue, token=token)
        if ok and is_gitcode_url(issue) and looks_like_login_required(text):
            return "", issue, [gitcode_token_help()]
        return (text, issue, []) if ok else ("", issue, [err])
    return issue, "inline", []


def detect_stack(repo: Path) -> dict[str, list[str]]:
    markers = {
        "python": ["pyproject.toml", "requirements.txt", "setup.py", "pytest.ini", "tox.ini"],
        "node": ["package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "tsconfig.json"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "dotnet": ["*.csproj", "*.sln"],
        "make": ["Makefile"],
    }
    found: dict[str, list[str]] = {}
    for stack, names in markers.items():
        hits: list[str] = []
        for name in names:
            if "*" in name:
                hits.extend(str(path.relative_to(repo)).replace("\\", "/") for path in repo.glob(name))
            elif (repo / name).exists():
                hits.append(name)
        if hits:
            found[stack] = sorted(hits)
    return found


def collect_project_context(repo: Path) -> dict[str, Any]:
    context: dict[str, Any] = {}
    code, stdout, stderr = run(["git", "status", "-sb"], cwd=repo, timeout=SUBPROCESS_TIMEOUT_SEC)
    context["git_status"] = stdout.strip() if code == 0 else stderr.strip()
    code, stdout, stderr = run(["git", "log", "-n", "10", "--oneline"], cwd=repo, timeout=SUBPROCESS_TIMEOUT_SEC)
    context["recent_commits"] = stdout.strip() if code == 0 else stderr.strip()
    interesting = [
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "tsconfig.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "Cargo.toml",
        "Makefile",
        "application.yml",
        "application.yaml",
        "application.properties",
    ]
    files: dict[str, str] = {}
    for name in interesting:
        path = repo / name
        if path.exists() and path.is_file():
            files[name] = "\n".join(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:220])
    context["key_files_head"] = files
    return context


def _normalize_repo_path(path: str) -> str:
    """Normalize a changed-file path for comparison (posix, no leading ./)."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _parse_skill_frontmatter(path: Path) -> dict[str, str]:
    """Best-effort ``name``/``description`` from a SKILL.md YAML frontmatter."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key in {"name", "description"} and value:
            fields[key] = value
    return fields


def _git_last_commit_date(repo: Path, rel: str) -> str:
    code, stdout, _ = run(["git", "log", "-1", "--format=%cs", "--", rel], cwd=repo, timeout=10)
    date = stdout.strip()
    return date if code == 0 and date else ""


def _skill_reference_files(repo: Path, skill_dir: Path) -> list[str]:
    """Repo-relative reference file list under a skill dir (SKILL.md excluded)."""
    refs: list[str] = []
    for item in sorted(skill_dir.rglob("*")):
        if not item.is_file() or item.name == "SKILL.md":
            continue
        try:
            refs.append(item.relative_to(repo).as_posix())
        except ValueError:
            continue
        if len(refs) >= SKILL_REFERENCES_CAP:
            break
    return refs


def _repo_standard_entry(
    repo: Path,
    file_path: Path,
    *,
    slot: str,
    kind: str,
    name: str,
    description: str = "",
    references: list[str] | None = None,
    changed: set[str] | None = None,
) -> dict[str, Any]:
    changed = changed or set()
    try:
        rel = file_path.relative_to(repo).as_posix()
    except ValueError:
        rel = str(file_path)
    if kind == "skill":
        # A PR touching the SKILL.md or anything under its dir (references,
        # assets) is treated as modifying the standard itself.
        prefix = str(Path(rel).parent.as_posix()) + "/"
        touched = rel in changed or any(p.startswith(prefix) for p in changed)
    else:
        touched = rel in changed
    return {
        "path": rel,
        "slot": slot,
        "kind": kind,
        "name": name,
        "description": description,
        "references": references or [],
        "last_commit": _git_last_commit_date(repo, rel),
        "modified_by_pr": touched,
    }


def _resolve_explicit_review_skill(repo: Path, raw: str) -> Path | None:
    """Resolve an explicit --review-skill value to a file (dir -> SKILL.md)."""
    target = Path(raw.strip()).expanduser()
    if not target.is_absolute():
        target = repo / target
    target = target.resolve()
    if target.is_dir():
        target = target / "SKILL.md"
    return target if target.is_file() else None


def discover_repo_review_standards(
    repo: Path,
    changed_files: list[str] | None = None,
    extra_paths: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Discover repo-local AI review standards; returns (entries, warnings).

    Scan order: explicit ``--review-skill`` paths first, then the slot globs in
    ``REVIEW_SKILL_SLOT_GLOBS`` (resources-workspace-skills is the primary
    convention), then repo-level instruction files (AGENTS.md / CLAUDE.md,
    ``kind="repo-instructions"``). Skills are deduplicated by name, keeping the
    highest-priority hit. File contents are intentionally NOT inlined — the
    reviewer reads the listed paths on demand.
    """
    changed = {_normalize_repo_path(p) for p in changed_files or [] if str(p or "").strip()}
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_names: set[str] = set()

    def add_skill(file_path: Path, slot: str) -> None:
        fields = _parse_skill_frontmatter(file_path)
        name = fields.get("name") or file_path.parent.name
        if name in seen_names:
            return
        seen_names.add(name)
        entry = _repo_standard_entry(
            repo,
            file_path,
            slot=slot,
            kind="skill",
            name=name,
            description=fields.get("description", ""),
            references=_skill_reference_files(repo, file_path.parent),
            changed=changed,
        )
        entries.append(entry)
        if entry["modified_by_pr"]:
            warnings.append(
                f"Repo review standard touched by this PR: {entry['path']}. "
                "Apply the PR base (target branch) version (a newly added standard does not "
                "apply to this review) and flag the change for maintainer confirmation."
            )

    for raw in extra_paths or []:
        resolved = _resolve_explicit_review_skill(repo, raw)
        if resolved is None:
            warnings.append(f"--review-skill path not found, skipped: {raw}")
            continue
        add_skill(resolved, "explicit")

    for slot, patterns in REVIEW_SKILL_SLOT_GLOBS:
        for pattern in patterns:
            for file_path in sorted(repo.glob(pattern)):
                add_skill(file_path, slot)

    for name in REPO_INSTRUCTION_FILES:
        file_path = repo / name
        if not file_path.is_file():
            continue
        entry = _repo_standard_entry(
            repo,
            file_path,
            slot="repo-instructions",
            kind="repo-instructions",
            name=name.removesuffix(".md"),
            changed=changed,
        )
        entries.append(entry)
        if entry["modified_by_pr"]:
            warnings.append(
                f"Repo instruction file touched by this PR: {entry['path']}. "
                "Apply the PR base (target branch) version (a newly added file does not "
                "apply to this review) and flag the change for maintainer confirmation."
            )

    return entries, warnings


def collect_hard_failure(pr: str, diff_text: str, changed_files: list[str], warnings: list[str]) -> bool:
    """True when local/URL collect produced no usable scope and errors are not recoverable by manual review alone."""
    if diff_text.strip() or changed_files:
        return False
    blob = " ".join(warnings).lower()
    if "timeoutexpired" in blob:
        return True
    if str(pr).strip().lower() == "local" and "not a git worktree" in blob:
        return True
    return False


def command_collect(ns: argparse.Namespace) -> int:
    repo = Path(ns.repo).resolve()
    if not ns.repo_root:
        ns.repo_root = str(repo)
    out_dir = resolve_out_dir(ns)
    gitcode_token = ns.gitcode_token or os.environ.get("GITCODE_TOKEN") or os.environ.get("GITCODE_ACCESS_TOKEN") or ""

    base = (ns.base or "").strip()
    head = (ns.head or "").strip()
    allow_wt = bool(getattr(ns, "allow_working_tree", False))
    git_snapshot = collect_git_snapshot(repo, run, timeout=SUBPROCESS_TIMEOUT_SEC)

    warnings: list[str] = []
    diff_text = ""
    changed_files: list[str] = []
    fetch_method = "none"
    diff_scope: dict[str, Any] = {}

    if is_url(ns.pr):
        remote_diff, remote_warnings = fetch_pr_diff(ns.pr, token=gitcode_token)
        warnings.extend(remote_warnings)
        if remote_diff:
            diff_text = remote_diff
            changed_files = changed_files_from_diff(remote_diff)
            fetch_method = "pr-url-diff"
            diff_scope["source"] = "pr-url"
            if base and head:
                local_result = local_git_diff(
                    repo,
                    base,
                    head,
                    run,
                    allow_working_tree=False,
                    changed_files_from_diff=changed_files_from_diff,
                    changed_files_from_status=changed_files_from_status,
                    timeout=SUBPROCESS_TIMEOUT_SEC,
                )
                warnings.extend(local_result.warnings)
                diff_scope["local_check"] = local_result.scope
                delta = file_set_differs(changed_files, local_result.changed_files)
                if delta:
                    preview = ", ".join(delta[:10])
                    if len(delta) > 10:
                        preview += " …"
                    warnings.append(
                        f"PR file list differs from local {local_result.scope.get('diff_spec')}: {preview}"
                    )
            else:
                warnings.append(
                    "PR diff collected without local cross-check; pass --base/--head to compare file lists."
                )

    if not diff_text:
        if not base or not head:
            if not allow_wt:
                print("[ERROR] collect requires --base and --head.", file=sys.stderr)
                return 2
        local_result = local_git_diff(
            repo,
            base,
            head,
            run,
            allow_working_tree=allow_wt,
            changed_files_from_diff=changed_files_from_diff,
            changed_files_from_status=changed_files_from_status,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
        warnings.extend(local_result.warnings)
        if (
            local_result.error_code in COLLECT_FATAL_ERRORS
            and not local_result.diff_text
            and not local_result.changed_files
        ):
            detail = local_result.warnings[-1] if local_result.warnings else local_result.error_code
            print(f"[ERROR] {detail}", file=sys.stderr)
            return 2
        diff_text = local_result.diff_text
        changed_files = local_result.changed_files
        diff_scope = local_result.scope
        fetch_method = resolve_fetch_method(diff_text, diff_scope)

    issue_text, issue_source, issue_warnings = read_issue(ns.issue, token=gitcode_token)
    warnings.extend(issue_warnings)

    (out_dir / "pr.diff").write_text(diff_text, encoding="utf-8")
    (out_dir / "issue.txt").write_text(issue_text, encoding="utf-8")
    module = (ns.module or "").strip()
    limitations: list[str] = []
    if git_snapshot.get("dirty") and fetch_method == "pr-url-diff":
        limitations.append("Local repo has uncommitted changes; checkout PR branch before lint.")
    repo_review_standards, standards_warnings = discover_repo_review_standards(
        repo,
        changed_files,
        extra_paths=list(getattr(ns, "review_skill", []) or []),
    )
    warnings.extend(standards_warnings)
    project_context = collect_project_context(repo)
    project_context["repo_review_standards"] = repo_review_standards
    context = {
        "schema_version": 1,
        "module": module,
        "review_dir": str(out_dir),
        "pr": ns.pr,
        "issue": ns.issue,
        "issue_source": issue_source,
        "repo": str(repo),
        "repo_root": str(Path(ns.repo_root or repo).resolve()),
        "base": base,
        "head": head,
        "fetch_method": fetch_method,
        "git_context": {**git_snapshot, **diff_scope},
        "gitcode_token_provided": bool(gitcode_token),
        "detected_stack": detect_stack(repo),
        "changed_files": changed_files,
        "diff_path": str(out_dir / "pr.diff"),
        "issue_text_path": str(out_dir / "issue.txt"),
        "project_context": project_context,
        "warnings": warnings,
        "limitations": limitations,
    }
    if not diff_text.strip():
        context["limitations"].append("No PR diff text was collected; inspect the local repo manually before reviewing.")
    if not changed_files:
        context["limitations"].append("No changed files were collected.")
    write_json(out_dir / "context.json", context)
    print(out_dir / "context.json")
    if collect_hard_failure(ns.pr, diff_text, changed_files, warnings):
        print(
            "[ERROR] collect failed: no diff or changed files (timeout, not a git worktree, or fetch error). "
            "See context.json warnings/limitations; retry with narrower scope or provide patch via Leader.",
            file=sys.stderr,
        )
        return 1
    return 0


def default_review_result(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "verdict": "FAIL",
        "gate_verdict": "REWORK",
        "verdict_reason": "Review has not been completed yet.",
        "layer_alignment": "FAIL",
        "patch_risk": "suspected",
        "risk_rating": "Unknown",
        "summary": {
            "change_intent": "Unknown until reviewer inspects diff and issue.",
            "affected_files": context.get("changed_files") or [],
            "scope": "PR code review only; no unit-test execution.",
        },
        "pass_fail_reasons": [
            "Initial draft only. The reviewer agent must inspect the diff and fill review findings."
        ],
        "findings": {
            "must_fix": [],
            "should_fix": [],
            "nice_to_have": [],
        },
        "security_review": {
            "status": "not_applicable",
            "items": [],
        },
        "testing_and_verification": [],
        "assumptions": [],
        "limitations": context.get("limitations") or [],
        "reviewer": "unfilled",
    }


def command_init_review(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    context = read_json(out_dir / "context.json")
    review = default_review_result(context)
    path = result_json_path(out_dir)
    write_json(path, review)
    print(path)
    return 0


def normalize_verdict(value: Any) -> str:
    text = str(value or "").strip().upper()
    return "PASS" if text == "PASS" else "FAIL"


def normalize_gate_verdict(value: Any, verdict: str) -> str:
    text = str(value or "").strip().upper()
    if text in {"REWORK", "HOLD"}:
        return text
    if text == "PASS" and verdict == "PASS":
        return "PASS"
    return "PASS" if verdict == "PASS" else "REWORK"


def report_exit_code(verdict: str, gate_verdict: str) -> int:
    """Exit 0 only when both verdict and gate_verdict are PASS."""
    normalized_verdict = normalize_verdict(verdict)
    normalized_gate = normalize_gate_verdict(gate_verdict, normalized_verdict)
    if normalized_verdict != "PASS":
        return 1
    if normalized_gate != "PASS":
        return 1
    return 0


def get_findings(review: dict[str, Any], key: str) -> list[dict[str, Any]]:
    findings = review.get("findings") or {}
    value = findings.get(key) or []
    return value if isinstance(value, list) else []


def bullet(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items if str(item).strip()] or ["- None recorded."]


def render_findings(lines: list[str], title: str, findings: list[dict[str, Any]]) -> None:
    lines.extend(["", f"## {title}", ""])
    if not findings:
        lines.append("None recorded.")
        return
    for index, finding in enumerate(findings, start=1):
        finding_id = finding.get("id") or f"CR-{index:03d}"
        severity = finding.get("severity") or "unknown"
        category = finding.get("category") or "general"
        location = finding.get("location") or "unknown"
        lines.extend(
            [
                f"### {finding_id} [{str(severity).upper()}] {category}",
                "",
                f"- Location: {location}",
                f"- Issue: {finding.get('issue') or 'Not specified.'}",
                f"- Risk: {finding.get('risk') or 'Not specified.'}",
                f"- Recommendation: {finding.get('recommendation') or 'Not specified.'}",
            ]
        )
        if finding.get("leader_escalate"):
            lines.append("- Leader escalate: **[Leader 建议升格]**")
        patch = str(finding.get("minimal_patch_example") or "").strip()
        if patch:
            fence = "```"
            if "```" in patch:
                fence = "````"
            lines.extend(["", f"{fence}diff", patch, fence])
        lines.append("")


def finding_buckets_for_comments(review: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    return [
        ("must_fix", get_findings(review, "must_fix")),
        ("should_fix", get_findings(review, "should_fix")),
    ]


def is_discussion_location(location: str) -> bool:
    return location.strip().lower() in DISCUSSION_LOCATIONS


def first_non_empty(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def normalize_comment_examples(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item).strip()]


def validate_finding_substance(finding: dict[str, Any], bucket: str) -> list[str]:
    """Return quality errors that would make a long PR comment too generic."""
    if bucket != "must_fix":
        return []
    finding_id = first_non_empty(finding.get("id"), "CR-UNKNOWN")
    comment = finding.get("comment") if isinstance(finding.get("comment"), dict) else {}
    checks = {
        "issue": first_non_empty(finding.get("issue")),
        "risk/comment.impact": first_non_empty(finding.get("risk"), comment.get("impact")),
        "recommendation/comment.fix": first_non_empty(finding.get("recommendation"), comment.get("fix")),
    }
    errors = [f"{finding_id}: Must Fix missing substantive {name}" for name, value in checks.items() if not value]
    if comment and not first_non_empty(comment.get("scenario")):
        errors.append(f"{finding_id}: Must Fix comment.scenario is required when comment is provided")
    if comment and not first_non_empty(comment.get("verification")):
        errors.append(f"{finding_id}: Must Fix comment.verification is required when comment is provided")
    return errors


def comment_signature(finding_id: str) -> str:
    return f"<!-- {COMMENT_SIGNATURE_PREFIX}{finding_id} -->"


def comment_severity_label(bucket: str) -> tuple[str, str]:
    if bucket == "must_fix":
        return "严重", "Must Fix"
    return "建议", "Should Fix"


def infer_code_fence_language(code: str) -> str:
    sample = code.lstrip()
    if sample.startswith("diff --git") or sample.startswith("@@") or sample.startswith(("+", "-")):
        return "diff"
    return ""


def normalize_code_fence_language(value: Any) -> str:
    language = str(value or "").strip().lower()
    if re.match(r"^[a-z0-9][a-z0-9_+.-]{0,30}$", language):
        return language
    return ""


def render_review_comment(finding: dict[str, Any], bucket: str) -> str:
    finding_id = first_non_empty(finding.get("id"), "CR-UNKNOWN")
    comment = finding.get("comment") if isinstance(finding.get("comment"), dict) else {}
    dimension = first_non_empty(finding.get("dimension"), "Code")
    severity_cn, severity_en = comment_severity_label(bucket)
    issue = first_non_empty(finding.get("issue"), comment.get("issue"), default="未填写问题描述。")
    title = first_non_empty(
        comment.get("title"),
        issue.splitlines()[0],
        f"{finding_id} 检视意见",
    )
    scenario = first_non_empty(comment.get("scenario"), issue)
    impact = first_non_empty(comment.get("impact"), finding.get("risk"), default="可能影响相关代码路径的正确性、稳定性或可维护性。")
    fix = first_non_empty(comment.get("fix"), finding.get("recommendation"), default="请按上述场景补充修复，并保持与现有设计和测试计划一致。")
    verification = first_non_empty(
        comment.get("verification"),
        "补充或执行覆盖该场景的单元测试/回归验证，确认修复后不会再次触发该问题。",
    )
    code = first_non_empty(comment.get("code"), finding.get("minimal_patch_example"))
    examples = normalize_comment_examples(comment.get("examples"))

    lines = [
        f"**[{severity_cn}][{severity_en}][{dimension}]** {title}",
        "",
        f"**问题：** {issue}",
        "",
        f"**触发场景：** {scenario}",
    ]
    if examples:
        lines.extend(["", "例如："])
        lines.extend(f"- {example}" for example in examples)
    lines.extend(
        [
            "",
            f"**影响：** {impact}",
            "",
            f"**建议修复：** {fix}",
        ]
    )
    if code:
        fence = "```"
        if "```" in code:
            fence = "````"
        language = normalize_code_fence_language(comment.get("code_language")) or infer_code_fence_language(code)
        lines.extend(["", f"{fence}{language}", code.rstrip(), fence])
    lines.extend(["", f"**验证建议：** {verification}", "", comment_signature(finding_id)])
    return "\n".join(lines).rstrip() + "\n"


def comments_dir_path(out_dir: Path) -> Path:
    return out_dir / COMMENTS_DIR


def comments_manifest_path(out_dir: Path) -> Path:
    return comments_dir_path(out_dir) / COMMENTS_MANIFEST


def safe_comment_filename(finding_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", finding_id.strip() or "CR-UNKNOWN")
    return f"{safe}.md"


def build_comment_manifest_item(
    finding: dict[str, Any],
    bucket: str,
    out_dir: Path,
) -> dict[str, Any]:
    finding_id = first_non_empty(finding.get("id"), "CR-UNKNOWN")
    location = first_non_empty(finding.get("location"))
    mode = "discussion" if is_discussion_location(location) else "inline"
    comment_file = comments_dir_path(out_dir) / safe_comment_filename(finding_id)
    item: dict[str, Any] = {
        "id": finding_id,
        "bucket": bucket,
        "location": location,
        "path": first_non_empty(finding.get("path")),
        "position": finding.get("position"),
        "comment_file": str(comment_file.resolve()),
        "need_to_resolve": True,
        "mode": mode,
        "signature": comment_signature(finding_id),
        "status": "pending",
    }
    if mode == "discussion":
        item["path"] = ""
        item["position"] = None
    return item


def build_report_body_lines(
    review: dict[str, Any],
    context: dict[str, Any],
    *,
    verdict: str,
    gate_verdict: str,
    pass_fail_reasons: list[str],
    limitations: list[str],
    warnings: list[str],
    evidence_files: list[str],
) -> list[str]:
    must_fix = get_findings(review, "must_fix")
    should_fix = get_findings(review, "should_fix")
    nice_to_have = get_findings(review, "nice_to_have")
    summary = review.get("summary") or {}
    security_review = review.get("security_review") or {}
    standards = (context.get("project_context") or {}).get("repo_review_standards") or []
    standard_paths = [
        str(item.get("path")) for item in standards if isinstance(item, dict) and item.get("path")
    ]
    lines: list[str] = [
        REPORT_BODY_NOTE,
        "",
        "# Code Review Report",
        "",
        "## Target",
        "",
        f"- PR: {context.get('pr') or 'unknown'}",
        f"- Issue: {context.get('issue') or 'none'}",
        f"- Repository: {context.get('repo') or 'unknown'}",
        f"- Diff source: {context.get('fetch_method') or 'unknown'}",
        f"- Changed files: {', '.join(context.get('changed_files') or []) or 'none collected'}",
        f"- Repo review standards: {', '.join(standard_paths) or 'none discovered'}",
        "",
        "## Overall Verdict",
        "",
        verdict,
        "",
        f"- Gate verdict: {gate_verdict}",
        f"- Layer alignment: {review.get('layer_alignment') or 'Unknown'}",
        f"- Patch risk: {review.get('patch_risk') or 'unknown'}",
        f"- Risk rating: {review.get('risk_rating') or 'Unknown'}",
        f"- Verdict reason: {review.get('verdict_reason') or 'Not specified.'}",
        "",
        "## Pass/Fail Reasons",
        *bullet(pass_fail_reasons),
        "",
        "## Review Summary",
        "",
        f"- Change intent: {summary.get('change_intent') or 'Unknown'}",
        f"- Scope: {summary.get('scope') or 'PR code review only.'}",
        f"- Affected files: {', '.join(summary.get('affected_files') or context.get('changed_files') or []) or 'none recorded'}",
    ]
    render_findings(lines, "Must Fix", must_fix)
    render_findings(lines, "Should Fix", should_fix)
    render_findings(lines, "Nice to Have", nice_to_have)
    lines.extend(["", "## Security and Compliance", "", f"- Status: {security_review.get('status') or 'not_applicable'}"])
    items = security_review.get("items") or []
    if items:
        lines.extend(["", "| Category | Status | Evidence |", "| --- | --- | --- |"])
        for item in items:
            lines.append(f"| {item.get('category', '')} | {item.get('status', '')} | {item.get('evidence', '')} |")
    else:
        lines.append("- No security checklist items recorded.")
    verification = review.get("testing_and_verification") or []
    lines.extend(["", "## Testing and Verification Suggestions", ""])
    if verification:
        for item in verification:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('item') or 'Check'}: `{item.get('command') or 'manual review'}` - {item.get('reason') or ''}"
                )
            else:
                lines.append(f"- {item}")
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Assumptions", *bullet([str(item) for item in review.get("assumptions") or []])])
    lines.extend(["", "## Risks and Limitations", *bullet([str(item) for item in limitations + warnings])])
    lines.extend(["", "## Evidence Files", *bullet(evidence_files)])
    return lines


def apply_gitcode_position_sync(out_dir: Path, review: dict[str, Any]) -> list[str]:
    diff_path = out_dir / "pr.diff"
    if not diff_path.is_file():
        return []
    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    return sync_finding_positions(review, diff_text)


def validate_review_comments(review: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    location_re = re.compile(r"^.+?:\d+(?:-\d+)?$")

    for bucket, findings in finding_buckets_for_comments(review):
        for index, finding in enumerate(findings, start=1):
            finding_id = first_non_empty(finding.get("id"), f"{bucket}-{index}")
            if finding_id in seen_ids:
                errors.append(f"{finding_id}: duplicate finding id")
            seen_ids.add(finding_id)

            body = render_review_comment(finding, bucket).strip()
            if not body:
                errors.append(f"{finding_id}: rendered comment body is empty")
            errors.extend(validate_finding_substance(finding, bucket))

            location = first_non_empty(finding.get("location"))
            if not location:
                errors.append(f"{finding_id}: missing location")
                continue
            if is_discussion_location(location):
                warnings.append(f"{finding_id}: {location} will be posted as a discussion comment")
                continue
            if not location_re.match(location):
                errors.append(f"{finding_id}: invalid location {location!r}")
                continue

            path = first_non_empty(finding.get("path"))
            position = finding.get("position")
            if not path:
                errors.append(f"{finding_id}: missing path; run resolve-positions or report first")
            if not isinstance(position, int) or position <= 0:
                errors.append(f"{finding_id}: missing valid position; run resolve-positions and ensure location is inside the diff")

    return errors, warnings


def command_validate_comments(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    result_path = result_json_path(out_dir)
    if not result_path.is_file():
        raise SystemExit(f"Missing {result_path}")
    review = read_json(result_path)
    position_notes = apply_gitcode_position_sync(out_dir, review)
    errors, warnings = validate_review_comments(review)
    if not (out_dir / "pr.diff").is_file():
        errors.append("missing pr.diff; run collect before validate-comments")
    write_json(result_path, review)
    payload = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "position_notes": position_notes,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def render_comment_files(out_dir: Path, review: dict[str, Any]) -> dict[str, Any]:
    comments_dir = comments_dir_path(out_dir)
    comments_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for bucket, findings in finding_buckets_for_comments(review):
        for finding in findings:
            item = build_comment_manifest_item(finding, bucket, out_dir)
            comment_path = Path(item["comment_file"])
            comment_path.write_text(render_review_comment(finding, bucket), encoding="utf-8")
            items.append(item)
    manifest = {
        "schema_version": 1,
        "generated_by": "dev-reviewer",
        "items": items,
    }
    write_json(comments_manifest_path(out_dir), manifest)
    return manifest


def command_render_comments(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    result_path = result_json_path(out_dir)
    if not result_path.is_file():
        raise SystemExit(f"Missing {result_path}")
    review = read_json(result_path)
    position_notes = apply_gitcode_position_sync(out_dir, review)
    errors, warnings = validate_review_comments(review)
    if not (out_dir / "pr.diff").is_file():
        errors.append("missing pr.diff; run collect before render-comments")
    write_json(result_path, review)
    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings, "position_notes": position_notes}, ensure_ascii=False, indent=2))
        return 1
    manifest = render_comment_files(out_dir, review)
    payload = {
        "ok": True,
        "manifest": str(comments_manifest_path(out_dir)),
        "comment_count": len(manifest.get("items") or []),
        "warnings": warnings,
        "position_notes": position_notes,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _repo_root_from_ns(ns: argparse.Namespace) -> Path:
    repo_root = (getattr(ns, "repo_root", None) or getattr(ns, "repo", None) or ".").strip()
    return Path(repo_root).resolve()


def _stack_from_context(context: dict[str, Any], repo: Path) -> dict[str, list[str]]:
    stack = context.get("detected_stack")
    if isinstance(stack, dict) and stack:
        return stack
    return detect_stack(repo)


def command_lint(ns: argparse.Namespace) -> int:
    repo = _repo_root_from_ns(ns)
    out_dir = resolve_out_dir(ns)
    context = read_json(out_dir / "context.json")
    stack = _stack_from_context(context, repo)
    changed_files = context.get("changed_files") or None
    payload = run_lint(repo, stack, run, SUBPROCESS_TIMEOUT_SEC, changed_files=changed_files)
    out_path = out_dir / "lint_results.json"
    write_json(out_path, payload)
    print(out_path)
    if payload.get("status") == "no_tools":
        print("[WARN] lint: no lint/typecheck tools detected for the project stack.", file=sys.stderr)
        return 2
    attempted = payload.get("tools_attempted", 0)
    passed = payload.get("tools_passed", 0)
    return 0 if passed == attempted else 1


def command_security_scan(ns: argparse.Namespace) -> int:
    repo = _repo_root_from_ns(ns)
    out_dir = resolve_out_dir(ns)
    context = read_json(out_dir / "context.json")
    stack = _stack_from_context(context, repo)
    changed_files = context.get("changed_files") or []
    scan = run_security_scan(repo, stack, run, SUBPROCESS_TIMEOUT_SEC, changed_files=changed_files)
    out_path = out_dir / "security_scan.json"
    write_json(out_path, scan)
    exit_code = 0
    if ns.merge_result:
        merge_errors = merge_security_scan_into_result(result_json_path(out_dir), scan)
        for msg in merge_errors:
            print(f"[WARN] merge-result: {msg}", file=sys.stderr)
            exit_code = 1
    print(out_path)
    failed = not (scan.get("secret_scan") or {}).get("passed", True)
    failed = failed or not (scan.get("config_scan") or {}).get("passed", True)
    dep = scan.get("dependency_audit") or {}
    if dep.get("passed") is False:
        failed = True
    for audit in dep.get("audits") or []:
        if audit.get("passed") is False:
            failed = True
    return 1 if failed or exit_code else 0


def command_performance_evidence(ns: argparse.Namespace) -> int:
    repo = _repo_root_from_ns(ns)
    out_dir = resolve_out_dir(ns)
    context = read_json(out_dir / "context.json")
    stack = _stack_from_context(context, repo)
    payload = build_performance_evidence(repo, stack, context)
    out_path = out_dir / "performance_evidence.json"
    write_json(out_path, payload)
    print(out_path)
    return 0


def default_gitcode_repo_root() -> Path:
    return (_SCRIPT_DIR.parent.parent / "gitcode-repo").resolve()


def build_pr_commenter_command(
    item: dict[str, Any],
    *,
    number: int,
    gitcode_repo_root: Path,
    config: str,
    workspace: str,
    target_project: str,
    dry_run: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        str(gitcode_repo_root / "scripts" / "pr_commenter.py"),
        "--number",
        str(number),
        "--comment-file",
        str(item["comment_file"]),
        "--target-project",
        target_project,
    ]
    if item.get("mode") == "inline":
        cmd.extend(["--path", str(item.get("path") or ""), "--position", str(item.get("position"))])
    elif item.get("mode") == "discussion":
        cmd.append("--allow-review-discussion-comment")
    if item.get("need_to_resolve"):
        cmd.append("--need-to-resolve")
    if config:
        cmd.extend(["--config", config])
    if workspace:
        cmd.extend(["--workspace", workspace])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def load_comment_manifest(out_dir: Path) -> dict[str, Any]:
    manifest_path = comments_manifest_path(out_dir)
    if not manifest_path.is_file():
        raise SystemExit(f"Missing {manifest_path}; run render-comments first.")
    manifest = read_json(manifest_path)
    if not isinstance(manifest.get("items"), list):
        raise SystemExit(f"Invalid comments manifest: {manifest_path}")
    return manifest


COMMENT_SIGNATURE_RE = re.compile(r"<!--\s*dev-reviewer:([^>\s]+)\s*-->")


def dev_reviewer_dedup_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remote comments that participate in post-time dedup.

    Only unresolved dev-reviewer comments count: a resolved comment must not
    block re-posting (a re-review may legitimately re-find the same issue, and
    per-run sequential finding ids get reused across review rounds).
    """
    dedup: list[dict[str, Any]] = []
    for comment in comments:
        if comment.get("resolved") is True:
            continue
        if "<!-- dev-reviewer:" in str(comment.get("body") or ""):
            dedup.append(comment)
    return dedup


def normalize_comment_body(body: str) -> str:
    """Strip dev-reviewer signatures and collapse whitespace (fingerprint input)."""
    return " ".join(COMMENT_SIGNATURE_RE.sub(" ", body or "").split())


def comment_fingerprint(path: str, body: str) -> str:
    """Content fingerprint: normalized path + signature-stripped comment body.

    Line position is deliberately excluded (positions drift between PR
    versions). sha1 is a plain dedup hash here, not a security primitive.
    Returns "" when the body is empty after normalization; callers treat that
    as "no fingerprint, never skip".
    """
    normalized = normalize_comment_body(body)
    if not normalized:
        return ""
    key = f"{_normalize_repo_path(path)}\n{normalized}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def extract_dev_reviewer_signatures(comments: list[dict[str, Any]]) -> set[str]:
    signatures: set[str] = set()
    for comment in dev_reviewer_dedup_comments(comments):
        for match in COMMENT_SIGNATURE_RE.finditer(str(comment.get("body") or "")):
            signatures.add(match.group(1))
    return signatures


def extract_dev_reviewer_fingerprints(comments: list[dict[str, Any]]) -> set[str]:
    fingerprints: set[str] = set()
    for comment in dev_reviewer_dedup_comments(comments):
        fingerprint = comment_fingerprint(
            str(comment.get("path") or ""),
            str(comment.get("body") or ""),
        )
        if fingerprint:
            fingerprints.add(fingerprint)
    return fingerprints


def comment_item_fingerprint(item: dict[str, Any]) -> str:
    """Fingerprint of a pending manifest item; "" when its comment file is unreadable."""
    comment_file = str(item.get("comment_file") or "")
    try:
        body = read_text(Path(comment_file))
    except OSError:
        return ""
    return comment_fingerprint(str(item.get("path") or ""), body)


def fetch_existing_comment_state(
    *,
    number: int,
    gitcode_repo_root: Path,
    config: str,
    workspace: str,
    target_project: str,
) -> tuple[set[str], set[str], str]:
    """Fetch (signatures, fingerprints, error) of a PR's existing remote comments."""
    cmd = [
        sys.executable,
        str(gitcode_repo_root / "scripts" / "pr_creator.py"),
        "--number",
        str(number),
        "--target-project",
        target_project,
    ]
    if config:
        cmd.extend(["--config", config])
    if workspace:
        cmd.extend(["--workspace", workspace])
    code, stdout, stderr = run(cmd, cwd=gitcode_repo_root, timeout=SUBPROCESS_TIMEOUT_SEC)
    if code != 0:
        return set(), set(), stderr.strip() or stdout.strip() or "failed to fetch PR comments"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return set(), set(), f"failed to parse PR comments JSON: {exc}"
    comments = payload.get("comments") or []
    if not isinstance(comments, list):
        return set(), set(), "PR comments payload did not contain comments[]"
    return (
        extract_dev_reviewer_signatures(comments),
        extract_dev_reviewer_fingerprints(comments),
        "",
    )


def update_manifest_item_from_result(item: dict[str, Any], stdout: str) -> None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, list) or not payload:
        return
    first = payload[0] if isinstance(payload[0], dict) else {}
    if first.get("comment_id") is not None:
        item["comment_id"] = first.get("comment_id")
    if first.get("html_url"):
        item["html_url"] = first.get("html_url")


def command_post_comments(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    manifest = load_comment_manifest(out_dir)
    items = manifest.get("items") or []
    gitcode_repo_root = Path(ns.gitcode_repo_root or default_gitcode_repo_root()).resolve()
    dry_run = not bool(ns.execute)
    if not (gitcode_repo_root / "scripts" / "pr_commenter.py").is_file():
        raise SystemExit(f"Missing pr_commenter.py under {gitcode_repo_root}")

    # Remote comments are the dedup source of truth. Refresh before every
    # attempted post (not once upfront) so a concurrent review run of the same
    # PR cannot double-post; the unions keep this run's own posts in memory
    # even when the remote read lags.
    existing_signatures: set[str] = set()
    existing_fingerprints: set[str] = set()

    results: list[dict[str, Any]] = []
    failures = 0
    for item in items:
        finding_id = str(item.get("id") or "")
        cmd = build_pr_commenter_command(
            item,
            number=ns.number,
            gitcode_repo_root=gitcode_repo_root,
            config=ns.config,
            workspace=ns.workspace,
            target_project=ns.target_project,
            dry_run=dry_run,
        )
        if item.get("status") == "posted" and item.get("comment_id"):
            results.append({"id": finding_id, "status": "skipped_manifest", "command": cmd})
            continue
        fingerprint = ""
        if ns.execute:
            signatures, fingerprints, fetch_error = fetch_existing_comment_state(
                number=ns.number,
                gitcode_repo_root=gitcode_repo_root,
                config=ns.config,
                workspace=ns.workspace,
                target_project=ns.target_project,
            )
            if fetch_error:
                failures += 1
                item["status"] = "failed"
                item["error"] = f"failed to refresh existing comments: {fetch_error}"
                results.append({"id": finding_id, "status": "failed", "command": cmd, "error": item["error"]})
                break
            existing_signatures |= signatures
            existing_fingerprints |= fingerprints
            if finding_id in existing_signatures:
                item["status"] = "skipped_existing"
                results.append({"id": finding_id, "status": "skipped_existing", "command": cmd})
                continue
            # Same content under a different finding id: two review runs of the
            # same PR renumber findings, so ids alone are not enough.
            fingerprint = comment_item_fingerprint(item)
            if fingerprint and fingerprint in existing_fingerprints:
                item["status"] = "skipped_duplicate"
                results.append({"id": finding_id, "status": "skipped_duplicate", "command": cmd})
                continue
        if dry_run:
            results.append({"id": finding_id, "status": "dry_run", "command": cmd})
            continue

        code, stdout, stderr = run(cmd, cwd=gitcode_repo_root, timeout=SUBPROCESS_TIMEOUT_SEC)
        item["last_stdout"] = stdout.strip()
        item["last_stderr"] = stderr.strip()
        if code == 0:
            item["status"] = "posted"
            item.pop("error", None)
            update_manifest_item_from_result(item, stdout)
            existing_signatures.add(finding_id)
            if fingerprint:
                existing_fingerprints.add(fingerprint)
            results.append({"id": finding_id, "status": "posted", "command": cmd})
        else:
            failures += 1
            item["status"] = "failed"
            item["error"] = stderr.strip() or stdout.strip() or f"exit code {code}"
            results.append({"id": finding_id, "status": "failed", "command": cmd, "error": item["error"]})
            break

    write_json(comments_manifest_path(out_dir), manifest)
    print(
        json.dumps(
            {
                "ok": failures == 0,
                "dry_run": dry_run,
                "manifest": str(comments_manifest_path(out_dir)),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if failures == 0 else 1


def command_resolve_positions(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    result_path = result_json_path(out_dir)
    if not result_path.is_file():
        raise SystemExit(f"Missing {result_path}")
    review = read_json(result_path)
    messages = apply_gitcode_position_sync(out_dir, review)
    for line in messages:
        print(line)
    if not messages:
        print("no position updates")
    write_json(result_path, review)
    return 0


def command_report(ns: argparse.Namespace) -> int:
    out_dir = resolve_out_dir(ns)
    review_md_path = resolve_review_md_path(ns, out_dir)
    context = read_json(out_dir / "context.json")
    review, load_limitations = load_review_data(out_dir, review_md_path, context)
    if not getattr(ns, "skip_schema_validation", False):
        schema_errors = validate_review_result(review)
        if schema_errors:
            print("[ERROR] result.json failed schema validation:", file=sys.stderr)
            for err in schema_errors:
                print(f"  - {err}", file=sys.stderr)
            return 2
    position_notes = apply_gitcode_position_sync(out_dir, review)
    if position_notes:
        load_limitations.append(
            "GitCode position synced from location+pr.diff: "
            + "; ".join(position_notes[:8])
            + (" …" if len(position_notes) > 8 else "")
        )
    review["limitations"] = list(review.get("limitations") or []) + load_limitations

    verdict = normalize_verdict(review.get("verdict"))
    gate_verdict = normalize_gate_verdict(review.get("gate_verdict"), verdict)
    limitations = list(context.get("limitations") or []) + list(review.get("limitations") or [])
    warnings = list(context.get("warnings") or [])
    pass_fail_reasons = [str(item) for item in review.get("pass_fail_reasons") or []]

    must_fix = get_findings(review, "must_fix")
    if must_fix and verdict != "FAIL":
        verdict = "FAIL"
        gate_verdict = normalize_gate_verdict(review.get("gate_verdict"), verdict)
        pass_fail_reasons.append("FAIL because Must Fix findings exist.")

    review["verdict"] = verdict
    review["gate_verdict"] = gate_verdict
    layer_alignment = str(review.get("layer_alignment") or "").strip().upper()
    patch_risk = str(review.get("patch_risk") or "").strip().lower()
    if layer_alignment == "FAIL" or patch_risk == "confirmed":
        if review["gate_verdict"] == "PASS":
            review["gate_verdict"] = "REWORK"
            gate_verdict = "REWORK"
            pass_fail_reasons.append(
                "Gate downgraded to REWORK due to layer_alignment=FAIL or patch_risk=confirmed."
            )

    evidence_files = [
        str(out_dir / name)
        for name in (
            "context.json",
            "pr.diff",
            "issue.txt",
            RESULT_JSON,
            "lint_results.json",
            "security_scan.json",
            "performance_evidence.json",
        )
        if (out_dir / name).exists()
    ]
    evidence_files.append(str(review_md_path))

    final_gate = str(review.get("gate_verdict") or gate_verdict)
    body_lines = build_report_body_lines(
        review,
        context,
        verdict=verdict,
        gate_verdict=final_gate,
        pass_fail_reasons=pass_fail_reasons,
        limitations=limitations,
        warnings=warnings,
        evidence_files=evidence_files,
    )

    write_json(result_json_path(out_dir), review)
    review_md_path.parent.mkdir(parents=True, exist_ok=True)
    review_md_path.write_text(compose_review_md_body(body_lines), encoding="utf-8")
    print(review_md_path)
    return report_exit_code(verdict, final_gate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review PR code changes and generate PASS/FAIL report.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="Collect PR diff, issue, changed files, and project context.")
    p.add_argument("--pr", required=True)
    p.add_argument("--issue", default="")
    p.add_argument("--repo", default=".")
    p.add_argument("--base", default="", help="Merge target ref for local diff, e.g. origin/main.")
    p.add_argument("--head", default="", help="PR tip ref for local diff, e.g. HEAD or feature branch.")
    p.add_argument(
        "--allow-working-tree",
        action="store_true",
        help="Allow staged/working-tree diff when --base/--head omitted. Not for PR review.",
    )
    add_review_output_args(p)
    p.add_argument("--gitcode-token", default="", help="GitCode access token for private/login-required PR or issue URLs.")
    p.add_argument(
        "--review-skill",
        action="append",
        default=[],
        help="Repo-relative path to a repo-local review standard \
        (SKILL.md, skill dir, or instruction file). Repeatable; takes priority over slot discovery.",
    )
    p.set_defaults(func=command_collect)

    p = sub.add_parser("init-review", help="Create doc/<module>/review/result.json draft.")
    add_review_output_args(p)
    p.set_defaults(func=command_init_review)

    p = sub.add_parser(
        "lint",
        help="Run project lint/typecheck/format checks (collect-only); writes lint_results.json.",
    )
    add_review_output_args(p)
    p.set_defaults(func=command_lint)

    p = sub.add_parser(
        "security-scan",
        help="Run secret/config/dependency scans; writes security_scan.json and optionally merges security_review.items.",
    )
    add_review_output_args(p)
    p.add_argument(
        "--merge-result",
        action="store_true",
        help="Merge scan items into review/result.json security_review.items when result.json exists.",
    )
    p.set_defaults(func=command_security_scan)

    p = sub.add_parser(
        "performance-evidence",
        help="Generate performance measurement template and suggested commands; writes performance_evidence.json.",
    )
    add_review_output_args(p)
    p.set_defaults(func=command_performance_evidence)

    p = sub.add_parser(
        "resolve-positions",
        help="Set finding position/path for GitCode from location + pr.diff (new-file line numbers).",
    )
    add_review_output_args(p)
    p.set_defaults(func=command_resolve_positions)

    p = sub.add_parser(
        "validate-comments",
        help="Validate Must/Should findings before rendering or posting GitCode review comments.",
    )
    add_review_output_args(p)
    p.set_defaults(func=command_validate_comments)

    p = sub.add_parser(
        "render-comments",
        help="Render one Markdown comment file per Must/Should finding and write comments/manifest.json.",
    )
    add_review_output_args(p)
    p.add_argument("--dry-run", action="store_true", help="Accepted for workflow symmetry; render-comments never calls GitCode APIs.")
    p.set_defaults(func=command_render_comments)

    p = sub.add_parser(
        "post-comments",
        help="Dry-run or explicitly post rendered GitCode review comments from comments/manifest.json.",
    )
    add_review_output_args(p)
    p.add_argument("--number", type=int, required=True, help="GitCode PR number.")
    p.add_argument("--config", default="", help="gitcode-repo.json path.")
    p.add_argument("--workspace", default="", help="gitcode-repo workspace name.")
    p.add_argument("--target-project", default="upstream", choices=["fork", "upstream"], help="PR target project.")
    p.add_argument("--gitcode-repo-root", default="", help="Path to skills/gitcode-repo; defaults to sibling skill directory.")
    p.add_argument("--execute", action="store_true", help="Actually call GitCode write APIs. Omit for dry-run.")
    p.set_defaults(func=command_post_comments)

    p = sub.add_parser(
        "report",
        help="Render doc/<module>/review.md from review/result.json; exit 0 only when verdict and gate_verdict are PASS.",
    )
    add_review_output_args(p)
    p.add_argument(
        "--skip-schema-validation",
        action="store_true",
        help="Skip result.json schema validation (not recommended for Aidlc gates).",
    )
    p.set_defaults(func=command_report)
    return parser


def main() -> int:
    ns = build_parser().parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
