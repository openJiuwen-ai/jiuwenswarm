# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``jiuwenswarm-doctor`` CLI: diagnose the WorkSwarm runtime environment.

Unlike ``startup_diagnostics`` (which stays stdlib-only and serves the frozen
desktop/installer path), this is a normal console script that may import
business modules. It reuses ``run_doctor()`` for the data-directory and
native-extension checks, then layers on richer checks (Python/config/env
files, external tools, model-API reachability, service ports, MCP servers) and
renders a flutter/openclaw-style checklist.

Diagnose-only: it never mutates state. The only write is an explicit
``--output`` file. Secrets are never printed (API keys surface as presence
booleans, URLs are printed without query strings, unauthenticated probes send
no credentials).
"""

# pylint: disable=broad-exception-caught,import-outside-toplevel

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from jiuwenswarm.common import startup_diagnostics as sd
from jiuwenswarm.common._build_config import DISPLAY_NAME, PACKAGE_NAME, VERSION
from jiuwenswarm.common.model_config_validation import (
    is_placeholder_model_entry,
    model_client_config_view,
)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

_PYTHON_FLOOR = (3, 11)
_PYTHON_CEILING = (3, 14)

# (id, env var, default port) — env-overridable service listeners.
_SERVICES: tuple[tuple[str, str, int], ...] = (
    ("gateway", "GATEWAY_PORT", 19001),
    ("web", "WEB_PORT", 19000),
    ("frontend", "FRONTEND_PORT", 5173),
    ("agentserver", "AGENT_PORT", 18092),
)

# (id, command, hint when missing) — warn-only, never fail.
_EXTERNAL_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("node", "node", "required for the web/TUI frontends"),
    ("git", "git", "required for git integrations"),
    ("ffmpeg", "ffmpeg", "optional; required for media features"),
)

_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "cat.environment": "Environment",
        "cat.native": "Native dependencies",
        "cat.tools": "External tools",
        "cat.models": "Model APIs",
        "cat.services": "Services",
        "cat.mcp": "MCP servers",
        "cat.internal": "Internal",
        "summary.issues": "{fail} issue(s), {warn} warning(s)",
        "hint.reinstall": "Reinstall the package in a fresh virtualenv",
        "hint.init": "Run `jiuwenswarm-init` to create the workspace",
        "hint.env": "Create config/.env with your model API keys",
    },
    "zh": {
        "cat.environment": "环境",
        "cat.native": "原生依赖",
        "cat.tools": "外部工具",
        "cat.models": "模型 API",
        "cat.services": "服务",
        "cat.mcp": "MCP 服务",
        "cat.internal": "内部",
        "summary.issues": "{fail} 项失败，{warn} 项警告",
        "hint.reinstall": "请在全新的虚拟环境中重新安装",
        "hint.init": "运行 `jiuwenswarm-init` 创建工作区",
        "hint.env": "请在 config/.env 中配置模型 API 密钥",
    },
}

_SYMBOLS = {STATUS_OK: "✓", STATUS_WARN: "!", STATUS_FAIL: "✗"}
_ASCII = {STATUS_OK: "[ OK ]", STATUS_WARN: "[WARN]", STATUS_FAIL: "[FAIL]"}
_COLORS = {STATUS_OK: "\033[32m", STATUS_WARN: "\033[33m", STATUS_FAIL: "\033[31m"}
_RESET = "\033[0m"


@dataclass
class CheckResult:
    """One rendered diagnostic finding."""

    id: str
    category: str
    display_name: str
    status: str
    message: str = ""
    hint: str = ""


@dataclass
class DoctorContext:
    """Runtime context handed to every check factory."""

    deep: bool = False
    timeout: float = 5.0
    lang: str = "en"
    config: dict[str, Any] | None = None
    config_error: str | None = None
    native_result: dict[str, Any] | None = None
    native_error: str | None = None


def _t(key: str, lang: str) -> str:
    table = _MESSAGES.get(lang) or _MESSAGES["en"]
    return table.get(key) or _MESSAGES["en"].get(key) or key


def _data_dir() -> Path:
    configured = os.environ.get("JIUWENSWARM_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".jiuwenswarm"


def _result(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    check_id: str,
    category: str,
    display_name: str,
    status: str,
    message: str = "",
    hint: str = "",
) -> CheckResult:
    return CheckResult(
        id=check_id,
        category=category,
        display_name=display_name,
        status=status,
        message=message,
        hint=hint,
    )


# --------------------------------------------------------------------------- #
# Environment / native checks
# --------------------------------------------------------------------------- #


def _check_python_version(_ctx: DoctorContext) -> list[CheckResult]:
    current = (sys.version_info.major, sys.version_info.minor)
    if current < _PYTHON_FLOOR:
        return [
            _result(
                "python_version",
                "environment",
                "Python version",
                STATUS_FAIL,
                f"{platform.python_version()} (requires >=3.11)",
                "Upgrade Python to 3.11 or newer",
            )
        ]
    if current >= _PYTHON_CEILING:
        return [
            _result(
                "python_version",
                "environment",
                "Python version",
                STATUS_WARN,
                f"{platform.python_version()} (supported: 3.11-3.13)",
                "Unsupported Python; pin to 3.11-3.13",
            )
        ]
    return [
        _result(
            "python_version",
            "environment",
            "Python version",
            STATUS_OK,
            platform.python_version(),
        )
    ]


def _dist_is_editable(dist: importlib.metadata.Distribution) -> bool:
    try:
        direct_url = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        return False
    return direct_url is not None and '"dir"' in direct_url


def _check_install_mode(_ctx: DoctorContext) -> list[CheckResult]:
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        mode = "frozen"
    else:
        try:
            dist = importlib.metadata.distribution(PACKAGE_NAME)
            mode = "editable" if _dist_is_editable(dist) else "installed"
        except importlib.metadata.PackageNotFoundError:
            mode = "source"
    return [
        _result(
            "install_mode",
            "environment",
            "Install mode",
            STATUS_OK,
            f"{mode} · {DISPLAY_NAME} {VERSION}",
        )
    ]


def _check_core(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.native_result is None:
        message = ctx.native_error or "startup diagnostics could not run"
        return [
            _result("core", "environment", "Core diagnostics", STATUS_FAIL, message)
        ]
    results: list[CheckResult] = []
    for check in ctx.native_result.get("checks", []):
        status = STATUS_FAIL if check.get("status") == "failed" else STATUS_OK
        kind = check.get("kind")
        category = "native" if kind == "native_import" else "environment"
        name = str(check.get("display_name") or check.get("name") or "unknown")
        hint = ""
        if status == STATUS_FAIL and kind == "native_import":
            hint = _t("hint.reinstall", ctx.lang)
        results.append(
            _result(
                check.get("name") or "check",
                category,
                name,
                status,
                str(check.get("message") or ""),
                hint,
            )
        )
    return results


def _config_yaml_path() -> Path:
    return _data_dir() / "config" / "config.yaml"


def _check_config_yaml(ctx: DoctorContext) -> list[CheckResult]:
    path = _config_yaml_path()
    if ctx.config is not None:
        return [
            _result("config_yaml", "environment", "config.yaml", STATUS_OK, str(path))
        ]
    if ctx.config_error:
        return [
            _result(
                "config_yaml",
                "environment",
                "config.yaml",
                STATUS_FAIL,
                ctx.config_error,
                _t("hint.init", ctx.lang),
            )
        ]
    return [
        _result(
            "config_yaml",
            "environment",
            "config.yaml",
            STATUS_FAIL,
            f"not found at {path}",
            _t("hint.init", ctx.lang),
        )
    ]


def _check_env_file(ctx: DoctorContext) -> list[CheckResult]:
    path = _data_dir() / "config" / ".env"
    if path.exists():
        return [_result("env_file", "environment", ".env", STATUS_OK, str(path))]
    return [
        _result(
            "env_file",
            "environment",
            ".env",
            STATUS_WARN,
            f"not found at {path}",
            _t("hint.env", ctx.lang),
        )
    ]


# --------------------------------------------------------------------------- #
# External tools
# --------------------------------------------------------------------------- #


def _check_external_tools(_ctx: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool_id, command, hint in _EXTERNAL_TOOLS:
        found = shutil.which(command)
        if found:
            results.append(
                _result(f"tool:{tool_id}", "tools", command, STATUS_OK, found)
            )
        else:
            results.append(
                _result(
                    f"tool:{tool_id}",
                    "tools",
                    command,
                    STATUS_WARN,
                    "not found on PATH",
                    hint,
                )
            )
    return results


# --------------------------------------------------------------------------- #
# Model APIs
# --------------------------------------------------------------------------- #


def _model_entries(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    models = config.get("models")
    if not isinstance(models, dict):
        return []
    defaults = models.get("defaults")
    if isinstance(defaults, list):
        return [entry for entry in defaults if isinstance(entry, dict)]
    if isinstance(defaults, dict):
        return [defaults]
    return []


def _probe_http(url: str, *, api_key: str | None, timeout: float) -> tuple[str, str]:
    """Probe an HTTP(S) endpoint and return (status, message).

    Unauthenticated (``api_key=None``) means "reachable": any HTTP response
    counts, regardless of status code. With a key, a 401/403 is a failure.
    """
    start = time.monotonic()
    headers = {"User-Agent": "workswarm-doctor"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    code: int
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        return STATUS_FAIL, f"{type(exc).__name__}: {exc}"
    latency = int((time.monotonic() - start) * 1000)
    if api_key and code in (401, 403):
        return STATUS_FAIL, f"authenticated request rejected (HTTP {code})"
    return STATUS_OK, f"reachable (HTTP {code}, {latency} ms)"


def _check_model_apis(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.config_error is not None:
        return [
            _result(
                "model_config",
                "models",
                "Model configuration",
                STATUS_FAIL,
                ctx.config_error,
                "Fix config.yaml or run jiuwenswarm-init",
            )
        ]
    if ctx.config is None:
        return [
            _result(
                "model_config",
                "models",
                "Model configuration",
                STATUS_WARN,
                "no config.yaml",
                _t("hint.init", ctx.lang),
            )
        ]
    entries = _model_entries(ctx.config)
    if not entries:
        return [
            _result(
                "model_entries",
                "models",
                "Configured models",
                STATUS_WARN,
                "no entries under models.defaults",
                "Add a model in config.yaml",
            )
        ]

    probes: list[tuple[int, str, str, str, str | None]] = []
    for index, entry in enumerate(entries):
        mcc = entry.get("model_client_config")
        if not isinstance(mcc, dict):
            mcc = entry
        view = model_client_config_view(mcc)
        label = str(
            view.get("model_name")
            or mcc.get("client_provider")
            or f"model #{index + 1}"
        )
        api_base = str(view.get("api_base") or "").strip()
        api_key = str(view.get("api_key") or "").strip()
        if is_placeholder_model_entry(mcc):
            probes.append((index, label, "", api_key, None))
            continue
        probe_key = api_key if (ctx.deep and api_key) else None
        probes.append((index, label, api_base, api_key, probe_key))

    def _probe_entry(item: tuple[int, str, str, str, str | None]) -> CheckResult:
        index, label, api_base, api_key, probe_key = item
        if not api_base:
            return _result(
                f"model:{index}",
                "models",
                label,
                STATUS_WARN,
                "placeholder entry (not configured)",
            )
        key_msg = "key set" if api_key else "key not set"
        status, message = _probe_http(api_base, api_key=probe_key, timeout=ctx.timeout)
        hint = "Check network/proxy or the api_base" if status == STATUS_FAIL else ""
        return _result(
            f"model:{index}",
            "models",
            label,
            status,
            f"{api_base} · {message} · {key_msg}",
            hint,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(_probe_entry, probes))


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_services(_ctx: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, env_var, default_port in _SERVICES:
        try:
            port = int(os.environ.get(env_var, str(default_port)))
        except ValueError:
            port = default_port
        if _port_open(port):
            results.append(
                _result(
                    f"service:{name}",
                    "services",
                    name,
                    STATUS_OK,
                    f"listening on port {port}",
                )
            )
        else:
            results.append(
                _result(
                    f"service:{name}",
                    "services",
                    name,
                    STATUS_OK,
                    f"not running (port {port} free)",
                )
            )
    return results


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #


def _check_mcp(ctx: DoctorContext) -> list[CheckResult]:
    try:
        from jiuwenswarm.common.mcp_config import extract_enabled_mcp_server_entries

        entries = extract_enabled_mcp_server_entries()
    except Exception as exc:  # noqa: BLE001
        return [
            _result(
                "mcp_config",
                "mcp",
                "MCP configuration",
                STATUS_FAIL,
                f"could not load MCP config: {type(exc).__name__}: {exc}",
            )
        ]
    if not entries:
        return [_result("mcp_servers", "mcp", "Enabled MCP servers", STATUS_OK, "none")]
    results: list[CheckResult] = []
    for entry in entries:
        name = str(entry.get("name") or "unnamed")
        transport = str(entry.get("transport") or "").strip().lower()
        if transport == "stdio":
            command = str(entry.get("command") or "").strip()
            if not command:
                results.append(
                    _result(f"mcp:{name}", "mcp", name, STATUS_WARN, "no command")
                )
                continue
            executable = command.split(" ", maxsplit=1)[0]
            if shutil.which(executable):
                results.append(
                    _result(
                        f"mcp:{name}",
                        "mcp",
                        name,
                        STATUS_OK,
                        f"stdio `{command}` found",
                    )
                )
            else:
                results.append(
                    _result(
                        f"mcp:{name}",
                        "mcp",
                        name,
                        STATUS_WARN,
                        f"stdio command `{command}` not on PATH",
                    )
                )
        else:
            url = str(entry.get("url") or "").strip()
            if not url:
                results.append(
                    _result(f"mcp:{name}", "mcp", name, STATUS_WARN, "no URL")
                )
                continue
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                results.append(
                    _result(
                        f"mcp:{name}", "mcp", name, STATUS_FAIL, f"invalid URL: {url}"
                    )
                )
                continue
            origin = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port:
                origin += f":{parsed.port}"
            status, message = _probe_http(origin, api_key=None, timeout=ctx.timeout)
            results.append(_result(f"mcp:{name}", "mcp", name, status, message))
    return results


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

_CHECK_FACTORIES: list[Callable[[DoctorContext], list[CheckResult]]] = [
    _check_python_version,
    _check_install_mode,
    _check_core,
    _check_config_yaml,
    _check_env_file,
    _check_external_tools,
    _check_model_apis,
    _check_services,
    _check_mcp,
]


def run_checks(ctx: DoctorContext) -> list[CheckResult]:
    """Run every registered check, isolating failures per factory."""
    results: list[CheckResult] = []
    for factory in _CHECK_FACTORIES:
        try:
            results.extend(factory(ctx))
        except Exception as exc:  # noqa: BLE001
            results.append(
                _result(
                    factory.__name__,
                    "internal",
                    factory.__name__,
                    STATUS_FAIL,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _group(results: Sequence[CheckResult]) -> list[tuple[str, list[CheckResult]]]:
    order: list[str] = []
    groups: dict[str, list[CheckResult]] = {}
    for result in results:
        if result.category not in groups:
            groups[result.category] = []
            order.append(result.category)
        groups[result.category].append(result)
    return [(category, groups[category]) for category in order]


def _overall_status(results: Sequence[CheckResult]) -> str:
    if any(r.status == STATUS_FAIL for r in results):
        return "error"
    if any(r.status == STATUS_WARN for r in results):
        return "warning"
    return "ok"


def _render_line(result: CheckResult, use_color: bool, unicode_symbols: bool) -> str:
    if unicode_symbols:
        symbol = _SYMBOLS[result.status]
        if use_color:
            symbol = f"{_COLORS[result.status]}{symbol}{_RESET}"
    else:
        symbol = _ASCII[result.status]
    line = f"  {symbol} {result.display_name}"
    if result.message:
        line += f" — {result.message}"
    return line


def render_text(
    results: Sequence[CheckResult],
    lang: str,
    *,
    use_color: bool,
    unicode_symbols: bool,
    verbose: bool,
) -> str:
    """Render the human-readable checklist."""
    lines = [
        f"{DISPLAY_NAME} Doctor {VERSION}",
        f"{platform.system()} · Python {platform.python_version()}",
        "",
    ]
    for category, items in _group(results):
        lines.append(_t(f"cat.{category}", lang))
        for result in items:
            lines.append(_render_line(result, use_color, unicode_symbols))
            if result.hint and (result.status != STATUS_OK or verbose):
                lines.append(f"      hint: {result.hint}")
        lines.append("")
    fails = sum(1 for r in results if r.status == STATUS_FAIL)
    warns = sum(1 for r in results if r.status == STATUS_WARN)
    lines.append(_t("summary.issues", lang).format(fail=fails, warn=warns))
    return "\n".join(lines).rstrip() + "\n"


def render_json(results: Sequence[CheckResult]) -> dict[str, Any]:
    """Render the machine-readable result (``doctor_cli_result`` schema)."""
    return {
        "type": "doctor_cli_result",
        "schema_version": 1,
        "status": _overall_status(results),
        "product": DISPLAY_NAME,
        "version": VERSION,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "summary": {
            "ok": sum(1 for r in results if r.status == STATUS_OK),
            "warn": sum(1 for r in results if r.status == STATUS_WARN),
            "fail": sum(1 for r in results if r.status == STATUS_FAIL),
        },
        "checks": [
            {
                "id": r.id,
                "category": r.category,
                "display_name": r.display_name,
                "status": r.status,
                "message": r.message,
                "hint": r.hint,
            }
            for r in results
        ],
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _run_native() -> tuple[dict[str, Any] | None, str | None]:
    try:
        return sd.run_doctor(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _read_config_yaml() -> tuple[dict[str, Any] | None, str | None]:
    path = _config_yaml_path()
    if not path.exists():
        return None, None
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return None, f"PyYAML not importable: {type(exc).__name__}: {exc}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to parse config.yaml: {type(exc).__name__}: {exc}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "config.yaml root is not a mapping"
    return data, None


def _resolve_lang(cli_lang: str | None, config: dict[str, Any] | None) -> str:
    if cli_lang:
        return cli_lang
    env = os.environ.get("JIUWENSWARM_LANG")
    if env:
        return "zh" if env.strip().lower().startswith("zh") else "en"
    if isinstance(config, dict):
        pref = config.get("preferred_language")
        if isinstance(pref, str):
            return "zh" if "zh" in pref.lower() else "en"
    return "en"


def _exit_code(results: Sequence[CheckResult], strict: bool) -> int:
    if any(r.status == STATUS_FAIL for r in results):
        return sd.DOCTOR_EXIT_ENVIRONMENT_ERROR
    if strict and any(r.status == STATUS_WARN for r in results):
        return sd.DOCTOR_EXIT_ENVIRONMENT_ERROR
    return sd.DOCTOR_EXIT_OK


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-doctor",
        description=f"Diagnose the {DISPLAY_NAME} environment",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON to stdout"
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="run authenticated model-API probes (sends the configured key, may spend tokens)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="show ok-check messages and hints"
    )
    parser.add_argument(
        "--lang", choices=("en", "zh"), default=None, help="output language"
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="per-probe network timeout (seconds)"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures for the exit code",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output",
    )
    parser.add_argument(
        "--output", metavar="PATH", help="additionally write the JSON result to PATH"
    )
    return parser


def _color_and_symbols(color: str) -> tuple[bool, bool]:
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    if color == "never":
        return False, tty
    if color == "always":
        return True, True
    if os.environ.get("NO_COLOR"):
        return False, tty
    return tty, tty


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Suppress application logging during checks: lazily importing the config
    # module (for MCP servers) registers connector pools and emits INFO logs to
    # stdout, which would corrupt ``--json`` output. Doctor's own results are
    # written directly to stdout via ``_emit``, not the logging module.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        native_result, native_error = _run_native()
        config, config_error = _read_config_yaml()
        lang = _resolve_lang(args.lang, config)
        ctx = DoctorContext(
            deep=args.deep,
            timeout=args.timeout,
            lang=lang,
            config=config,
            config_error=config_error,
            native_result=native_result,
            native_error=native_error,
        )
        results = run_checks(ctx)
    except Exception as exc:  # noqa: BLE001
        _emit(f"{DISPLAY_NAME} Doctor failed: {type(exc).__name__}: {exc}\n")
        return sd.DOCTOR_EXIT_INTERNAL_ERROR
    finally:
        logging.disable(previous)

    code = _exit_code(results, args.strict)
    payload = render_json(results)
    if args.output:
        try:
            _write_json(args.output, payload)
        except OSError as exc:
            _emit(f"{DISPLAY_NAME} Doctor: could not write --output: {exc}\n")
            return sd.DOCTOR_EXIT_INTERNAL_ERROR
    if args.json:
        _emit(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        use_color, unicode_symbols = _color_and_symbols(args.color)
        _emit(
            render_text(
                results,
                lang,
                use_color=use_color,
                unicode_symbols=unicode_symbols,
                verbose=args.verbose,
            )
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
