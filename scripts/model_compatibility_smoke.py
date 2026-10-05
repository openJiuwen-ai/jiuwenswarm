"""Run a redacted OpenAI-compatible model compatibility smoke test."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import OpenAI


_PROVIDER_PREFIX = {"deepseek": "DEEPSEEK", "qwen": "QWEN"}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    api_base: str
    model: str
    api_key: str = field(repr=False)


def load_provider_config(provider: str, env_file: Path) -> ProviderConfig:
    prefix = _PROVIDER_PREFIX.get(provider)
    if prefix is None:
        raise ValueError(f"unsupported provider: {provider}")
    values = dotenv_values(env_file)
    fields = {
        "api_base": str(values.get(f"{prefix}_API_BASE") or "").strip(),
        "model": str(values.get(f"{prefix}_MODEL") or "").strip(),
        "api_key": str(values.get(f"{prefix}_API_KEY") or "").strip(),
    }
    missing = [f"{prefix}_{name.upper()}" for name, value in fields.items() if not value]
    if missing:
        raise ValueError(f"missing configuration: {', '.join(missing)}")
    return ProviderConfig(provider=provider, **fields)


def _usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _first_message(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    return getattr(choices[0], "message", None) if choices else None


def build_report(
    *,
    provider: str,
    api_base: str,
    model: str,
    model_listed: bool,
    chat_response: Any,
    tool_response: Any,
    chat_latency_ms: int,
    tool_latency_ms: int,
) -> dict[str, Any]:
    chat_message = _first_message(chat_response)
    chat_ok = bool(str(getattr(chat_message, "content", "") or "").strip())
    tool_message = _first_message(tool_response)
    tool_calls = getattr(tool_message, "tool_calls", None) or []
    called_tool = None
    if tool_calls:
        called_tool = str(getattr(getattr(tool_calls[0], "function", None), "name", "") or "")
    tool_ok = called_tool == "record_probe"
    checks = {
        "model_list": {"status": "PASS" if model_listed else "FAIL"},
        "chat": {
            "status": "PASS" if chat_ok else "FAIL",
            "latency_ms": chat_latency_ms,
            "usage": _usage(chat_response),
        },
        "tool_call": {
            "status": "PASS" if tool_ok else "FAIL",
            "called_tool": called_tool,
            "latency_ms": tool_latency_ms,
            "usage": _usage(tool_response),
        },
    }
    return {
        "provider": provider,
        "api_base": api_base,
        "model": model,
        "status": "PASS" if all(item["status"] == "PASS" for item in checks.values()) else "FAIL",
        "checks": checks,
    }


def run_smoke(config: ProviderConfig) -> dict[str, Any]:
    client = OpenAI(
        api_key=config.api_key,
        base_url=config.api_base,
        timeout=30.0,
        max_retries=0,
    )
    models = client.models.list()
    model_listed = any(str(getattr(item, "id", "")) == config.model for item in models.data)

    started = time.perf_counter()
    chat_response = client.chat.completions.create(
        model=config.model,
        messages=[{"role": "user", "content": "Reply with exactly OK."}],
        max_tokens=16,
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    chat_latency_ms = round((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    tool_response = client.chat.completions.create(
        model=config.model,
        messages=[{"role": "user", "content": "Record the integer value 1."}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "record_probe",
                    "description": "Record one integer for a compatibility check.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
        max_tokens=64,
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    tool_latency_ms = round((time.perf_counter() - started) * 1000)
    return build_report(
        provider=config.provider,
        api_base=config.api_base,
        model=config.model,
        model_listed=model_listed,
        chat_response=chat_response,
        tool_response=tool_response,
        chat_latency_ms=chat_latency_ms,
        tool_latency_ms=tool_latency_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(_PROVIDER_PREFIX), required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env.model-smoke.local"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("evidence/runtime") / f"model-smoke-{args.provider}.json"

    try:
        config = load_provider_config(args.provider, args.env_file)
        report = run_smoke(config)
    except Exception as exc:  # noqa: BLE001 - CLI emits only redacted failure metadata
        report = {
            "provider": args.provider,
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "http_status": getattr(exc, "status_code", None),
        }
    report["evaluated_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{args.provider}: {report['status']} ({output})")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
