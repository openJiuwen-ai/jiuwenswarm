# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recognise shell commands whose output is file contents or a diff.

Coding agents often look at code through the shell instead of ``read_file``:
``sed -n '120,180p' app.py``, ``cat test_x.py``, ``git diff``, ``python -c
"print(inspect.getsource(f))"``. That output is what the next edit is built
from, so the context diet treats it like a file read: it is not trimmed (up to
a safety cap) and not folded by the shell compaction window.

The check is purely lexical and errs on the side of "not a source print": a
command counts only when one of its pipelines *starts* with a recognised
viewer and every later stage only selects a line range (``head``, ``tail``,
``sed -n 'N,Mp'``…). ``cat x.py | grep foo`` is filtered output, so it is still
trimmed like any other shell result.

Recognised viewers:

* ``cat`` / ``nl`` / ``tac`` / ``less`` / ``more`` / ``bat`` with a file operand;
* ``head`` / ``tail`` with a file operand;
* ``sed -n`` whose script only prints line ranges (``'1,60p'``, ``'10,20p;40,50p'``);
* ``awk`` whose program only selects ``NR`` ranges;
* ``git diff`` / ``git show`` / ``git log -p`` / ``git blame`` / ``git cat-file -p`` /
  ``git stash show -p`` (not with summary flags such as ``--stat`` or ``--name-only``);
* ``diff`` of two files;
* ``grep`` / ``rg`` / ``git grep`` with five or more lines of context;
* ``python`` code (``-c`` or a here-document) that calls ``inspect.getsource``.

