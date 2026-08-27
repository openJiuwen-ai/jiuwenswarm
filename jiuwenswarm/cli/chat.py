# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat command orchestration: parse args, connect Gateway, stream response."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import signal
import sys
import time
import uuid as uuid_module
from pathlib import Path
from typing import Any

# Importing ``readline`` transparently upgrades the builtin ``input()`` with
# line editing: left/right cursor movement, history, and correct multi-byte
# UTF-8 deletion (so backspacing a Chinese character removes the whole glyph
# instead of leaving a broken byte / raw escape sequences like ``^[[C``).
try:
    import readline
    _ = readline  # side-effect import — CPython input() hooks into readline
except ImportError:
    # Windows lacks the ``readline`` module.  Reconfigure stdin to UTF-8 so
    # that multi-byte characters (Chinese, Japanese, etc.) are read and
    # displayed correctly, and try ``pyreadline3`` (a drop-in ``readline``
    # replacement for Windows) when available.
    if sys.platform == "win32":
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
        try:
            import pyreadline3
            _ = pyreadline3  # Windows readline replacement
        except ImportError:
            pass

from jiuwenswarm.cli._terminal import write_stderr, write_stdout
from jiuwenswarm.cli.gateway_client import GatewayClient
from jiuwenswarm.cli.events import (
    event_kind,
    is_content_final,
    is_terminal_event,
    needs_user_input,
)
from jiuwenswarm.cli.render import HumanRenderer, JsonRenderer, JsonlRenderer

logger = logging.getLogger(__name__)

# ── Trusted-directory persistence (local, no server/harness changes) ───

# 基于 get_agent_workspace_dir()（~/.jiuwenswarm/agent/workspace），
# 跟随 JIUWENSWARM_DATA_DIR 做多实例隔离，与 config.yaml 保持同一基准。
from jiuwenswarm.common.utils import get_agent_workspace_dir

_STATE_FILE = get_agent_workspace_dir() / "cli_trusted_dirs_state.json"


def _normalize_dir(path: str) -> str:
    """将目录路径归一化为绝对、去除末尾分隔符的形式。

    统一 _build_request / _get_persisted_external_dirs / _prompt_and_cleanup_dirs
    的路径比对基准，避免因末尾 '/'、相对路径或符号链接导致重复或误判。
    """
    return str(Path(path).expanduser().resolve())


