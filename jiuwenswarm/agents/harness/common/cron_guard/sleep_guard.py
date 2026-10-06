# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L1: static sleep / poll interception for cron runs (issue #5018, design §3.4).

This is a best-effort lint on shell command strings, **not** a security
boundary.  It reduces pointless ``sleep``-polling waits inside cron
``scheduled_run`` requests; anything it misses is bounded by the L-WD hard
deadline.

Parsing contract (design v2 §3.4.1):

* Token-level splitting with ``shlex`` punctuation mode — quoted strings are
  literals and never become commands or loop keywords
  (``echo "while true; sleep 120"`` is inert).
* ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines segment commands.
* Nested unwrapping up to ``max_nesting_depth`` (default 3):
  ``bash|sh|zsh|dash -c '<script>'``, ``eval '<script>'``, ``$(...)`` and
  backticks recurse; ``env``/``nohup``/``setsid``/``timeout``/``time``/
  ``nice``/``stdbuf``/``command``/``exec`` prefixes are skipped.
* One ``sleep`` call's duration is the **sum of all its arguments**
  (``sleep 1m 30s`` == 90s).  Suffixes ``s/m/h/d`` and decimals supported.
* PowerShell ``Start-Sleep -Seconds N / -Milliseconds N``.
* Durations containing variables or command substitution → ``unknown_duration``.
* Loop keywords (``while``/``until``/``for``) in command position with a
  sleep inside the loop body → ``loop_poll``.
* Interpreter-executes-file forms (``python worker.py``) are NOT inspected
  (``script_scan: off``); inline ``python -c`` numeric literals are matched
  best-effort.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from jiuwenswarm.common.utils import logger

_SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash", "ksh"}
_C_FLAGS = {"-c", "-lc", "-ac"}
_SKIP_PREFIXES = {
    "env", "nohup", "setsid", "time", "nice", "stdbuf", "command", "exec",
}
_LOOP_KEYWORDS = {"while", "until", "for"}
_BLOCK_OPENERS = {"do", "then", "else"}
_SLEEP_COMMANDS = {"sleep"}
_POWERSHELL_SLEEP = {"start-sleep"}

