# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``jiuwenswarm skill-train``: run ReflACT offline skill training with the
jiuwenswarm coding agent as the target.

Thin CLI over :func:`openjiuwen.agent_evolving.skill_train.run_offline_training`.
Each benchmark item is executed through ``jiuwenswarm chat --jsonl`` inside an
isolated workspace (skill + task + attachments), the event stream is turned
into compact trace steps, and the analyst / optimizer (a chat model configured
via ``API_KEY`` / ``API_BASE`` / ``OPTIMIZER_MODEL``) rewrites the skill.

The Gateway must be running first::

    jiuwenswarm-start app
    jiuwenswarm skill-train --env searchqa --data-root ./data
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

EXIT_BAD_ARGS = 2
EXIT_GATEWAY_UNREACHABLE = 3
EXIT_MISSING_DEPENDENCY = 4
EXIT_TRAIN_FAILED = 5

_DEFAULT_BACKEND = "jiuwenswarm_cli_exec"
_ENVS = ("searchqa", "docvqa", "officeqa")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jiuwenswarm skill-train",
        description="ReflACT offline skill training with jiuwenswarm as the target agent.",
    )
    p.add_argument("--env", choices=_ENVS, default=os.getenv("SKILL_TRAIN_ENV", "searchqa"),
                   help="Benchmark environment (default: searchqa).")
    p.add_argument("--backend", default=os.getenv("TARGET_BACKEND", _DEFAULT_BACKEND),
                   help="Target backend: jiuwenswarm_cli_exec (default) or openai_chat.")
    p.add_argument("--data-root", default="",
                   help="skill_train data root holding *_id_split dirs (SKILL_TRAIN_DATA_ROOT).")
    p.add_argument("--split-dir", default="", help="Explicit id-split directory for --env.")
    p.add_argument("--skill-init", default="", help="Initial SKILL markdown (default: env preset).")
    p.add_argument("--output-dir", default="", help="Training output directory (SKILL_TRAIN_OUTPUT).")
    # optimizer credentials (chat model)
    p.add_argument("--optimizer-model", default="", help="Analyst / optimizer chat model (OPTIMIZER_MODEL).")
    p.add_argument("--api-key", default="", help="Optimizer API key (API_KEY).")
    p.add_argument("--api-base", default="", help="Optimizer API base URL (API_BASE).")
    p.add_argument("--target-model", default="", help="Target chat model; only for --backend openai_chat.")
    # schedule
    p.add_argument("--num-epochs", type=int, default=0)
    p.add_argument("--train-size", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=0)
    p.add_argument("--limit", type=int, default=None, help="Cap items per train rollout (debug).")
    p.add_argument(
        "--selection-eval-size",
        type=int,
        default=None,
        help="Cap selection/gate eval on valid_seen (default 40; 0 = full val split).",
    )
    p.add_argument("--workers", type=int, default=0, help="Parallel jiuwenswarm chat processes.")
    p.add_argument("--exec-timeout", type=int, default=0, help="Seconds per jiuwenswarm chat run.")
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-gate", action="store_true", help="Disable the acceptance gate.")
    # jiuwenswarm target
    p.add_argument("--gateway-url", default="", help="Gateway WebSocket URL (default ws://127.0.0.1:19001/tui).")
    p.add_argument("--name", default="", help="Named jiuwenswarm instance (passed to `chat --name`).")
    p.add_argument("--mode", default="", help="`jiuwenswarm chat --mode` for the target (default code.normal).")
    p.add_argument("--cli-path", default="", help="jiuwenswarm executable (default: the one on PATH).")
    p.add_argument("--no-trace-to-optimizer", action="store_true",
                   help="Do not feed jiuwenswarm trace steps to the analyst.")
    p.add_argument("--skip-gateway-check", action="store_true",
                   help="Do not probe the Gateway before starting.")
    # eval-only
    p.add_argument("--eval-only", action="store_true", help="Evaluate --skill-init once, no optimization.")
    p.add_argument("--split", default="test", help="Split for --eval-only (default: test).")
    p.add_argument("--log-level", default=os.getenv("SKILL_TRAIN_LOG_LEVEL", "INFO"))
    return p


def _default_gateway_url() -> str:
    from jiuwenswarm.cli.chat import _build_default_gateway_url

    return _build_default_gateway_url()


async def _probe_gateway_async(url: str, timeout: float) -> None:
    from jiuwenswarm.cli.gateway_client import GatewayClient

    client = GatewayClient(url)
    await asyncio.wait_for(client.connect(), timeout=timeout)
    await client.close()