def _load_state() -> dict[str, bool]:
    """Load per-directory prompt state.  {dir_path: keep_bool}."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def _save_state(state: dict[str, bool]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _get_persisted_external_dirs() -> list[str]:
    """读取已信任目录：优先 ``file_guard.paths``（read/write allow），过渡期兼容 ``external_directory`` allow。"""
    from jiuwenswarm.common.config import CONFIG_YAML_PATH, load_yaml_round_trip

    cfg_path = Path(CONFIG_YAML_PATH) if not isinstance(CONFIG_YAML_PATH, Path) else CONFIG_YAML_PATH
    if not cfg_path.exists():
        return []
    try:
        data = load_yaml_round_trip(cfg_path)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []

    result: list[str] = []
    seen: set[str] = set()

    fg = perms.get("file_guard")
    if isinstance(fg, dict):
        paths = fg.get("paths")
        if isinstance(paths, list):
            for item in paths:
                if not isinstance(item, dict):
                    continue
                raw = item.get("path")
                if not isinstance(raw, str) or not raw.strip():
                    continue
                # 信任目录约定：read+write allow（exec 可为 ask）
                if str(item.get("read") or "").lower() != "allow":
                    continue
                if str(item.get("write") or "").lower() != "allow":
                    continue
                if str(item.get("match") or "prefix") == "glob":
                    continue
                norm = _normalize_dir(raw)
                if norm and norm not in seen:
                    seen.add(norm)
                    result.append(norm)

    ext = perms.get("external_directory")
    if isinstance(ext, dict):
        for k, v in ext.items():
            if str(k) != "*" and str(v) == "allow":
                norm = _normalize_dir(str(k))
                if norm and norm not in seen:
                    seen.add(norm)
                    result.append(norm)
    return result


def _remove_dir_from_config(dir_path: str) -> bool:
    """从 config.yaml 移除信任目录（``file_guard.paths`` 及过渡期 ``external_directory``）。"""
    from jiuwenswarm.common.config import CONFIG_YAML_PATH, load_yaml_round_trip, dump_yaml_round_trip

    cfg_path = Path(CONFIG_YAML_PATH) if not isinstance(CONFIG_YAML_PATH, Path) else CONFIG_YAML_PATH
    if not cfg_path.exists():
        return False
    try:
        data = load_yaml_round_trip(cfg_path)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return False

    target = _normalize_dir(dir_path)
    removed = False

    fg = perms.get("file_guard")
    if isinstance(fg, dict):
        paths = fg.get("paths")
        if isinstance(paths, list):
            new_paths = []
            for item in paths:
                if not isinstance(item, dict):
                    new_paths.append(item)
                    continue
                raw = item.get("path")
                if isinstance(raw, str) and _normalize_dir(raw) == target:
                    removed = True
                    continue
                new_paths.append(item)
            if removed:
                fg["paths"] = new_paths

    ext = perms.get("external_directory")
    if isinstance(ext, dict):
        key_to_remove = None
        for key in list(ext.keys()):
            if _normalize_dir(str(key)) == target:
                key_to_remove = key
                break
        if key_to_remove is not None:
            del ext[key_to_remove]
            removed = True

    if not removed:
        return False
    try:
        dump_yaml_round_trip(cfg_path, data)
    except Exception:
        return False
    return True


async def _persist_trusted_dirs(client: GatewayClient, trusted_dirs: list[str]) -> None:
    """Tell the agent-server to persist each trusted directory via ``command.add_dir``."""
    failed: list[str] = []
    for d in trusted_dirs:
        resolved = _normalize_dir(d)
        try:
            await client.send_request({
                "type": "req",
                "id": f"add_dir-{uuid_module.uuid4().hex[:8]}",
                "method": "command.add_dir",
                "is_stream": False,
                "params": {"path": resolved, "remember": True},
            })
        except Exception:
            logger.warning("Failed to persist trusted directory: %s", resolved, exc_info=True)
            failed.append(resolved)
    if failed:
        write_stderr(
            "Warning: could not persist the following trusted directories:\n  "
            + "\n  ".join(failed)
            + "\n"
            + "The agent-server may not support command.add_dir.\n"
        )


def _prompt_and_cleanup_dirs() -> None:
    """Check whether persisted dirs are covered by state; if not, ask once for all.

    Only called from the REPL loop (interactive TTY).
    """
    if not sys.stdin.isatty():
        return

    persisted = _get_persisted_external_dirs()
    state = _load_state()
    new_dirs = sorted(d for d in persisted if d not in state)

    # All persisted dirs already covered by state — nothing to confirm.
    if not new_dirs:
        return

    write_stderr("\nNew trusted directories detected:\n")
    for d in new_dirs:
        write_stderr(f"  {d}\n")
    try:
        answer = input("Keep these directories as trusted? [Y/n]: ").strip().lower()
    except EOFError:
        answer = "y"

    keep = answer not in ("n", "no")
    for d in new_dirs:
        state[d] = keep
    _save_state(state)

    if not keep:
        for d in new_dirs:
            if _remove_dir_from_config(d):
                write_stderr(f"Trusted directory removed: {d}\n")
            else:
                write_stderr(f"Failed to remove trusted directory: {d}\n")

MODE_ALIASES: dict[str, str] = {
    "plan": "agent",
    "fast": "agent",
    "code": "code.normal",
    "team.plan": "team.plan.normal",
}

# plan / fast 已合并为单一 agent 模式；agent.plan / agent.fast 作为历史别名仍可接受，归一到 agent。
VALID_MODES = frozenset({
    "agent", "agent.plan", "agent.fast", "code.plan", "code.normal", "code.team", "team",
    "team.plan", "team.plan.normal", "team.plan.code",
})

# Sources that require the answer to be sent via ``chat.send`` (streaming) to
# resume the paused agent task.  Other sources use ``chat.user_answer``.
_INTERRUPT_RESUME_SOURCES = frozenset({
    "confirm_interrupt",
    "permission_interrupt",
    "ask_user_interrupt",
    "evolution_interrupt",
})


def _permission_answer_for_card(
    questions: object,
    *,
    selected: str,
    custom_input: str,
) -> list[dict[str, object]]:
    if not isinstance(questions, list) or len(questions) != 1:
        return []
    question = questions[0]
    if not isinstance(question, dict):
        return []
    raw_card_id = question.get("card_id")
    if not isinstance(raw_card_id, str):
        return []
    card_id = raw_card_id.strip()
    if not card_id or len(card_id) > 128:
        return []
    return [{
        "selected_options": [selected],
        "custom_input": custom_input,
        "card_id": card_id,
    }]


def resolve_mode(raw: str) -> str:
    normalized = raw.strip().lower()
    if normalized in MODE_ALIASES:
        normalized = MODE_ALIASES[normalized]
    if normalized in ("agent.plan", "agent.fast"):
        normalized = "agent"
    if normalized in VALID_MODES:
        return normalized
    raise ValueError(
        f"invalid mode: {raw!r}.  valid values: {', '.join(sorted(VALID_MODES))}"
    )


def _build_default_gateway_url(path: str = "/tui") -> str:
    port = os.getenv("GATEWAY_PORT", "19001")
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    return f"ws://{host}:{port}{path}"


def _generate_session_id() -> str:
    now = datetime.datetime.now(datetime.UTC)
    short = uuid_module.uuid4().hex[:8]
    return f"cli-{now.strftime('%Y%m%d-%H%M%S')}-{short}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jiuwenswarm chat",
        description="Chat with JiuwenSwarm from the command line.",
    )
    p.add_argument(
        "prompt", nargs="*",
        help="Prompt text (joined with spaces).  If omitted and stdin is piped, read stdin.",
    )
    p.add_argument(
        "--mode", default="code.normal",
        help="Execution mode: agent|code|team|team.plan|team.plan.normal|team.plan.code|"
             "code.plan|code.normal|code.team"
             " (default: code.normal).",
    )
    p.add_argument(
        "--session",
        help="Reuse or create a named session id.",
    )
    p.add_argument(
        "--cwd",
        help="Working directory for file mentions and agent context.",
    )
    p.add_argument(
        "--project-dir",
        help="Project identity for session and agent cache.  Defaults to --cwd.",
    )
    p.add_argument(
        "--trusted-dir", action="append", default=None,
        help="Trusted directory (repeatable).  Defaults to --project-dir.",
    )
    p.add_argument(
        "--gateway-url",
        help="Explicit Gateway WebSocket URL.",
    )
    p.add_argument(
        "--name",
        help="Named instance for env isolation.",
    )
    p.add_argument(
        "--dotenv",
        help="Path to .env file for env isolation.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Print one final JSON object.",
    )
    p.add_argument(
        "--jsonl", action="store_true",
        help="Print each Gateway event frame as JSON Lines.",
    )
    p.add_argument(
        "--show-reasoning", action="store_true",
        help="Include reasoning output (stderr).",
    )
    p.add_argument(
        "--show-tools", action="store_true",
        help="Include compact tool call/result status (stderr).",
    )
    p.add_argument(
        "--timeout", type=float,
        help="Total response timeout in seconds.",
    )
    return p


def _validate_args(args: argparse.Namespace) -> int | None:
    try:
        args.mode = resolve_mode(args.mode)
    except ValueError as exc:
        logger.error("invalid mode: %s", exc)
        return 2
    if args.json and args.jsonl:
        logger.error("--json and --jsonl are mutually exclusive")
        return 2
    if args.show_reasoning and args.jsonl:
        pass
    if args.timeout is not None and args.timeout <= 0:
        logger.error("--timeout must be positive")
        return 2
    return None


def _build_request(
    args: argparse.Namespace,
    prompt: str,
    *,
    supports_user_interaction: bool = False,
) -> dict:
    session_id = args.session or _generate_session_id()
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    project_dir = str(Path(args.project_dir or cwd).resolve())
    trusted_dirs = args.trusted_dir
    if not trusted_dirs:
        trusted_dirs = [project_dir]
    else:
        trusted_dirs = [_normalize_dir(d) for d in trusted_dirs]

    # Merge persisted external_directory allow entries into trusted_dirs
    # so that RuntimePromptRail can inject them into the system prompt.
    persisted = _get_persisted_external_dirs()
    for pd in persisted:
        if pd not in trusted_dirs:
            trusted_dirs.append(pd)

    return {
        "type": "req",
        "id": f"chat-{uuid_module.uuid4().hex[:12]}",
        "method": "chat.send",
        "is_stream": True,
        "params": {
            "session_id": session_id,
            "content": prompt,
            "query": prompt,
            "mode": args.mode,
            "cwd": cwd,
            "project_dir": project_dir,
            "trusted_dirs": trusted_dirs,
            "supports_user_interaction": supports_user_interaction,
            # V2: 显式发 agent_ref，支撑同 session 切 mode 不串窗（设计 §5.2 场景 2）。
            # gateway 用 (channel, scope, agent_ref) 3 元组注册/查找，AgentServer 回带
            # 同值，切换 mode 后旧 agent 的延迟 chunk 不会错路由到新 agent 的 UI 区块。
            "agent_ref": {"mode": args.mode, "id": "default"},
        },
    }


async def _spinner_loop(renderer: HumanRenderer) -> None:
    while renderer.loading:
        renderer.tick_spinner()
        await asyncio.sleep(0.2)


def _request_supports_user_interaction(request: dict) -> bool:
    params = request.get("params", {})
    return (
        not isinstance(params, dict)
        or params.get("supports_user_interaction") is not False
    )


async def _run_interactive_loop(
    client: GatewayClient,
    renderer: HumanRenderer,
    request: dict,
    *,
    timeout: float | None = None,
) -> int:
    interrupted = False
    supports_user_interaction = _request_supports_user_interaction(request)
    # `timeout` is the TOTAL response timeout (matches --help/docs). We track
    # the deadline and compute the remaining budget for each recv() call so
    # long-running streams still respect the overall cap. When unset, a
    # generous per-recv idle timeout keeps the connection alive.
    total_timeout = timeout if timeout and timeout > 0 else None
    # Short idle timeout so the loop can react to Ctrl+C within ~1 second
    # on platforms where signal handlers cannot wake a blocked socket recv
    # (e.g. Windows ProactorEventLoop). The spinner ticks every 0.2 s, so
    # the loop is woken up frequently regardless.
    recv_idle_timeout = 1.0
    deadline = None
    if total_timeout is not None:
        deadline = time.monotonic() + total_timeout
    # In team mode the session is persistent: leader replies arrive as
    # chat.final but the team keeps working (creating workflows, delegating
    # to members, etc.). Only chat.processing_status(is_processing=False)
    # or team.error should terminate the CLI stream.
    from jiuwenswarm.common.mode_matrix import is_team_mode
    team_mode = is_team_mode(request.get("params", {}).get("mode"))
    # When the team leader replies with text but creates no tasks (e.g. a
    # simple greeting), the server's team-completion logic never fires
    # (is_team_completed() returns None for zero tasks), so the stream
    # never closes and processing_status(False) never arrives.  We detect
    # this stall by watching for chat.final (leader reply) and then
    # waiting a short idle window; if no further events arrive, we treat
    # the round as complete and prompt the user.
    team_final_seen = False
    team_final_time: float = 0.0
    _team_round_idle_timeout = 3.0
    # In plan mode the agent may end its turn with a text question (without
    # calling ask_user or exit_plan_mode). In that case chat.final arrives
    # but the conversation isn't really done — the user needs to respond.
    # We track plan_exited (set when plan.mode_exited is received) to
    # distinguish "agent finished implementing after approval" from "agent
    # ended turn with text, waiting for user follow-up".
    plan_mode = request.get("params", {}).get("mode", "") in ("code.plan", "agent", "agent.plan")
    plan_exited = False
    # When we send an interrupt-resume answer (chat.send with source), the
    # previous stream's trailing chat.final / processing_status(False) may
    # still arrive. We skip terminal events until the resumed stream sends
    # its own processing_status(False).
    awaiting_resume = False
    # Tracks whether we just sent a plan-approval answer (source=confirm_interrupt
    # with exit_plan_mode). When True, the next chat.final is the completion of
    # the approved plan execution — do NOT prompt for follow-up. plan.mode_exited
    # arrives as a separate push AFTER the stream finishes, so plan_exited would
    # still be False when chat.final hits.
    _approval_resume = False

    loop = asyncio.get_running_loop()

    def _on_first_sigint() -> None:
        nonlocal interrupted
        interrupted = True
        logger.warning("Interrupted. Sending cancel (press Ctrl+C again to force exit)...")
        try:
            loop.add_signal_handler(signal.SIGINT, _on_second_sigint)
        except NotImplementedError:
            pass

    def _on_second_sigint() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    # Windows does not support loop.add_signal_handler; fall back to
    # signal.signal so Ctrl+C sets the interrupted flag instead of raising
    # KeyboardInterrupt and skipping the graceful cancel path.
    _win_prev_handler = None
    _force_exit = False

    try:
        loop.add_signal_handler(signal.SIGINT, _on_first_sigint)
    except NotImplementedError:
        import threading

        def _win_sigint_handler(signum, frame) -> None:
            nonlocal interrupted, _force_exit
            if interrupted:
                # Second Ctrl+C — force exit (only log once)
                if not _force_exit:
                    _force_exit = True
                    logger.warning("Force exiting...")
                return
            interrupted = True
            logger.warning("Interrupted. Sending cancel (press Ctrl+C again to force exit)...")

        # signal.signal must be called from the main thread.
        if threading.current_thread() is threading.main_thread():
            _win_prev_handler = signal.signal(signal.SIGINT, _win_sigint_handler)

    await client.send_request(request)

    renderer.ensure_loading()
    spinner = asyncio.create_task(_spinner_loop(renderer))

    try:
        while True:
            # Check interruption flags immediately (no blocking wait).
            if _force_exit:
                renderer.clear_loading()
                return 130

            if interrupted:
                renderer.clear_loading()
                try:
                    await asyncio.wait_for(
                        client.send_request({
                            "type": "req",
                            "id": f"interrupt-{uuid_module.uuid4().hex[:8]}",
                            "method": "chat.interrupt",
                            "is_stream": False,
                            "params": {
                                "session_id": request["params"]["session_id"],
                                "intent": "cancel",
                                "mode": request["params"]["mode"],
                            },
                        }),
                        timeout=3.0,
                    )
                except (asyncio.TimeoutError, ConnectionError, KeyboardInterrupt):
                    pass
                except Exception:
                    logger.exception("cancel request failed")
                return 130

            # Compute the remaining budget for the total deadline.
            # In team mode after the leader's chat.final, use a short idle
            # window to detect round-completion stall (no tasks created).
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("response timed out")
                    return 1
                cur_timeout = max(min(remaining, 0.3), 0.01)
            elif team_final_seen:
                elapsed = time.monotonic() - team_final_time
                cur_timeout = max(_team_round_idle_timeout - elapsed, 0.1)
            else:
                cur_timeout = recv_idle_timeout

            # Race recv against a short sleep so the event loop can
            # react to Ctrl+C flags (critical on Windows ProactorEventLoop
            # where asyncio.wait_for cancellation may not interrupt IOCP).
            recv_task = asyncio.create_task(client.recv())
            sleep_task = asyncio.create_task(asyncio.sleep(cur_timeout))
            done, _ = await asyncio.wait(
                [recv_task, sleep_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel whichever didn't finish. Await recv cancellation with a
            # short timeout to avoid concurrent reads on the WebSocket,
            # but never block indefinitely on a stuck recv cancellation.
            if not recv_task.done():
                recv_task.cancel()
                try:
                    await asyncio.wait_for(recv_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            if not sleep_task.done():
                sleep_task.cancel()

            # Total deadline exhausted
            if recv_task not in done and deadline is not None:
                if time.monotonic() >= deadline:
                    logger.warning("response timed out")
                    return 1
                continue

            # Team idle-watch: leader's chat.final seen and idle window
            # elapsed with no further events — server's team-completion
            # stalled (no tasks). Prompt for next message on TTY.
            if recv_task not in done and team_final_seen:
                team_final_seen = False
                if sys.stdin.isatty():
                    renderer.clear_loading()
                    if renderer.streamed_text and not renderer.streamed_text.endswith("\n"):
                        write_stdout("\n")
                    try:
                        line = (await asyncio.to_thread(input, "\n> ")).strip()
                    except EOFError:
                        return 0
                    if not line or line in ("/exit", "/quit", "/q"):
                        return 0
                    renderer.reset_streamed_text()
                    await client.send_request({
                        "type": "req",
                        "id": f"chat-{uuid_module.uuid4().hex[:12]}",
                        "method": "chat.send",
                        "is_stream": True,
                        "params": {
                            "session_id": request["params"]["session_id"],
                            "query": line,
                            "mode": request["params"]["mode"],
                            "cwd": request["params"].get("cwd", ""),
                            "project_dir": request["params"].get("project_dir", ""),
                            "supports_user_interaction": supports_user_interaction,
                        },
                    })
                    renderer.ensure_loading()
                    continue
                # Non-TTY: no way to prompt, exit
                return 0

            try:
                data = recv_task.result()
            except OSError:
                logger.warning("WebSocket connection lost — the Gateway may have closed the connection", exc_info=True)
                write_stderr("Connection lost. Use --session to re-connect or retry.\n")
                return 4
            except Exception:
                logger.warning("Unexpected error while receiving", exc_info=True)
                write_stderr(
                    "Error receiving response. Check ~/.jiuwenswarm/agent/.logs/full.log"
                    " (gateway errors: gateway.log in the same dir"
                    " or $AGENTOS_GATEWAY_LOG_DIR)\n"
                )
                return 5
            except KeyboardInterrupt:
                if _force_exit:
                    return 130
                if not interrupted:
                    interrupted = True
                continue
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.exception("recv_task.result() failed")
                continue

            if data.get("type") != "event":
                continue

            event_type = data.get("event", "")
            payload = data.get("payload", {})
            kind = event_kind(event_type)

            # Any event arriving during the team idle-watch window means
            # the team is still active (member tool calls, reasoning, etc.).
            # Reset the idle timer so we don't prematurely prompt the user.
            # chat.final re-arms it below.
            if team_final_seen and kind != "final":
                team_final_seen = False

            if kind == "delta":
                renderer.handle_delta(payload)
            elif kind == "reasoning":
                renderer.handle_reasoning(payload)
            elif kind == "tool_call":
                renderer.handle_tool_call(payload)
            elif kind == "tool_result":
                renderer.handle_tool_result(payload)
            elif kind == "final":
                # team.error is broadcast through the chat.final envelope
                # (gateway default for unknown EventType). Route it to the
                # error handler so the message is shown instead of silently
                # exiting on an empty content field.
                if payload.get("event_type") == "team.error":
                    renderer.handle_error(payload)
                    return 1
                # Unknown/control event types are transported in a chat.final
                # envelope. They must not consume HumanRenderer's one final
                # slot or arm the team idle timer.
                if not is_content_final(payload):
                    continue
                renderer.handle_final(payload)
                # In team mode, the leader's chat.final is a turn boundary
                # but not a terminal event.  Start the idle-watch timer so
                # we can detect when the server's team-completion logic has
                # stalled (no tasks created → no processing_status(False)).
                if team_mode:
                    team_final_seen = True
                    team_final_time = time.monotonic()
            elif kind == "error":
                renderer.handle_error(payload)
                return 1
            elif kind == "processing_status":
                if payload.get("is_processing", False):
                    renderer.ensure_loading()
            elif kind == "interactive":
                renderer.clear_loading()
                if supports_user_interaction and sys.stdin.isatty():
                    request_id = payload.get("request_id", "")
                    source = payload.get("source", "")
                    questions = payload.get("questions", [])
                    primary = (
                        questions[0]
                        if isinstance(questions, list)
                        and questions
                        and isinstance(questions[0], dict)
                        else payload
                    )
                    options = primary.get("options", [])
                    all_options: list[dict[str, Any]] = [opt for opt in options if isinstance(opt, dict)]

                    if options:
                        write_stderr(
                            f"\n[{event_type}] "
                            f"{primary.get('question') or primary.get('message', 'Input needed')}\n"
                        )
                        for idx, opt in enumerate(all_options, 1):
                            label = opt.get("label") or opt.get("value") or "?"
                            desc = opt.get("description", "")
                            if desc:
                                write_stderr(f"  {idx}. {label} — {desc}\n")
                            else:
                                write_stderr(f"  {idx}. {label}\n")
                    else:
                        write_stderr(
                            f"\n[{event_type}] "
                            f"{primary.get('question') or primary.get('message', 'Input needed')}\n"
                        )

                    answer = (await asyncio.to_thread(input, "> ")).strip()
                    # Map numeric input or label text to the option's value.
                    # E.g. "1" → "approve", "Approve" → "approve", "批准" → "approve"
                    selected = answer
                    if all_options:
                        # Try numeric index (1-based)
                        if answer.isdigit():
                            idx = int(answer) - 1
                            if 0 <= idx < len(all_options):
                                selected = str(all_options[idx].get("value") or all_options[idx].get("label") or answer)
                        # Try matching label (case-insensitive)
                        if selected == answer:
                            for opt in all_options:
                                label = str(opt.get("label") or "")
                                if label and label.lower() == answer.lower():
                                    selected = str(opt.get("value") or label)
                                    break
                    answers = (
                        _permission_answer_for_card(
                            questions,
                            selected=selected,
                            custom_input=answer,
                        )
                        if source == "permission_interrupt"
                        else [{"selected_options": [selected], "custom_input": answer}]
                    )

                    try:
                        if source in _INTERRUPT_RESUME_SOURCES and request_id:
                            # Interrupt resume: send via chat.send (streaming)
                            # to resume the paused agent task. Using
                            # chat.user_answer here would NOT resume the task.
                            awaiting_resume = True
                            if source == "confirm_interrupt":
                                _approval_resume = True
                            await client.send_request({
                                "type": "req",
                                "id": f"answer-{uuid_module.uuid4().hex[:12]}",
                                "method": "chat.send",
                                "is_stream": True,
                                "params": {
                                    "session_id": request["params"]["session_id"],
                                    "query": "",
                                    "request_id": request_id,
                                    "answers": answers,
                                    "source": source,
                                    "mode": request["params"]["mode"],
                                    "supports_user_interaction": True,
                                },
                            })
                        else:
                            await client.send_request({
                                "type": "req",
                                "id": f"answer-{uuid_module.uuid4().hex[:12]}",
                                "method": "chat.user_answer",
                                "is_stream": False,
                                "params": {
                                    "session_id": request["params"]["session_id"],
                                    "answers": answers,
                                    "request_id": request_id,
                                },
                            })
                    except ConnectionError:
                        return 0
                else:
                    logger.error(
                        "interactive input is unavailable for this chat invocation: %s",
                        event_type,
                    )
                    return 4
            elif event_type == "plan.mode_exited":
                # Agent exited plan mode (user approved). Subsequent
                # chat.final is a real terminal event.
                plan_exited = True

            if is_terminal_event(event_type, payload):
                # After sending an interrupt-resume answer, the previous
                # stream's trailing chat.final / processing_status(False)
                # may arrive. Skip them — the resumed stream will send its
                # own terminal events.
                if awaiting_resume:
                    awaiting_resume = False
                    continue
                # In team mode, chat.final (leader reply) is not terminal —
                # the team keeps working after the leader responds. Only
                # processing_status(is_processing=False) or team.error ends
                # the stream.
                if team_mode and event_type == "chat.final":
                    if payload.get("event_type") == "team.error":
                        renderer.clear_loading()
                        return 1
                    # leader reply or team control event — keep listening
                    continue
                # In team mode, a completed round (processing_status
                # is_processing=False / team.completed) is NOT the end of
                # the session — the team runtime is persistent and
                # inherently conversational. Treat round completion as a
                # turn boundary: prompt for the next message on TTY.
                if (
                    team_mode
                    and event_type == "chat.processing_status"
                    and sys.stdin.isatty()
                ):
                    team_final_seen = False
                    renderer.clear_loading()
                    if renderer.streamed_text and not renderer.streamed_text.endswith("\n"):
                        write_stdout("\n")
                    try:
                        line = (await asyncio.to_thread(input, "\n> ")).strip()
                    except EOFError:
                        return 0
                    if not line or line in ("/exit", "/quit", "/q"):
                        return 0
                    renderer.reset_streamed_text()
                    await client.send_request({
                        "type": "req",
                        "id": f"chat-{uuid_module.uuid4().hex[:12]}",
                        "method": "chat.send",
                        "is_stream": True,
                        "params": {
                            "session_id": request["params"]["session_id"],
                            "query": line,
                            "mode": request["params"]["mode"],
                            "cwd": request["params"].get("cwd", ""),
                            "project_dir": request["params"].get("project_dir", ""),
                            "supports_user_interaction": supports_user_interaction,
                        },
                    })
                    renderer.ensure_loading()
                    continue
                # In plan mode, chat.final is only terminal if the agent
                # has exited plan mode (plan_exited=True) or this is an
                # approval-resume completion. Otherwise prompt for follow-up.
                _follow_up_in_plan = (
                    plan_mode and not plan_exited and not _approval_resume
                )
                if _follow_up_in_plan and event_type == "chat.final" and sys.stdin.isatty():
                    renderer.clear_loading()
                    if renderer.streamed_text and not renderer.streamed_text.endswith("\n"):
                        write_stdout("\n")
                    try:
                        line = (await asyncio.to_thread(input, "\n> ")).strip()
                    except EOFError:
                        return 0
                    if not line or line in ("/exit", "/quit", "/q"):
                        return 0
                    # Send follow-up message and continue streaming.
                    # Reset the renderer's streamed text so the next
                    # response starts fresh.
                    renderer.reset_streamed_text()
                    await client.send_request({
                        "type": "req",
                        "id": f"chat-{uuid_module.uuid4().hex[:12]}",
                        "method": "chat.send",
                        "is_stream": True,
                        "params": {
                            "session_id": request["params"]["session_id"],
                            "query": line,
                            "mode": request["params"]["mode"],
                            "cwd": request["params"].get("cwd", ""),
                            "project_dir": request["params"].get("project_dir", ""),
                            "supports_user_interaction": supports_user_interaction,
                        },
                    })
                    # After sending the follow-up, the previous stream's
                    # trailing chat.final / processing_status(False) may
                    # still arrive. Skip them and wait for the new stream.
                    awaiting_resume = True
                    continue
                renderer.clear_loading()
                if renderer.streamed_text and not renderer.streamed_text.endswith("\n"):
                    write_stdout("\n")
                return 0

    finally:
        spinner.cancel()
        try:
            await spinner
        except asyncio.CancelledError:
            pass
        # Restore the previous SIGINT handler (Windows fallback or Unix
        # loop handler removal) so the REPL loop regains control.
        if _win_prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, _win_prev_handler)
            except (ValueError, OSError):
                pass
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, ValueError):
            pass


async def _run_jsonl_loop(
    client: GatewayClient,
    renderer: JsonlRenderer,
    request: dict,
) -> int:
    from jiuwenswarm.common.mode_matrix import is_team_mode
    team_mode = is_team_mode(request.get("params", {}).get("mode"))
    await client.send_request(request)
    while True:
        data = await client.recv()
        if data.get("type") != "event":
            continue
        event_type = data.get("event", "")
        payload = data.get("payload", {})
        renderer.handle_event(event_type, payload)
        if needs_user_input(event_type) and not _request_supports_user_interaction(request):
            logger.error(
                "interactive input is unavailable for this chat invocation: %s",
                event_type,
            )
            return 4
        if event_type == "chat.error":
            return 1
        if is_terminal_event(event_type, payload):
            # team.error is wrapped in chat.final by the gateway; treat it
            # as an error exit so callers can detect the failure.
            if payload.get("event_type") == "team.error":
                return 1
            # In team mode, chat.final (leader reply) is not terminal —
            # the team keeps working. Keep listening for
            # processing_status(is_processing=False).
            if team_mode and event_type == "chat.final":
                continue
            return 0


async def _run_json_loop(
    client: GatewayClient,
    renderer: JsonRenderer,
    request: dict,
) -> int:
    from jiuwenswarm.common.mode_matrix import is_team_mode
    team_mode = is_team_mode(request.get("params", {}).get("mode"))
    await client.send_request(request)
    has_error = False
    while True:
        data = await client.recv()
        if data.get("type") != "event":
            continue
        event_type = data.get("event", "")
        payload = data.get("payload", {})
        if event_type == "chat.final":
            # team.error is wrapped in chat.final by the gateway; route it
            # through the error path so has_error is set and the JSON output
            # reports ok=false.
            if payload.get("event_type") == "team.error":
                renderer.handle_error(payload)
                has_error = True
            else:
                renderer.handle_event(event_type, payload)
        elif event_type == "chat.error":
            renderer.handle_error(payload)
            has_error = True
        elif event_type == "chat.delta":
            pass
        else:
            renderer.handle_event(event_type, payload)
        if needs_user_input(event_type) and not _request_supports_user_interaction(request):
            logger.error(
                "interactive input is unavailable for this chat invocation: %s",
                event_type,
            )
            renderer.output()
            return 4
        if is_terminal_event(event_type, payload):
            if team_mode and event_type == "chat.final":
                if payload.get("event_type") == "team.error":
                    renderer.output()
                    return 1
                # leader reply — team keeps working, keep listening
                continue
            renderer.output()
            return 1 if has_error else 0


async def _run_chat(
    args: argparse.Namespace,
    prompt: str,
    *,
    supports_user_interaction: bool = False,
) -> int:
    if getattr(args, "name", None) and not getattr(args, "gateway_url", None):
        from jiuwenswarm.dotenv_early import load_named_instance_env

        load_named_instance_env(args.name)
    gateway_url = args.gateway_url or _build_default_gateway_url()

    client = GatewayClient(gateway_url)

    try:
        await asyncio.wait_for(client.connect(), timeout=10.0)
    except asyncio.TimeoutError:
        _print_connection_hint(gateway_url, args)
        return 3
    except ConnectionError:
        _print_connection_hint(gateway_url, args)
        return 3
    except OSError:
        _print_connection_hint(gateway_url, args)
        return 3

    request = _build_request(
        args,
        prompt,
        supports_user_interaction=(
            supports_user_interaction and not args.json and not args.jsonl
        ),
    )

    # Persist explicitly-provided trusted dirs after building the request
    # so _build_request still sees the original trusted_dirs for the prompt.
    if getattr(args, "trusted_dir", None):
        await _persist_trusted_dirs(client, args.trusted_dir)
        args.trusted_dir = None  # prevent re-persistence on subsequent REPL turns

    try:
        if args.jsonl:
            renderer: JsonlRenderer | JsonRenderer | HumanRenderer = JsonlRenderer()
            return await _run_jsonl_loop(client, renderer, request)
        elif args.json:
            renderer = JsonRenderer()
            return await _run_json_loop(client, renderer, request)
        else:
            renderer = HumanRenderer(
                show_reasoning=args.show_reasoning,
                show_tools=args.show_tools,
            )
            return await _run_interactive_loop(
                client, renderer, request,
                timeout=args.timeout,
            )
    finally:
        await client.close()


def _print_connection_hint(url: str, args: argparse.Namespace) -> None:
    logger.error("Cannot connect to JiuwenSwarm Gateway at %s.", url)
    if args.name:
        logger.error("Start services with: jiuwenswarm-start app --name %s", args.name)
    else:
        logger.error("Start services with: jiuwenswarm-start app")


def run_chat(args: argparse.Namespace) -> int:
    error = _validate_args(args)
    if error is not None:
        return error

    from jiuwenswarm.common.mode_matrix import is_team_mode as is_team_runtime_mode
    is_team_mode = is_team_runtime_mode(args.mode)
    supports_user_interaction = False

    if args.prompt:
        prompt = " ".join(args.prompt)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    elif is_team_mode:
        # Team runtime is persistent across conversation turns. Use
        # the same interactive loop model (single connection, multiple
        # follow-ups) that _run_interactive_loop already supports when
        # stdin is a TTY — we just need to prime it with the first line
        # instead of bouncing through _run_repl (which creates a fresh
        # connection per turn and loses team state).
        logger.info("Team mode — interactive persistent session.")
        logger.info("Type your prompt and press Enter. Ctrl+C to interrupt, /exit to exit.")
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not prompt:
            return 0
        supports_user_interaction = True
    else:
        return _run_repl(args)

    if not prompt:
        logger.error("no prompt provided and stdin is empty")
        return 2

    # Ctrl+C during the synchronous startup phase (before _run_interactive_loop
    # installs its async SIGINT handler) would otherwise bubble up as a raw
    # KeyboardInterrupt traceback. Mirror the TUI's keymap.ts behavior: print
    # a friendly hint and exit with the conventional 128+SIGINT code instead.
    # The TUI uses a 3-second double-press-to-exit window, but that requires
    # an event loop already pumping input — the CLI sync phase has none, so a
    # single Ctrl+C exits cleanly. Once asyncio.run starts, the in-loop
    # handler takes over with its own double-press semantics.
    try:
        # Check for newly persisted dirs before sending the message,
        # so the user gets a chance to clean up from previous sessions.
        _prompt_and_cleanup_dirs()

        return asyncio.run(
            _run_chat(
                args,
                prompt,
                supports_user_interaction=supports_user_interaction,
            )
        )
    except KeyboardInterrupt:
        logger.warning("Interrupted. Exiting (press Ctrl+C again during chat to cancel a running task).")
        return 130


def _run_repl(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        logger.error("REPL mode requires interactive terminal")
        return 2

    error = _validate_args(args)
    if error is not None:
        return error

    session_id = args.session or _generate_session_id()
    args.session = session_id

    logger.info("Session: %s", session_id)
    if os.name == "nt":
        logger.info(
            "Type your prompt and press Enter.  Ctrl+C to interrupt, "
            "Ctrl+Z+Enter or /exit to exit."
        )
    else:
        logger.info(
            "Type your prompt and press Enter.  Ctrl+C to interrupt, "
            "Ctrl+D or /exit to exit."
        )

    exit_code = 0
    try:
        while True:
            _prompt_and_cleanup_dirs()
            try:
                line = input("> ")
            except EOFError:
                logger.info("<EOF>")
                break
            if not line.strip():
                continue
            # Cross-platform exit commands (Ctrl+D does not send EOF on Windows)
            if line.strip() in ("/exit", "/quit", "/q"):
                logger.info("<exit>")
                break
            exit_code = asyncio.run(
                _run_chat(
                    args,
                    line.strip(),
                    supports_user_interaction=True,
                )
            )
            if exit_code == 130:
                # Task was interrupted via Ctrl+C — cancel was already sent,
                # stay in the REPL for the next prompt instead of exiting.
                continue
            if exit_code != 0:
                logger.warning("[exit code: %s]", exit_code)
    except KeyboardInterrupt:
        logger.info("<KeyboardInterrupt>")
    return exit_code