_DURATION_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
# A single duration token: one or more <number><unit> groups, unit optional (seconds).
_DURATION_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)([smhd])?)+$")
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)([smhd])?")
_HAS_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{|\$[A-Za-z_@*#?]")
_SLEEPISH_RE = re.compile(r"(?:^|[;&|(\s])(?:sleep|start-sleep)(?:\s|$)", re.IGNORECASE)
# Loose hint used only on unparseable paths (depth exceeded / tokenizer failure):
# any occurrence of "sleep" in a command we cannot fully analyze is treated as
# a potential sleep so it can never slip through silently.
_SLEEP_HINT_RE = re.compile(r"sleep", re.IGNORECASE)
# Best-effort inline interpreter sleep literals: time.sleep(30) / sleep(30)
_INLINE_SLEEP_RE = re.compile(r"(?:time\.)?sleep\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)")
_INLINE_SLEEP_UNKNOWN_RE = re.compile(r"(?:time\.)?sleep\(\s*[^)0-9.\s]")

_INTERPRETERS = {"python", "python3", "python2", "node", "nodejs", "perl", "ruby"}


class _PunctLexer(shlex.shlex):
    """shlex with shell punctuation preserved as tokens and merged operators.

    ``punctuation_chars="();|&<>"`` keeps operators out of words while quoted
    strings stay single literals; ``read_token`` is overridden to merge
    adjacent identical operator characters into multi-char operators
    (``&&``, ``||``) the way a real shell does.
    """

    def read_token(self):  # type: ignore[override]
        tok = super().read_token()
        if tok is None:
            return None
        if tok in ("&", "|", "<", ">"):
            nxt = self.read_token()
            if nxt is not None and nxt == tok:
                return tok + nxt
            if nxt is not None:
                self.push_token(nxt)
        return tok


@dataclass
class SleepParseResult:
    """Outcome of statically parsing one shell command string."""

    sleep_calls: int = 0
    sleep_seconds: float = 0.0          # sum of all identified sleep durations
    max_single_call_seconds: float = 0.0  # largest single sleep-call total
    unknown_duration: bool = False      # a duration depends on a variable/substitution
    loop_poll: bool = False             # sleep inside a while/until/for scope (or watch)
    unparseable: bool = False           # tokenizer failed or nesting too deep (sleep-ish)
    background: bool = False            # command contains a `&` job control segment
    details: list[str] = field(default_factory=list)

    @property
    def has_sleep(self) -> bool:
        # ``unparseable`` counts as a hit: a command we could not fully analyze
        # but that carries sleep markers must reach the guard's decision logic
        # (which blocks it under enforce mode) instead of slipping through.
        return self.sleep_calls > 0 or self.unknown_duration or self.loop_poll or self.unparseable


def _parse_duration_token(token: str) -> float | None:
    """Parse ``90``, ``1m30s``, ``0.5`` → seconds.  None when not a literal."""
    if not token or _HAS_SUBSTITUTION_RE.search(token):
        return None
    if not _DURATION_RE.match(token):
        return None
    total = 0.0
    for num, unit in _DURATION_PART_RE.findall(token):
        total += float(num) * _DURATION_UNIT_SECONDS.get(unit or "s", 1.0)
    return total


_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on command separators (``;``, ``&&``, ``||``, ``|``, ``&``, newline)."""
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if tok == "&":
                current.append("&")  # mark background on this segment
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _extract_substitutions(text: str) -> list[str]:
    """Pull inner scripts out of ``$( ... )`` and backtick constructs.

    Each substitution is returned exactly once: the outer ``$(...)`` body is
    extracted here and its nested parts are picked up when that body is
    re-parsed; backticks inside a ``$(...)`` body are skipped for the same
    reason (they are extracted from the inner text instead) — double-counting
    a nested ``sleep`` would inflate the run's budget.
    """
    inner: list[str] = []
    substitution_spans: list[tuple[int, int]] = []
    idx = 0
    while True:
        start = text.find("$(", idx)
        if start == -1:
            break
        depth = 1
        i = start + 2
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if depth == 0:
            inner.append(text[start + 2:i - 1])
            substitution_spans.append((start, i))
            idx = i  # continue after the close paren; nested $() come via recursion
        else:
            idx = start + 2  # unbalanced opener — skip past it
    for m in re.finditer(r"`([^`]*)`", text):
        if any(a <= m.start() < b for a, b in substitution_spans):
            continue
        if m.group(1).strip():
            inner.append(m.group(1))
    return inner


def _parse_sleep_args(args: list[str]) -> tuple[float | None, bool]:
    """Sum sleep duration args.  Returns (seconds_or_None, unknown)."""
    total = 0.0
    unknown = False
    for arg in args:
        if arg.startswith("-"):
            continue
        value = _parse_duration_token(arg)
        if value is None:
            unknown = True
        else:
            total += value
    if unknown:
        return None, True
    return total, False


def _tokenize(text: str) -> list[str]:
    lexer = _PunctLexer(text, posix=True, punctuation_chars="();|&<>")
    lexer.whitespace = " \t\n"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _parse_script(
    text: str,
    depth: int,
    max_depth: int,
    result: SleepParseResult,
) -> None:
    if depth > max_depth:
        # Only a sleep-hinted remainder is worth flagging: blocking a deep-nested
        # command with no sleep markers would be a pure false positive.
        if _SLEEP_HINT_RE.search(text):
            result.unparseable = True
            result.details.append(f"nesting depth exceeded (>{max_depth}) with sleep markers")
        return
    try:
        tokens = _tokenize(text)
    except ValueError:
        # Tokenizer failure: only meaningful as a hit when the raw text hints
        # at sleep; otherwise treat as inert (avoid false positives on exotic
        # quoting that bash accepts but shlex rejects).
        if _SLEEP_HINT_RE.search(text):
            result.unparseable = True
            result.details.append("tokenizer failure on sleep-hinted command")
        return

    # Command substitutions can span multiple tokens after punctuation
    # lexing — extract them from the full text before segmenting.
    for inner in _extract_substitutions(text):
        _parse_script(inner, depth + 1, max_depth, result)

    # Loop-scope tracking across segments: a `while|until|for` header opens a
    # scope that stays open until the matching `done`.
    loop_depth = 0
    for segment in _split_segments(tokens):
        head = segment[0] if segment else None
        if head in _LOOP_KEYWORDS:
            loop_depth += 1
            # Parse the remainder of the header segment (the condition) with
            # the loop already open — `while sleep 5; ...` is a poll too.
            _parse_segment(segment, depth, max_depth, result, in_loop=True)
            continue
        if head == "done":
            loop_depth = max(0, loop_depth - 1)
            continue
        _parse_segment(segment, depth, max_depth, result, in_loop=loop_depth > 0)


def _parse_segment(
    segment: list[str],
    depth: int,
    max_depth: int,
    result: SleepParseResult,
    in_loop: bool = False,
) -> None:
    if not segment:
        return
    if "&" in segment:
        result.background = True
    segment = [t for t in segment if t != "&"]
    if not segment:
        return

    # Skip block openers (`do`, `then`, `else`) to reach command position.
    while segment and segment[0] in _BLOCK_OPENERS:
        segment = segment[1:]
    if not segment:
        return
    command = segment[0]
    args = segment[1:]
    base = command.rsplit("/", 1)[-1].lower()

    # Wrapper prefixes.
    if base == "timeout":
        # timeout [OPTIONS] DURATION CMD... — drop options + duration, parse the rest.
        rest = list(args)
        while rest:
            head = rest[0]
            if head.startswith("-"):
                rest = rest[1:]
                continue
            if _parse_duration_token(head) is not None:
                rest = rest[1:]
                break
            break
        if rest:
            _parse_script(" ".join(_requote(rest)), depth + 1, max_depth, result)
        return
    if base in _SKIP_PREFIXES:
        if base == "env":
            rest = [a for a in args if ("=" not in a or a.startswith("-"))]
        else:
            cleaned: list[str] = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a == "-n" or (base == "stdbuf" and a in ("-o", "-e", "-i")):
                    skip_next = True
                    continue
                if a.startswith("-"):
                    continue
                cleaned.append(a)
            rest = cleaned
        if rest:
            _parse_script(" ".join(_requote(rest)), depth + 1, max_depth, result)
        return

    if base == "eval":
        for arg in args:
            if not arg.startswith("-"):
                _parse_script(arg, depth + 1, max_depth, result)
        return

    if base in _SHELL_WRAPPERS:
        for pos, arg in enumerate(args):
            if arg in _C_FLAGS and pos + 1 < len(args):
                _parse_script(args[pos + 1], depth + 1, max_depth, result)
                return
        # `bash script.sh` — interpreter-executes-file: not inspected.
        return

    if base in _SLEEP_COMMANDS:
        seconds, unknown = _parse_sleep_args(args)
        result.sleep_calls += 1
        if unknown:
            result.unknown_duration = True
            result.details.append("sleep with variable/substitution duration")
        else:
            result.sleep_seconds += seconds or 0.0
            result.max_single_call_seconds = max(result.max_single_call_seconds, seconds or 0.0)
            if in_loop:
                result.loop_poll = True
        return

    if base in _POWERSHELL_SLEEP:
        seconds = _parse_powershell_sleep(args)
        result.sleep_calls += 1
        if seconds is None:
            result.unknown_duration = True
            result.details.append("Start-Sleep with unknown duration")
        else:
            result.sleep_seconds += seconds
            result.max_single_call_seconds = max(result.max_single_call_seconds, seconds)
            if in_loop:
                result.loop_poll = True
        return

    if base == "watch":
        result.loop_poll = True
        result.details.append("watch used as polling wrapper")
        return

    if base == "read":
        for pos, arg in enumerate(args):
            if arg == "-t" and pos + 1 < len(args):
                seconds = _parse_duration_token(args[pos + 1])
                if seconds is None:
                    result.unknown_duration = True
                else:
                    result.sleep_calls += 1
                    result.sleep_seconds += seconds
                    result.max_single_call_seconds = max(result.max_single_call_seconds, seconds)
                return
        return

    # Inline interpreter code: python -c '... time.sleep(30) ...'
    if base in _INTERPRETERS:
        codes: list[str] = []
        for pos, arg in enumerate(args):
            if arg in ("-c", "-m"):
                if pos + 1 < len(args):
                    codes.append(args[pos + 1])
            elif re.fullmatch(r"-c.+", arg):
                codes.append(arg[2:])
        for code in codes:
            for m in _INLINE_SLEEP_RE.finditer(code):
                seconds = float(m.group(1))
                result.sleep_calls += 1
                result.sleep_seconds += seconds
                result.max_single_call_seconds = max(result.max_single_call_seconds, seconds)
            if _INLINE_SLEEP_UNKNOWN_RE.search(code):
                result.unknown_duration = True
        return

    # Plain command: substitutions already recursed above.
    return


def _requote(tokens: list[str]) -> list[str]:
    return [shlex.quote(t) for t in tokens]


def _parse_powershell_sleep(args: list[str]) -> float | None:
    seconds = 0.0
    found = False
    unknown = False
    i = 0
    while i < len(args):
        arg = args[i]
        low = arg.lower()
        if low in ("-seconds", "-s") and i + 1 < len(args):
            value = _parse_duration_token(args[i + 1])
            if value is None:
                unknown = True
            else:
                seconds += value
                found = True
            i += 2
            continue
        if low in ("-milliseconds", "-m") and i + 1 < len(args):
            try:
                seconds += float(args[i + 1]) / 1000.0
                found = True
            except ValueError:
                unknown = True
            i += 2
            continue
        i += 1
    if unknown:
        return None
    return seconds if found else None


def parse_shell_command(command: str, max_nesting_depth: int = 3) -> SleepParseResult:
    """Statically parse *command* for sleep/poll constructs.  Never raises."""
    result = SleepParseResult()
    try:
        _parse_script(command or "", 1, max(1, int(max_nesting_depth)), result)
    except Exception as exc:  # noqa: BLE001 — parser failure on sleep-ish text = unparseable
        if _SLEEPISH_RE.search(command or ""):
            result.unparseable = True
            result.details.append(f"parser error: {exc}")
    return result


# ---------------------------------------------------------------------------
# Guard entry (identity-aware, budget-aware, fail-open)
# ---------------------------------------------------------------------------

_BLOCK_MESSAGE = (
    "[cron_guard] 该命令被 cron 运行守卫拦截：{reason}。"
    "定时任务中禁止 sleep 轮询等待。请基于已有信息直接给出结论；"
    "若依赖尚未就绪，请直接汇报当前状态，不要等待。"
)


def guard_shell_command(command: str, background: bool = False, session_id: str | None = None) -> str | None:
    """Return an error string when *command* must not run in a cron run.

    Returns ``None`` (allow) for interactive requests, when the guard is
    disabled/warn-mode, or on any internal failure (fail-open).
    """
    try:
        from .config import get_cron_guard_config
        from .identity import get_current_or_registered_run
        from .ledger import sync_budget_to_ledger

        cfg = get_cron_guard_config()
        if not cfg.get("enabled"):
            return None
        ctx = get_current_or_registered_run(session_id)
        if ctx is None:
            return None  # interactive: zero impact

        sleep_cfg = cfg.get("sleep") or {}
        mode = str(sleep_cfg.get("mode", "enforce"))
        if mode == "off":
            return None

        parsed = parse_shell_command(command, int(sleep_cfg.get("max_nesting_depth", 3)))
        if not parsed.has_sleep:
            return None

        blocked_reason: str | None = None
        max_single = float(sleep_cfg.get("max_single_seconds", 10))
        max_total = float(sleep_cfg.get("max_total_seconds", 30))
        max_calls = int(sleep_cfg.get("max_sleep_calls", 3))
        unknown_policy = str(sleep_cfg.get("unknown_duration", "block"))

        if parsed.unparseable:
            blocked_reason = "无法静态解析的 sleep 相关命令（unknown shape）"
        elif parsed.unknown_duration and unknown_policy == "block":
            blocked_reason = "sleep 时长依赖变量/命令替换，无法静态确认（unknown_duration=block）"
        elif parsed.loop_poll:
            blocked_reason = "循环轮询（loop_poll）"
        elif parsed.max_single_call_seconds > max_single:
            blocked_reason = f"单条 sleep 累计 {parsed.max_single_call_seconds:g}s 超过上限 {max_single:g}s"
        else:
            # Counted against the run budget (per sleep call, not per command:
            # one command with two sleeps consumes two calls).
            counted_seconds = parsed.sleep_seconds
            counted_calls = parsed.sleep_calls
            if parsed.unknown_duration and unknown_policy == "assume":
                counted_seconds += float(sleep_cfg.get("unknown_assumed_seconds", 10))
            ctx.budget.add_sleep(counted_seconds, calls=counted_calls)
            if ctx.budget.sleep_calls > max_calls:
                blocked_reason = f"本 run 的 sleep 调用次数 {ctx.budget.sleep_calls} 超过上限 {max_calls}"
            elif ctx.budget.sleep_seconds > max_total:
                blocked_reason = (
                    f"本 run 的 sleep 累计 {ctx.budget.sleep_seconds:g}s 超过总预算 {max_total:g}s"
                )
            sync_budget_to_ledger(ctx)

        # parsed.has_sleep is guaranteed here (early return above); a compliant
        # sleep in a background job is still a polling escape hatch.
        if background and blocked_reason is None:
            blocked_reason = "后台命令包含 sleep/轮询"

        if blocked_reason is None:
            return None
        if mode == "warn":
            logger.warning("[cron_guard] (warn) would block: %s | cmd=%r", blocked_reason, command[:200])
            return None
        logger.info("[cron_guard] blocked: %s | run_id=%s cmd=%r", blocked_reason, ctx.run_id, command[:200])
        return _BLOCK_MESSAGE.format(reason=blocked_reason)
    except Exception as exc:  # noqa: BLE001 — guard failure must never block the run
        logger.warning("[cron_guard] sleep guard internal error (fail-open): %s", exc)
        return None
