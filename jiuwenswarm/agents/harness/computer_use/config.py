"""Serializable, host-owned Computer Use settings and bundled source lookup."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def toolkit_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "cua_engine"
    if (root / "pyproject.toml").is_file():
        return root
    raise RuntimeError(
        "Bundled Computer Use sources are missing; reinstall jiuwenswarm."
    )


@dataclass(frozen=True)
class ComputerUseSettings:
    desktop_id: str = "local"
    command: str = sys.executable
    args: tuple[str, ...] = (
        "-m",
        "jiuwenswarm.agents.harness.computer_use.launcher",
    )
    env: dict[str, str] = field(
        default_factory=lambda: {"COMPUTER_USE_DESKTOP": "shared"}
    )
    cwd: str | None = None
    skill_path: str | None = None
    lock_dir: str | None = None
    startup_timeout: float = 180.0
    call_timeout: float = 180.0
    cleanup_timeout: float = 30.0

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> ComputerUseSettings:
        raw = (config or {}).get("computer_use", {})
        if not isinstance(raw, dict):
            raise ValueError("computer_use must be a mapping")
        mcp = raw.get("mcp", {})
        if not isinstance(mcp, dict):
            raise ValueError("computer_use.mcp must be a mapping")
        defaults = cls()
        desktop_id = raw.get("desktop_id", defaults.desktop_id)
        command = defaults.command if mcp.get("command") is None else mcp["command"]
        args = defaults.args if mcp.get("args") is None else mcp["args"]
        env = mcp.get("env", {})
        if not isinstance(desktop_id, str) or not desktop_id.strip():
            raise ValueError("computer_use.desktop_id must be nonempty")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("computer_use.mcp.command must be nonempty")
        if not isinstance(args, (list, tuple)) or not all(
            isinstance(arg, str) for arg in args
        ):
            raise ValueError("computer_use.mcp.args must be a list of strings")
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("computer_use.mcp.env must contain string keys and values")
        mode = env.get("COMPUTER_USE_DESKTOP", "shared")
        if mode not in {"shared", "exclusive"}:
            raise ValueError("COMPUTER_USE_DESKTOP must be shared or exclusive")
        optional = {}
        for name in ("skill_path", "lock_dir"):
            value = raw.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"computer_use.{name} must be a nonempty path")
            optional[name] = value
        cwd = mcp.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            raise ValueError("computer_use.mcp.cwd must be a nonempty path")
        for name in ("startup_timeout", "call_timeout", "cleanup_timeout"):
            value = raw.get(name, getattr(defaults, name))
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"computer_use.{name} must be positive seconds")
            optional[name] = float(value)
        return cls(
            desktop_id=desktop_id.strip(),
            command=command,
            args=tuple(args),
            env={**defaults.env, **env},
            cwd=cwd,
            **optional,
        )


def load_skill(settings: ComputerUseSettings, platform: str | None) -> str:
    if settings.skill_path:
        return Path(settings.skill_path).expanduser().read_text(encoding="utf-8")
    root = toolkit_root()
    if platform in {"macos", "windows"}:
        path = root / "skills" / f"computer-use-{platform}" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
    elif platform is not None:
        raise RuntimeError(
            f"Bundled Computer Use supports macOS and Windows; MCP host reported {platform!r}."
        )
    else:
        text = (
            "The MCP host did not identify its platform. Establish the target system before "
            "using platform-specific selectors or shortcuts.\n\n"
            + (root / "skills" / "computer-use" / "base.md").read_text(encoding="utf-8")
        )
    return text
