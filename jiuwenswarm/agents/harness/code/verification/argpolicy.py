# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Allow-list for ``run_tests`` ``extra_args``.

``extra_args`` is the only option channel the model has. A deny-list of
dangerous options proved leaky (clustered short flags, ``@argfile`` expansion,
camelCase/kebab-case aliases, options forwarded through ``npm run``), so each
runner has an explicit list of options that only select, filter or format the
run. Everything else is refused, including:

* bare positional arguments (they would be read as extra test paths or
  packages and bypass the workspace containment of ``tests`` / ``files``);
* ``@``-prefixed arguments (argument-file expansion);
* clustered short flags unless every letter is an allowed no-value flag;
* a separate option value that itself starts with ``-``.

Options that load code or configuration, change the root directory, write
files (junit/xml/log/coverage reports) or send output elsewhere
(``--pastebin``) are simply not on any list.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_INT = re.compile(r"^\d{1,6}$")
_DURATION = re.compile(r"^\d{1,6}(ms|s|m|h)?$")


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One allowed option. ``value`` is None for flags, else a validator name."""

    value: str | None = None  # None | "int" | "text" | "duration" | "choice" | "bool" | "int_or_auto"
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerPolicy:
    options: dict[str, OptionSpec]
    #: pytest/unittest use argparse: "-kEXPR" means "-k EXPR".
    attached_short_values: bool = False
    #: go accepts "-run" and "--run" alike.
    double_dash_alias: bool = False
    #: cargo: bare names are test-name filters, not paths.
    positional_filters: bool = False
    #: cargo: options after "--" go to the libtest harness.
    after_double_dash: dict[str, OptionSpec] = field(default_factory=dict)


_F = OptionSpec()
_I = OptionSpec("int")
_T = OptionSpec("text")

_PYTEST = RunnerPolicy(
    options={
        "-q": _F, "--quiet": _F, "-v": _F, "--verbose": _F, "-x": _F, "--exitfirst": _F,
        "-s": _F, "-l": _F, "--showlocals": _F, "--lf": _F, "--last-failed": _F,
        "--ff": _F, "--failed-first": _F, "--nf": _F, "--new-first": _F,
        "--sw": _F, "--stepwise": _F, "--no-header": _F, "--no-summary": _F,
        "--strict-markers": _F, "--runxfail": _F, "--disable-warnings": _F,
        "--maxfail": _I, "-k": _T, "-m": _T, "--deselect": _T, "--durations": _I,
        "-r": _T, "--timeout": _I,
        "--tb": OptionSpec("choice", ("auto", "long", "short", "line", "native", "no")),
        "--capture": OptionSpec("choice", ("fd", "sys", "no", "tee-sys")),
        "--color": OptionSpec("choice", ("yes", "no", "auto")),
        "-n": OptionSpec("int_or_auto"),
    },
    attached_short_values=True,
)

_UNITTEST = RunnerPolicy(
    options={
        "-v": _F, "--verbose": _F, "-q": _F, "--quiet": _F, "-f": _F, "--failfast": _F,
        "-b": _F, "--buffer": _F, "-c": _F, "--catch": _F, "--locals": _F,
        "-k": _T, "--durations": _I,
    },
    attached_short_values=True,
)

_JEST = RunnerPolicy(
    options={
        "--ci": _F, "--silent": _F, "--verbose": _F, "--runInBand": _F, "--run-in-band": _F,
        "-i": _F, "--no-coverage": _F, "--detectOpenHandles": _F, "--detect-open-handles": _F,
        "--forceExit": _F, "--force-exit": _F, "--bail": _F, "--no-cache": _F,
        "-t": _T, "--testNamePattern": _T, "--test-name-pattern": _T,
        "--maxWorkers": _I, "--max-workers": _I, "-w": _I,
        "--testTimeout": _I, "--test-timeout": _I,
        "--coverage": OptionSpec("choice", ("false",)),
    },
)

_VITEST = RunnerPolicy(
    options={
        "--silent": _F, "--no-file-parallelism": _F, "--no-coverage": _F,
        "-t": _T, "--testNamePattern": _T, "--test-name-pattern": _T,
        "--bail": _I, "--retry": _I, "--maxWorkers": _I, "--max-workers": _I,
        "--testTimeout": _I, "--test-timeout": _I,
    },
)

_GO_BOOL = OptionSpec("bool")
_GO = RunnerPolicy(
    options={
        "-v": _GO_BOOL, "-short": _GO_BOOL, "-failfast": _GO_BOOL, "-race": _GO_BOOL,
        "-cover": _GO_BOOL,
        "-run": _T, "-skip": _T, "-count": _I, "-parallel": _I, "-cpu": _T,
        "-timeout": OptionSpec("duration"),
    },
    double_dash_alias=True,
)

_CARGO = RunnerPolicy(
    options={
        "-q": _F, "--quiet": _F, "--release": _F, "--no-fail-fast": _F, "--lib": _F,
        "--bins": _F, "--tests": _F, "--doc": _F, "--workspace": _F, "--all-features": _F,
        "--no-default-features": _F, "--locked": _F, "--offline": _F, "--frozen": _F,
        "-j": _I, "--jobs": _I, "-p": _T, "--package": _T, "--features": _T, "-F": _T,
        "--test": _T, "--bin": _T,
    },
    positional_filters=True,
    after_double_dash={
        "--exact": _F, "--nocapture": _F, "--ignored": _F, "--include-ignored": _F,
        "-q": _F, "--quiet": _F, "--show-output": _F, "--test-threads": _I, "--skip": _T,
    },
)

POLICIES: dict[str, RunnerPolicy] = {
    "pytest": _PYTEST,
    "unittest": _UNITTEST,
    "jest": _JEST,
    "vitest": _VITEST,
    "go": _GO,
    "cargo": _CARGO,
}


def allowed_options(runner: str) -> list[str]:
    policy = POLICIES.get(runner)
    if policy is None:
        return []
    names = sorted(policy.options)
    if policy.after_double_dash:
        names += ["--"] + sorted(policy.after_double_dash)
    return names


def _value_ok(spec: OptionSpec, value: str) -> bool:
    kind = spec.value
    if kind == "int":
        return bool(_INT.match(value))
    if kind == "int_or_auto":
        return bool(_INT.match(value)) or value in ("auto", "logical")
    if kind == "duration":
        return bool(_DURATION.match(value))
    if kind == "choice":
        return value in spec.choices
    if kind == "bool":
        return value in ("true", "false")
    if kind == "text":
        return "\x00" not in value and not value.startswith("@")
    return False


def check_extra_args(runner: str, extra_args: list[str]) -> str | None:
    """Return None when every entry is allowed for ``runner``, else a reason."""

    args = [str(a) for a in extra_args or []]
    if not args:
        return None
    policy = POLICIES.get(runner)
    if policy is None:
        return f"extra_args are not supported for {runner}"
    options = policy.options
    in_harness = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("@"):
            return f"{arg!r}: argument files are not allowed"
        if "\x00" in arg:
            return "NUL bytes are not allowed in extra_args"
        if arg == "--":
            if policy.after_double_dash and not in_harness:
                in_harness = True
                options = policy.after_double_dash
                i += 1
                continue
            return "'--' is not allowed in extra_args (targets go in `tests`)"
        if not arg.startswith("-") or arg == "-":
            if policy.positional_filters and re.match(r"^[A-Za-z0-9_:]+$", arg):
                i += 1
                continue
            return (f"{arg!r}: bare arguments are not allowed in extra_args; "
                    "put test files or ids in `tests`")
        name, eq, attached = arg.partition("=")
        if policy.double_dash_alias and name.startswith("--"):
            name = name[1:]
        spec = options.get(name)
        if spec is None and not eq and policy.attached_short_values \
                and not arg.startswith("--") and len(arg) > 2:
            # argparse: "-kEXPR" == "-k EXPR"; "-vx" == "-v -x".
            head = options.get(arg[:2])
            if head is not None and head.value is not None:
                if not _value_ok(head, arg[2:]):
                    return f"{arg!r}: invalid value for {arg[:2]}"
                i += 1
                continue
            letters = ["-" + ch for ch in arg[1:]]
            if all(options.get(flag) is not None and options[flag].value is None for flag in letters):
                i += 1
                continue
            return f"{arg!r}: combined short options are only allowed for no-value flags"
        if spec is None:
            return f"{arg!r} is not an allowed {runner} option"
        if spec.value is None:
            if eq:
                return f"{arg!r}: {name} takes no value"
            i += 1
            continue
        if spec.value == "bool" and not eq:
            i += 1  # bare boolean flag ("-v", "--coverage")
            continue
        if eq:
            value = attached
        else:
            if i + 1 >= len(args):
                return f"{arg!r} needs a value"
            value = args[i + 1]
            if value.startswith("-"):
                return f"{arg!r}: a value starting with '-' must be attached with '='"
            i += 1
        if not _value_ok(spec, value):
            return f"{arg!r}: invalid value {value!r}"
        i += 1
    return None


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def npm_script_runner(root: str | Path, script: str) -> str | None:
    """``jest`` / ``vitest`` when package.json's ``script`` directly runs one, else None."""

    try:
        data = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
        command = str((data.get("scripts") or {}).get(script or "test") or "")
        tokens = shlex.split(command)
    except (OSError, ValueError, AttributeError):
        return None
    if any(t in ("&&", "||", ";", "|") or t.endswith(";") for t in tokens):
        return None
    while tokens and (_ENV_ASSIGN.match(tokens[0]) or tokens[0] in ("npx", "cross-env")):
        tokens.pop(0)
    if not tokens:
        return None
    head = Path(tokens[0]).name
    if head == "jest":
        return "jest"
    if head == "vitest" and (len(tokens) == 1 or tokens[1] == "run"):
        return "vitest"
    return None


__all__ = ["POLICIES", "allowed_options", "check_extra_args", "npm_script_runner"]