A viewer whose only file operands were written earlier in the same command
(``pytest > /tmp/out.log; sed -n '1,40p' /tmp/out.log``) is a log, not source.
"""

from __future__ import annotations

import posixpath
import re
import shlex

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

_VIEWERS = frozenset({"cat", "nl", "tac", "less", "more", "bat", "batcat", "view"})
_HEAD_TAIL = frozenset({"head", "tail"})
_PASS_THROUGH = frozenset({"head", "tail", "cat", "nl", "less", "more", "tee"})
_PREFIX_WORDS = frozenset(
    {
        "do",
        "then",
        "else",
        "elif",
        "!",
        "{",
        "(",
        "time",
        "nohup",
        "sudo",
        "command",
        "builtin",
        "exec",
        "env",
    }
)
_SKIP_WORDS = frozenset(
    {
        "for",
        "while",
        "until",
        "if",
        "case",
        "select",
        "done",
        "fi",
        "esac",
        "}",
        ")",
        "function",
    }
)
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")
_SOURCE_PY_RE = re.compile(r"\bgetsource(lines)?\s*\(")
_SED_PRINT_RE = re.compile(
    r"^\s*(?:(?:\d+|\$|/[^/]*/)(?:\s*,\s*(?:\d+|\$|/[^/]*/|\+\d+))?\s*p\s*;?\s*)+$"
)
_AWK_RANGE_RE = re.compile(
    r"^\s*NR\s*(?:[<>]=?|==)\s*\d+(?:\s*&&\s*NR\s*(?:[<>]=?|==)\s*\d+)*\s*(?:\{\s*print(?:\s+\$0)?\s*;?\s*\})?\s*$"
)
_GIT_SUMMARY_FLAGS = (
    "--stat",
    "--shortstat",
    "--numstat",
    "--name-only",
    "--name-status",
    "--summary",
    "--dirstat",
    "--compact-summary",
    "--no-patch",
    "--quiet",
    "--exit-code",
    "--check",
    "--oneline",
)
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_CONTEXT_MIN = 5
_STDOUT_REDIRECT_RE = re.compile(r"^(?:1|&)?>>?\|?(?!&)(.+)$")


def is_source_print_command(command: str | None) -> bool:
    """True when ``command`` prints file contents or a diff (see module docstring)."""
    return source_print_reason(command) is not None


def source_print_reason(command: str | None) -> str | None:
    """The viewer that made ``command`` a source print (``"sed -n"``, ``"git diff"``…), or ``None``."""
    if not command or not isinstance(command, str):
        return None
    try:
        return _classify(command)
    except Exception:  # noqa: BLE001 - a parser bug must never break trimming
        return None


def _classify(command: str) -> str | None:
    written: set[str] = set()
    found: str | None = None
    cwd = ""
    for pipeline in _pipelines(_split_shell(command)):
        stages = [_parse_stage(seg["text"], seg["bodies"]) for seg in pipeline]
        if stages and _is_runner(stages[0]):
            # Test / build output dominates a mixed command and is exactly what
            # trimming is for (failures are kept as signal lines).
            return None
        if len(stages) == 1 and stages[0]["cmd"] == "cd":
            target = (stages[0]["args"] or ["~"])[0]
            cwd = (
                target if target.startswith(("/", "~")) else posixpath.join(cwd, target)
            )
            continue
        for stage in stages:
            stage["cwd"] = cwd
        reason = _pipeline_reason(stages, written)
        for stage in stages:
            written.update(stage["written"])
        found = found or reason
    return found


_RUNNERS = frozenset(
    {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "make",
        "cmake",
        "ctest",
        "ninja",
        "mvn",
        "gradle",
        "gradlew",
        "jest",
        "vitest",
        "mocha",
        "ansible-test",
        "nosetests",
        "trial",
        "phpunit",
        "rspec",
        "bazel",
    }
)
_RUNNER_SUBCOMMANDS = {
    "npm": {"test", "t", "run", "run-script", "ci", "install", "i"},
    "yarn": {"test", "run", "install", "build"},
    "pnpm": {"test", "run", "install", "build"},
    "npx": None,
    "go": {"test", "build", "vet", "run"},
    "cargo": {"test", "build", "check", "run", "clippy"},
    "pip": {"install"},
    "uv": {"run", "sync", "pip"},
}


def _is_runner(stage: dict) -> bool:
    cmd, args = stage["cmd"], stage["args"]
    if cmd in _RUNNERS or cmd.endswith("/gradlew"):
        return True
    if cmd in _RUNNER_SUBCOMMANDS:
        subs = _RUNNER_SUBCOMMANDS[cmd]
        return subs is None or (bool(args) and args[0] in subs)
    if _PYTHON_RE.match(cmd) and "-m" in args:
        idx = args.index("-m")
        module = args[idx + 1] if idx + 1 < len(args) else ""
        return module in (
            "pytest",
            "unittest",
            "tox",
            "nox",
            "pip",
            "build",
            "doctest",
            "nose",
            "nose2",
            "trial",
        )
    return False


# ─── shell splitting ──────────────────────────────────────────────────────


def _split_shell(command: str) -> list[dict]:
    """Split into simple commands with their separator and here-document bodies.

    Quotes are respected, ``#`` comments dropped, and here-document bodies are
    attached to the command that opened them instead of being parsed as shell.
    """
    segments: list[dict] = []
    buf: list[str] = []
    pending: list[tuple[str, int]] = []
    quote: str | None = None
    i, n = 0, len(command)

    def flush(sep: str) -> None:
        segments.append({"text": "".join(buf), "sep": sep, "bodies": []})
        buf.clear()

    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c + command[i + 1])
            i += 2
            continue
        if c == "#" and (not buf or buf[-1][-1:].isspace()):
            j = command.find("\n", i)
            i = n if j < 0 else j
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m:
                pending.append((m.group(2), len(segments)))
                buf.append(command[i : m.end()])
                i = m.end()
                continue
        if c == "\n":
            flush("\n")
            i += 1
            for tag, owner in pending:
                body: list[str] = []
                while i < n:
                    j = command.find("\n", i)
                    line = command[i:] if j < 0 else command[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip() == tag:
                        break
                    body.append(line)
                if owner < len(segments):
                    segments[owner]["bodies"].append("\n".join(body))
            pending = []
            continue
        sep = None
        for candidate in ("&&", "||", ";", "|", "&"):
            if command.startswith(candidate, i):
                sep = candidate
                break
        if sep == "&":
            prev = "".join(buf).rstrip()[-1:]
            nxt = command[i + 1 : i + 2]
            if prev in (">", "<") or nxt == ">":
                sep = None  # part of a redirect: 2>&1, &>file, >&2
        if sep == "|" and command.startswith("|&", i):
            sep = "|"
        if sep:
            flush(sep)
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    if buf or pending:
        flush("")
    return [seg for seg in segments if seg["text"].strip() or seg["bodies"]]


def _pipelines(segments: list[dict]) -> list[list[dict]]:
    pipelines: list[list[dict]] = []
    current: list[dict] = []
    for seg in segments:
        current.append(seg)
        if seg["sep"] != "|":
            pipelines.append(current)
            current = []
    if current:
        pipelines.append(current)
    return pipelines


# ─── one stage ────────────────────────────────────────────────────────────


def _parse_stage(text: str, bodies: list[str]) -> dict:
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        words = text.split()
    words = _strip_wrappers(words)
    stage = {
        "cmd": "",
        "args": [],
        "inputs": [],
        "written": set(),
        "stdout": True,
        "bodies": bodies,
    }
    if not words or words[0] in _SKIP_WORDS:
        stage["stdout"] = False
        return stage
    args: list[str] = []
    rest = words[1:]
    k = 0
    while k < len(rest):
        word = rest[k]
        if word.startswith("<<"):
            k += 1
            continue
        if word in (">", ">>", "1>", "1>>", ">|", "&>", "&>>"):
            target = rest[k + 1] if k + 1 < len(rest) else ""
            _redirect(stage, target)
            k += 2
            continue
        if word == "<":
            if k + 1 < len(rest):
                stage["inputs"].append(rest[k + 1])
            k += 2
            continue
        if word.startswith("2>") or word in (">&2", "1>&2"):
            k += 1
            continue
        m = _STDOUT_REDIRECT_RE.match(word)
        if m and word.startswith((">", "1>", "&>")):
            _redirect(stage, m.group(1))
            k += 1
            continue
        args.append(word)
        k += 1
    stage["cmd"] = posixpath.basename(words[0])
    stage["args"] = args
    return stage


_WRAPPERS_WITH_ARG = {
    "timeout": 1,
    "nice": 0,
    "ionice": 0,
    "stdbuf": 0,
    "xvfb-run": 0,
    "taskset": 1,
    "chrt": 1,
}


def _strip_wrappers(words: list[str]) -> list[str]:
    """Drop prefixes that do not change what is printed: env assignments, ``timeout 900``, ``nice``…"""
    changed = True
    while words and changed:
        changed = False
        if words[0] in _PREFIX_WORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
            changed = True
            continue
        name = posixpath.basename(words[0])
        if name in _WRAPPERS_WITH_ARG:
            rest = words[1:]
            while rest and rest[0].startswith("-"):
                option = rest.pop(0)
                if (
                    option
                    in (
                        "-s",
                        "-n",
                        "-k",
                        "--signal",
                        "--kill-after",
                        "-c",
                        "-p",
                        "-o",
                        "-e",
                        "-i",
                    )
                    and rest
                ):
                    rest.pop(0)
            for _ in range(_WRAPPERS_WITH_ARG[name]):
                if rest:
                    rest.pop(0)
            words = rest
            changed = True
    return words


def _redirect(stage: dict, target: str) -> None:
    stage["stdout"] = False
    if target and target != "/dev/null":
        stage["written"].add(target)


# ─── classification ───────────────────────────────────────────────────────


def _pipeline_reason(stages: list[dict], written: set[str]) -> str | None:
    if not stages:
        return None
    first = stages[0]
    reason = _producer_reason(first, written)
    if not reason:
        return None
    for stage in stages[1:]:
        if not _is_range_filter(stage):
            return None
    if not stages[-1]["stdout"]:
        return None
    return reason


def _is_range_filter(stage: dict) -> bool:
    """A later pipeline stage that only passes lines through or selects a range."""
    cmd, args = stage["cmd"], stage["args"]
    if cmd in _PASS_THROUGH:
        if cmd in _HEAD_TAIL:
            return not _file_operands(
                args, skip_values=frozenset({"-n", "-c", "--lines", "--bytes"})
            )
        return cmd == "tee" or not _file_operands(args)
    if cmd == "sed":
        quiet = any(a in ("-n", "--quiet", "--silent") for a in args)
        scripts = [a for a in args if not a.startswith("-")]
        return quiet and len(scripts) == 1 and bool(_SED_PRINT_RE.match(scripts[0]))
    return False


def _file_operands(
    args: list[str], *, skip_values: frozenset[str] = frozenset()
) -> list[str]:
    operands: list[str] = []
    k = 0
    while k < len(args):
        arg = args[k]
        if arg in skip_values:
            k += 2
            continue
        if arg == "--":
            operands.extend(args[k + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            k += 1
            continue
        operands.append(arg)
        k += 1
    return [op for op in operands if op != "-"]


_NOT_SOURCE_PATH_RE = re.compile(
    r"(^|/)(tmp|var/tmp|dev|proc)/|context_diet/|/agent/workspace/|/\.jiuwenswarm|"
    r"\.(log|out|err|lock)$|(^|/)nohup\.out$",
)
"""Logs, temp files, the diet's own spill files and agent scratch space are not source."""