def check_gateway(url: str, *, timeout: float = 10.0) -> bool:
    """Return whether a Gateway answers with ``connection.ack`` at *url*."""
    try:
        asyncio.run(_probe_gateway_async(url, timeout))
    except OSError as exc:
        # TimeoutError / ConnectionError are OSError subclasses; catch only the parent.
        logger.error("Cannot connect to JiuwenSwarm Gateway at %s (%s).", url, exc or type(exc).__name__)
        return False
    return True


def _resolve_cli_path(explicit: str) -> str:
    if explicit:
        return explicit
    env = os.getenv("JIUWENSWARM_CLI_PATH", "").strip()
    if env:
        return env
    # Prefer the sibling console script of the current interpreter so the
    # subprocess shares this venv even when PATH differs.
    scripts_dir = Path(sys.executable).resolve().parent
    for candidate in ("jiuwenswarm.exe", "jiuwenswarm"):
        path = scripts_dir / candidate
        if path.exists():
            return str(path)
    return "jiuwenswarm"


def _build_options(args: argparse.Namespace):
    from openjiuwen.agent_evolving.skill_train import TrainLaunchOptions

    return TrainLaunchOptions(
        env_name=args.env,
        target_backend=args.backend,
        skill_init=args.skill_init,
        split_dir=args.split_dir,
        data_root=args.data_root,
        output_dir=args.output_dir,
        optimizer_model=args.optimizer_model,
        api_key=args.api_key,
        api_base=args.api_base,
        target_model=args.target_model,
        num_epochs=args.num_epochs,
        train_size=args.train_size,
        batch_size=args.batch_size,
        workers=args.workers,
        exec_timeout=args.exec_timeout,
        limit=args.limit,
        selection_eval_size=args.selection_eval_size,
        seed=args.seed,
        max_turns=args.max_turns,
        use_gate=False if args.no_gate else None,
        jiuwenswarm_cli_path=_resolve_cli_path(args.cli_path),
        jiuwenswarm_gateway_url=args.gateway_url,
        jiuwenswarm_chat_mode=args.mode,
        jiuwenswarm_instance_name=args.name,
        jiuwenswarm_trace_to_optimizer=not args.no_trace_to_optimizer,
    )


def _configure_logging(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    try:
        from openjiuwen.core.common.logging import llm_logger
        from openjiuwen.core.common.logging import logger as oj_logger

        fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        llm_logger.reconfigure({"level": "WARNING", "output": ["console"], "format": fmt})
        oj_logger.reconfigure({"level": level.upper(), "output": ["console"], "format": fmt})
    except Exception:
        logger.debug("openjiuwen logger reconfigure skipped", exc_info=True)


def run_skill_train(args: argparse.Namespace) -> int:
    try:
        from openjiuwen.agent_evolving.skill_train import (
            run_offline_eval,
            run_offline_training,
        )
    except ImportError as exc:
        logger.error("openjiuwen with agent_evolving.skill_train is required: %s", exc)
        logger.error("Install it into this environment, e.g. `uv pip install -e <agent-core checkout>`.")
        return EXIT_MISSING_DEPENDENCY

    _configure_logging(args.log_level)
    opts = _build_options(args)

    if opts.is_exec_backend and not args.skip_gateway_check:
        url = args.gateway_url or _default_gateway_url()
        if not check_gateway(url):
            hint = f" --name {args.name}" if args.name else ""
            logger.error("Start services with: jiuwenswarm-start app%s", hint)
            return EXIT_GATEWAY_UNREACHABLE
        logger.info("Gateway reachable at %s; target backend=%s", url, opts.resolved_backend())

    try:
        if args.eval_only:
            summary = run_offline_eval(opts, split=args.split)
            logger.info(
                "Eval complete. env=%s backend=%s n=%s hard=%.4f soft=%.4f output=%s",
                summary["env"],
                summary["target_backend"],
                summary["n_items"],
                summary["hard"],
                summary["soft"],
                summary["output_dir"],
            )
            return 0
        result = run_offline_training(opts)
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return EXIT_BAD_ARGS
    except KeyboardInterrupt:
        logger.warning("Interrupted. Partial results stay under the output directory.")
        return 130
    except Exception as exc:
        logger.error("skill-train failed: %s: %s", type(exc).__name__, exc)
        logger.debug("traceback", exc_info=True)
        return EXIT_TRAIN_FAILED

    logger.info(
        "Training complete. env=%s backend=%s best_score=%.4f output=%s",
        args.env,
        opts.resolved_backend(),
        result.best_score,
        result.output_dir,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_skill_train(args)


if __name__ == "__main__":
    sys.exit(main())