def _real_files(operands: list[str], written: set[str], cwd: str = "") -> list[str]:
    real = []
    for op in operands:
        full = op if op.startswith(("/", "~")) or not cwd else posixpath.join(cwd, op)
        if op in written or full in written or _NOT_SOURCE_PATH_RE.search(full):
            continue
        real.append(op)
    return real


def _producer_reason(stage: dict, written: set[str]) -> str | None:
    cmd, args = stage["cmd"], stage["args"]
    if not cmd or not stage["stdout"]:
        return None
    if cmd in _VIEWERS:
        files = _real_files(
            _file_operands(args) + stage["inputs"], written, stage.get("cwd", "")
        )
        return cmd if files else None
    if cmd in _HEAD_TAIL:
        files = _real_files(
            _file_operands(
                args, skip_values=frozenset({"-n", "-c", "--lines", "--bytes"})
            )
            + stage["inputs"],
            written,
            stage.get("cwd", ""),
        )
        return cmd if files else None
    if cmd == "sed":
        return (
            "sed -n"
            if _sed_prints_file(args, stage["inputs"], written, stage.get("cwd", ""))
            else None
        )
    if cmd in ("awk", "gawk", "mawk"):
        return (
            "awk"
            if _awk_prints_range(args, stage["inputs"], written, stage.get("cwd", ""))
            else None
        )
    if cmd == "git":
        return _git_reason(args, written)
    if cmd in ("diff", "colordiff"):
        if any(
            a == "--brief"
            or (a.startswith("-") and not a.startswith("--") and "q" in a)
            for a in args
        ):
            return None
        return (
            "diff"
            if len(_real_files(_file_operands(args), written, stage.get("cwd", "")))
            >= 2
            else None
        )
    if cmd in ("grep", "egrep", "fgrep", "rg", "ag", "ack"):
        return f"{cmd} with context" if _context_lines(args) >= _CONTEXT_MIN else None
    if _PYTHON_RE.match(cmd):
        code = ""
        if "-c" in args:
            idx = args.index("-c")
            code = args[idx + 1] if idx + 1 < len(args) else ""
        code += "\n".join(stage["bodies"])
        return "inspect.getsource" if _SOURCE_PY_RE.search(code) else None
    return None


def _sed_prints_file(
    args: list[str], inputs: list[str], written: set[str], cwd: str = ""
) -> bool:
    quiet = False
    scripts: list[str] = []
    operands: list[str] = []
    k = 0
    while k < len(args):
        arg = args[k]
        if arg in ("-n", "--quiet", "--silent"):
            quiet = True
        elif arg in ("-e", "--expression"):
            if k + 1 < len(args):
                scripts.append(args[k + 1])
            k += 2
            continue
        elif arg.startswith("-") and len(arg) > 1:
            if "n" in arg[1:] and not arg.startswith("--"):
                quiet = True
            if "i" in arg[1:] and not arg.startswith("--"):
                return False  # in-place edit
        else:
            operands.append(arg)
        k += 1
    if not quiet:
        return False
    if not scripts:
        if not operands:
            return False
        scripts.append(operands.pop(0))
    if not all(_SED_PRINT_RE.match(script) for script in scripts):
        return False
    return bool(_real_files(operands + inputs, written, cwd))


def _awk_prints_range(
    args: list[str], inputs: list[str], written: set[str], cwd: str = ""
) -> bool:
    operands = [a for a in args if not a.startswith("-")]
    if not operands:
        return False
    program, files = operands[0], operands[1:]
    return bool(_AWK_RANGE_RE.match(program)) and bool(
        _real_files(files + inputs, written, cwd)
    )


def _git_reason(args: list[str], written: set[str]) -> str | None:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        if args[k] in _GIT_VALUE_OPTS:
            k += 2
        else:
            k += 1
    if k >= len(args):
        return None
    sub, rest = args[k], args[k + 1 :]
    patch = any(a in ("-p", "-u", "--patch") or a.startswith("-L") for a in rest)
    summary = (
        any(
            a == flag or a.startswith(flag + "=")
            for a in rest
            for flag in _GIT_SUMMARY_FLAGS
        )
        or "-s" in rest
    )
    if sub in ("diff", "show"):
        if summary and not patch:
            return None
        return f"git {sub}"
    if sub == "log":
        return "git log -p" if patch else None
    if sub == "blame" or sub == "annotate":
        return "git blame"
    if sub == "cat-file":
        return "git cat-file" if "-p" in rest else None
    if sub == "stash":
        return "git stash show -p" if rest[:1] == ["show"] and patch else None
    if sub == "grep":
        return "git grep with context" if _context_lines(rest) >= _CONTEXT_MIN else None
    return None


def _context_lines(args: list[str]) -> int:
    best = 0
    k = 0
    while k < len(args):
        arg = args[k]
        value: str | None = None
        long = re.match(r"^--(?:after-|before-)?context(?:=(\d+))?$", arg)
        if long:
            value = long.group(1)
            if value is None and k + 1 < len(args):
                value = args[k + 1]
        else:
            short = re.match(r"^-[A-Za-z]*[ABC](\d*)$", arg)
            if short:
                value = short.group(1) or (args[k + 1] if k + 1 < len(args) else None)
        if value and value.isdigit():
            best = max(best, int(value))
        k += 1
    return best


__all__ = ["is_source_print_command", "source_print_reason"]
