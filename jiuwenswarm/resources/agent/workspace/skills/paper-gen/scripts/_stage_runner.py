# -*- coding: utf-8 -*-
"""_stage_runner.py — paper-gen 端到端编排的 subprocess 协议层。

设计：参考 plan-supervisor call_planning_skill.py 模式，参数化 cli_module 让 3 stage
共用同一份 subprocess 逻辑。3 个 stage 都按 plan-supervisor 的 5 flag 协议调：
    --input-dir X --output-dir Y --status-file Y/status.json --progress-file Y/progress.json [extra]

回读：status.json 优先（新协议）；缺失时 fallback 到退出码 {0: complete, 1: failed}。

experiment 特殊：3 步串行（adapt-planning + scaffold-manifest + run）——走 _run_experiment_stage()。

M3 增项（writing 接入）：只加 _run_stage("writing", ...) 一行 + _STAGE_CONFIG["writing"] 增项。
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# 模块级 logger（main.py 已 setup_logging；这里只取实例）
log = logging.getLogger("paper_gen.stage_runner")

# 模块三终态解读（CLI 与 agent 两条路径共用）。本模块可能被当成 `scripts._stage_runner`
# 导入（scripts 目录不一定在 sys.path 上），沿用本文件既有的"扁平 import + 失败补 path"惯例。
try:
    import _experiment_output
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _experiment_output

PROGRESS_POLL_INTERVAL = 0.5     # 500ms
DEFAULT_TIMEOUT_SECONDS = 600
# 注：MVP 阶段不设 stage 级别超时——LLaMA 长 prompt / 联网 / 实验执行都可能远超 10 min。
# 想强杀请用户自己 Ctrl-C。保留 DEFAULT_TIMEOUT_SECONDS 是给未来兜底用。

# 子进程环境（强制 UTF-8 + 强制 unbuffered，让 stderr/stdout 实时 forward 到 paper-gen）
# - PYTHONIOENCODING=utf-8 / PYTHONUTF8=1: Windows terminal 默认 GBK 会让 LLM 输出 Unicode 字符崩
# - PYTHONUNBUFFERED=1: subprocess 的 stderr 连 PIPE 时是 block buffer（4KB 缓冲），
#   planning 4 sub-agent 各 30-60s 一次 LLM 调用，单行可能 100 字符，积几分钟才 flush 一次
#   → 体感"卡死"。强制 line buffer 之后每一行实时可见
_NO_PROXY = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
_CHILD_ENV = {**os.environ,
              "PYTHONIOENCODING": "utf-8",
              "PYTHONUTF8": "1",
              "PYTHONUNBUFFERED": "1",
              "NO_PROXY": _NO_PROXY,
              "no_proxy": _NO_PROXY}

# ── stage CLI 入口（绝对路径 / 相对路径 fallback）──

_SKILL_MAIN_REL = {
    "conception":  "skills/conception/scripts/main.py",
    "planning":    "skills/planning/scripts/main.py",
    "experiment":  "skills/experiment/scripts/main.py",
    "writing":     "skills/writing/scripts/main.py",
}

_STAGE_CONFIG: dict[str, dict[str, Any]] = {
    # MVP：不设 stage 级别 timeout。LLM 联网 + 15 篇 references + 反思 + 实验都要时间。
    "conception": {"default_status": "complete"},
    "planning":   {"default_status": "complete"},
    "experiment": {"default_status": "complete"},
    "writing":    {"default_status": "complete"},
}


def _resolve_skill_main(cli_module: str) -> Path:
    """定位 skill main.py（项目工作区优先，再走源码与 editable install）。

    与 plan-supervisor call_planning_skill.py:_resolve_planning_skill_main() 同策略。
    """
    rel = _SKILL_MAIN_REL[cli_module]
    data_dir = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if data_dir:
        workspace_candidate = Path(data_dir).resolve() / "agent" / "workspace" / rel
        if workspace_candidate.is_file():
            return workspace_candidate
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        cand = parent / rel
        if cand.is_file():
            return cand
    try:
        import jiuwenswarm as _pkg
        pkg_init = Path(_pkg.__file__).resolve()
        project_root = pkg_init.parent.parent
        cand = project_root / "jiuwenswarm" / "resources" / "agent" / "workspace" / rel
        if cand.is_file():
            return cand
    except (ImportError, OSError, ValueError):
        pass
    raise FileNotFoundError(
        f"cannot locate {rel} (walked up from {cwd}; "
        "and JIUWENSWARM_DATA_DIR/editable install fallbacks also failed)"
    )


# ── status.json 读取 + 退出码 fallback ──


def _read_status_or_fallback(status_file: Path, rc: int) -> dict:
    """status.json 存在 → 读；缺失 → 退出码 fallback。

    planning 退出码语义：0=complete, 1=error, 2=aborted（已弃用但兼容）。
    experiment 退出码：0=PASS, 1=error, 2=REPLAN, 其他=unknown。
    """
    from _status import read_status_file
    payload = read_status_file(status_file)
    if payload is not None:
        return payload
    fallback = {0: "complete", 2: "partial", 1: "error"}.get(rc, "error")
    return {
        "status": fallback,
        "artifacts": [],
        "errors": [f"status.json missing; rc={rc}"],
        "warnings": [],
        "wall_time_seconds": 0.0,
        "tier": 0,
        "method_rounds_used": 0,
    }


# ── 通用 stage runner（conception / planning 用）──


async def run_skill_subprocess(
    *,
    stage_name: str,
    cli_module: str,
    input_dir: Path,
    output_dir: Path,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> dict:
    """subprocess 跑一个 stage：通用 5 flag 协议 + 500ms mtime 轮询 + status.json 回读。

    Returns:
        {"summary": {"status": ..., "artifacts": [...], "errors": [...],
                     "wall_time_seconds": float, "warnings": [...], "stderr_tail": ...}}
    """
    log.info(f"  → run_skill_subprocess: stage={stage_name}, cli_module={cli_module}, "
             f"input_dir={input_dir}, output_dir={output_dir}, extra_args={extra_args}")
    timeout = timeout_seconds or _STAGE_CONFIG.get(stage_name, {}).get("timeout")  # None = 不超时
    main_path = _resolve_skill_main(cli_module)
    output_dir.mkdir(parents=True, exist_ok=True)
    status_file = output_dir / "status.json"
    progress_file = output_dir / "progress.json"

    cmd: list[str] = [
        sys.executable, str(main_path),
        "--input-dir", str(input_dir),
        "--output-dir", str(output_dir),
        "--status-file", str(status_file),
        "--progress-file", str(progress_file),
    ]
    if extra_args:
        cmd.extend(extra_args)
    # 注：--dry-run 是 planning 专属 flag，conception/experiment 不接受——MVP 不透传。
    # MVP 走真 LLM 端到端；mock mode 留给 M2 优化。

    return await _execute_subprocess(
        stage_name=stage_name,
        cmd=cmd,
        output_dir=output_dir,
        status_file=status_file,
        progress_file=progress_file,
        timeout=timeout,
    )


async def _execute_subprocess(
    *,
    stage_name: str,
    cmd: list[str],
    output_dir: Path,
    status_file: Path,
    progress_file: Path,
    timeout: int | None = None,
    status_value_map: dict[str, str] | None = None,
) -> dict:
    """subprocess 执行 + progress.json 轮询 + status.json 回读（与 status 二次映射）。

    与 run_skill_subprocess 关系：run_skill_subprocess 组装 5 flag cmd 后调本函数；
    run_writing_stage 组装 --module1/2/3 cmd 后也调本函数（status_value_map 走
    writing 的 success/partial/failed → paper-gen 的 complete/partial/error 映射）。

    Args:
        stage_name:        "conception" / "planning" / "experiment" / "writing"
        cmd:               完整 subprocess 命令（python + script + flags）
        output_dir:        stage 输出根目录
        status_file:       status.json 落盘路径
        progress_file:     progress.json 落盘路径
        timeout:           子进程超时秒数（None = 不超时）
        status_value_map:  可选 status 二次映射表（用于 writing 协议转换）

    Returns:
        {"summary": {"status": ..., "artifacts": [...], "errors": [...],
                     "wall_time_seconds": float, "warnings": [...], "stderr_tail": ...}}
    """
    log.info(f"  → _execute_subprocess: stage={stage_name}, cmd={cmd[:3]}...{cmd[-2:]}, "
             f"timeout={timeout}, status_value_map={status_value_map or 'identity'}")
    # 上次运行的终态会误导本次 watcher（例如刚启动就显示“写产物 error”）。
    # 删除的只是可重建的控制面文件；阶段产物与日志完整保留，用于断点和调试。
    for stale_control_file in (status_file, progress_file):
        try:
            stale_control_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(f"[{stage_name}] 无法清理旧控制文件 {stale_control_file}: {exc}")
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_CHILD_ENV,
            # 2026-09-08 latent bug 修复：stdout 改 DEVNULL 消除 PIPE 死锁。
            # 原模式 stdout=PIPE + 下方 await proc.stdout.read() 只在 proc.wait()
            # 完成后才 drain。Python subprocess pipe 默认 64KB buffer，子进程
            # 写满后阻塞在 write，父进程 await proc.wait() 也卡住，形成死锁。
            # planning skill 9.7 logger 迁移后所有 print 都改 logging（走 stderr
            # forward + *.log 落盘），stdout 实际为空，丢 DEVNULL 不丢任何信息。
            # stderr 仍 PIPE + _stream_forward 实时 forward 给 paper-gen logger。
            # 其他 3 处 spawn (line ~505/546/597) 用 proc.communicate() 不是死锁模式，
            # 不动。
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return {"summary": {
            "status": "error",
            "errors": [f"spawn 失败: {e}", f"cmd={cmd}"],
            "artifacts": [],
            "wall_time_seconds": round(time.monotonic() - t0, 2),
        }}

    # 500ms 轮询 progress.json：phase 变化时日志 + 30s heartbeat
    poll_task = asyncio.create_task(_watch_progress(
        stage_name=stage_name, progress_file=progress_file, proc=proc,
    ))
    # 实时 forward stderr 到 paper-gen stderr（带 stage 前缀）
    streamed_usage = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    stderr_task = asyncio.create_task(_stream_forward(
        proc.stderr, f"[{stage_name} stderr] ", usage_accumulator=streamed_usage,
    ))
    # 实时 tail 子进程落盘的 *.log 文件（即使 stderr 被 buffered 也能穿透）
    log_tail_task = asyncio.create_task(_tail_log_files(
        stage_name=stage_name, output_dir=output_dir, proc=proc,
    ))
    try:
        if timeout is not None:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        else:
            await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(stderr_task, timeout=2.0)
        except asyncio.TimeoutError:
            stderr_task.cancel()
        poll_task.cancel()
        log_tail_task.cancel()
        return {"summary": {
            "status": "error",
            "errors": [f"{stage_name} 子进程超时（>{timeout}s）", f"cmd={cmd}"],
            "artifacts": [],
            "wall_time_seconds": round(time.monotonic() - t0, 2),
            "stderr_tail": "(see forwarded stderr above)",
        }}
    try:
        await asyncio.wait_for(stderr_task, timeout=5.0)
    except asyncio.TimeoutError:
        stderr_task.cancel()
    poll_task.cancel()
    log_tail_task.cancel()
    # 2026-09-08 latent bug 修复：stdout 已改 DEVNULL，无需 drain。下面原本有
    # `await proc.stdout.read()` 反模式（只在 wait() 之后才读，PIPE 写满会死锁），
    # 删除整段。
    stderr_b = b""

    wall = time.monotonic() - t0
    stderr_text = _safe_decode(stderr_b)
    summary = _read_status_or_fallback(status_file, proc.returncode)
    summary["wall_time_seconds"] = round(wall, 2)
    declared_usage = ((summary.get("llm_token_usage") or {}).get("total") or {})
    # Native/agent based skills may log authoritative provider usage but omit it from
    # status.json.  Count only the compact `[LLM] <<< ... tokens={...}` line, never the
    # verbose llm_call_end JSON, so each request is counted once.
    if streamed_usage["request_count"] and not int(declared_usage.get("request_count") or 0):
        summary["llm_token_usage"] = {
            "total": dict(streamed_usage),
            "by_stage": {stage_name: dict(streamed_usage)},
            "by_operation": {},
            "records": [],
            "measurement_status": "reported",
            "source": "paper-gen streamed provider usage",
        }
    elif not isinstance((summary.get("llm_token_usage") or {}).get("total"), dict):
        # Never silently create a zero token total when the child omitted
        # provider telemetry.  The orchestrator needs to distinguish this from
        # a deterministic/dry-run stage that explicitly reports zero.
        summary["llm_token_usage"] = {
            "total": {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "by_stage": {}, "by_operation": {}, "records": [],
            "measurement_status": "unavailable",
            "source": "child_status_omitted_provider_usage",
        }
    # status 二次映射（writing 等用 success/partial/failed → complete/partial/error）
    if status_value_map and summary.get("status") in status_value_map:
        old = summary["status"]
        summary["status"] = status_value_map[old]
        log.info(f"  → status 二次映射: {old} → {summary['status']}")
    # 补全 artifacts（status.json 的 + 落盘的）
    on_disk = sorted(str(p) for p in output_dir.iterdir() if p.is_file()) if output_dir.is_dir() else []
    artifacts = list(summary.get("artifacts") or [])
    for p in on_disk:
        if p not in artifacts and not p.endswith(("status.json", "progress.json", ".log")):
            artifacts.append(p)
    summary["artifacts"] = artifacts
    if proc.returncode != 0 and summary.get("status") == "complete":
        summary["status"] = "error"
        summary.setdefault("errors", []).append(f"rc={proc.returncode} 但 status.json=complete")
    if stderr_text:
        summary.setdefault("warnings", []).append(f"stderr_tail: {_tail(stderr_text, 200)}")
    # Persist the reconciled wall/token telemetry so a later cache-only resume keeps
    # the real module cost instead of reverting to the child's incomplete status.
    try:
        _write_stage_status(output_dir, summary)
    except OSError as exc:
        log.warning("[%s] 无法回写聚合后的 stage telemetry: %s", stage_name, exc)
    log.info(f"  → _execute_subprocess 完成: status={summary['status']}, wall={summary['wall_time_seconds']}s, "
             f"artifacts={len(summary.get('artifacts', []))}, errors={len(summary.get('errors', []))}")
    return {"summary": summary}


async def _poll_progress_mtime(progress_file: Path) -> None:
    """兼容旧名——新逻辑在 _watch_progress。"""
    await _watch_progress(progress_file=progress_file, stage_name="?", proc=None)


async def _watch_progress(
    *, stage_name: str, progress_file: Path, proc: asyncio.subprocess.Process | None = None,
    heartbeat_seconds: float = 30.0,
) -> None:
    """轮询 progress.json：phase 变化时日志 + heartbeat 报存活。

    之前 _poll_progress_mtime 只看 mtime 不读内容——白浪费了 progress.json 的 5 阶段信号。
    改读全文：phase 跳变时打一行 [stage_name progress] phase 2/5: 实验规划 (elapsed=Xs)；
    同一 phase 内超过 30s 没 mtime 变化 → 打 heartbeat 表示 LLM 还在跑。
    """
    last_mtime: float | None = None
    last_phase: str | None = None
    last_status: str | None = None
    last_log_at = time.monotonic()
    try:
        from _progress import read_progress_file
    except ImportError:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _progress import read_progress_file

    while True:
        await asyncio.sleep(PROGRESS_POLL_INTERVAL)  # 500ms

        # proc 已死就退出
        if proc is not None and proc.returncode is not None:
            return

        now = time.monotonic()
        try:
            mtime = progress_file.stat().st_mtime
        except OSError:
            mtime = None

        # mtime 变化 → 读全文
        if mtime != last_mtime and mtime is not None:
            payload = read_progress_file(progress_file)
            if payload:
                phase = payload.get("current_phase", "?")
                idx = payload.get("index", 0)
                total = payload.get("total", 0)
                status = payload.get("status", "running")
                elapsed = payload.get("elapsed_seconds", 0.0)

                # phase 变化 → 打日志
                if phase != last_phase:
                    log.info(
                        f"[{stage_name} progress] phase {idx + 1}/{total}: {phase} "
                        f"(elapsed={elapsed:.1f}s, status={status})"
                    )
                    last_phase = phase
                    last_log_at = now
                # 终态跳转单独打一行（即使 phase 没变）。partial 代表有
                # 可审计产物但没有发布资格，不能和 error 混同。
                elif status != last_status and status in ("complete", "partial", "aborted", "error"):
                    log.info(
                        f"[{stage_name} progress] {phase} → status={status} "
                        f"(elapsed={elapsed:.1f}s)"
                    )
                    last_status = status
                    last_log_at = now
                else:
                    last_status = status
            last_mtime = mtime

        # heartbeat：超过 30s 没新 phase → 报存活
        if now - last_log_at > heartbeat_seconds:
            waited = now - last_log_at
            if last_phase is not None:
                log.info(
                    f"[{stage_name} progress] still working on '{last_phase}' "
                    f"(alive {waited:.0f}s, no phase change)"
                )
            else:
                log.info(
                    f"[{stage_name} progress] subprocess alive, no progress.json yet "
                    f"(waited {waited:.0f}s)"
                )
            last_log_at = now


_MAX_FORWARDED_STDERR_LINE_CHARS = 16_384


_LLM_USAGE_LINE = re.compile(
    r"\[LLM\]\s+<<<.*?tokens=\{input=(\d+),\s*output=(\d+)\}"
)


async def _stream_forward(
    stream: asyncio.StreamReader | None,
    prefix: str,
    *,
    usage_accumulator: dict[str, int] | None = None,
) -> None:
    """实时转发子进程 stderr，且始终排空管道。

    ``StreamReader.readline`` 受 asyncio 的 64 KiB 行长度限制。LLM tool
    回填 JSON 时很容易产生一行超过该上限的日志；若这里因
    ``LimitOverrunError`` 退出，子进程的 stderr 管道最终会写满，从而把整个
    stage 锁死。按块读取不受该限制。为了不让一条异常长日志淹没顶层日志，
    仅展示其前缀，但仍持续读取到换行符为止。
    """
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    discarding_long_line = False

    def forward(line: str) -> None:
        match = _LLM_USAGE_LINE.search(line)
        if match and usage_accumulator is not None:
            prompt_tokens = int(match.group(1))
            completion_tokens = int(match.group(2))
            usage_accumulator["request_count"] += 1
            usage_accumulator["prompt_tokens"] += prompt_tokens
            usage_accumulator["completion_tokens"] += completion_tokens
            usage_accumulator["total_tokens"] += prompt_tokens + completion_tokens
        if line:
            log.info(f"{prefix}{line}")

    try:
        while True:
            chunk = await stream.read(8_192)
            if not chunk:
                break
            pending += decoder.decode(chunk)

            while pending:
                if discarding_long_line:
                    newline = pending.find("\n")
                    if newline < 0:
                        pending = ""
                        break
                    pending = pending[newline + 1:]
                    discarding_long_line = False
                    continue
                newline = pending.find("\n")
                if newline >= 0:
                    forward(pending[:newline].rstrip("\r"))
                    pending = pending[newline + 1:]
                    continue
                if len(pending) > _MAX_FORWARDED_STDERR_LINE_CHARS:
                    forward(
                        pending[:_MAX_FORWARDED_STDERR_LINE_CHARS]
                        + " … [long stderr line truncated; stream continues draining]"
                    )
                    pending = ""
                    discarding_long_line = True
                break
        pending += decoder.decode(b"", final=True)
        if pending and not discarding_long_line:
            forward(pending.rstrip("\r"))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning(f"{prefix}[forward error: {e}]")


async def _tail_log_files(
    *, stage_name: str, output_dir: Path, proc: asyncio.subprocess.Process | None = None,
    poll_interval: float = 1.0,
) -> None:
    """Tail 子进程落盘的 ``*.log`` 文件 → paper-gen log（穿透被 buffered 的子进程日志）。

    2026-09-08 新增：之前子进程用 file handler 落 stage{1,2,3,4}_*/stage*_*.log，
    即使 stderr 走 line buffer 也会被 log_format 截断或 buffered。paper-gen 只 forward
    stderr 看不全。**本函数每个 .log 文件开个 1s 轮询**：mtime/size 变就 read 新字节
    + 按行 log 到 paper-gen。

    实现策略：
        - 每 1s 扫 output_dir/*.log（包含子目录如 stage4_writing/_input/）
        - 记录每个文件的 size；新行 = [old_size, new_size)
        - 进程退出（proc.returncode != None）→ 等 1 个 poll_interval 让残余 flush → 退出
    """
    # 每个文件上次读到的字节位置
    file_positions: dict[str, int] = {}
    # 每个文件第一次见到时全量还是增量（False = 第一次只记位置不打，True = 增量打）
    first_seen: dict[str, bool] = {}
    try:
        while True:
            await asyncio.sleep(poll_interval)
            # 进程退出 → 收尾
            if proc is not None and proc.returncode is not None:
                # 最后再 tail 一次残余内容
                await _tail_log_iter(
                    output_dir, file_positions, first_seen, stage_name,
                    log_all=True,
                )
                return
            await _tail_log_iter(
                output_dir, file_positions, first_seen, stage_name,
                log_all=False,
            )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning(f"[{stage_name} logfile] tail error: {e}")


async def _tail_log_iter(
    output_dir: Path,
    file_positions: dict[str, int],
    first_seen: dict[str, bool],
    stage_name: str,
    *,
    log_all: bool,
) -> None:
    """单次扫描 + 增量 log（_tail_log_files 的内部 helper）。"""
    if not output_dir.is_dir():
        return
    # 找所有 .log（包含子目录）—— stage4 的 writing 在 stage4_writing/_input/ 可能有内部 log
    for log_file in sorted(output_dir.rglob("*.log")):
        try:
            if not log_file.is_file():
                continue
            key = str(log_file)
            cur_size = log_file.stat().st_size
            if key not in file_positions:
                # 旧日志是调试资产，但不应在每次 resume 时整份重放到总日志。
                # 从当前 EOF 开始，只转发本轮新增内容。
                file_positions[key] = cur_size
                first_seen[key] = True
                log.info(
                    f"[{stage_name} logfile:{log_file.name}] === 已存在 {cur_size}B，"
                    f"从 EOF 开始 tail 新行 ==="
                )
                continue
            old_size = file_positions.get(key, 0)
            if cur_size <= old_size and not log_all:
                continue
            if cur_size == 0:
                file_positions[key] = 0
                first_seen[key] = True
                continue
            # 读 [old_size, cur_size) 字节
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(old_size)
                chunk = f.read(cur_size - old_size)
            file_positions[key] = cur_size
            # 计算相对路径作 prefix（短点好看）
            try:
                rel = log_file.relative_to(output_dir)
            except ValueError:
                rel = log_file.name
            for line in chunk.splitlines():
                line = line.rstrip("\r")
                if not line:
                    continue
                if not first_seen.get(key, False):
                    # 第一次见 → 标"前 N 行"边界，不全打
                    first_seen[key] = True
                    log.info(
                        f"[{stage_name} logfile:{rel}] === 已存在 {cur_size}B，"
                        f"开始 tail 新行 ==="
                    )
                else:
                    log.info(f"[{stage_name} logfile:{rel}] {line}")
        except OSError as e:
            log.debug(f"[{stage_name} logfile] tail {log_file} 失败: {e}")
        except Exception as e:
            log.warning(f"[{stage_name} logfile] tail {log_file} 异常: {e}")


# ── experiment 特殊：3 步串行 ──


# 注：run_adapt_planning / run_scaffold_manifest / _extract_replan_blockers 已于
# 2026-09-09 删除。它们走模块三 adapt-planning CLI，而 adapt fail-closed 要求
# implementation manifest 先存在 → manifest 要由已验证 request scaffold → request 只能
# 从 adapt 出来，三者互锁。现由 _experiment_bootstrap.write_stage3_inputs() 取代
# （直接产标准输入 + 复用同事的 scaffold_manifest()）。


async def run_experiment_run(
    *, request: Path, manifest: Path, output_dir: Path,
    run_dir: Path | None = None,
    allow_downloads: bool = True,
    max_download_gb: float = 20.0,
) -> dict:
    """experiment run：跑完整 pipeline。

    CLI: experiment main.py run --input X --manifest Y [--print-output]

    退出码语义（main.py）：0=PASS/PARTIAL、2=REPLAN、1=FAILED。**rc=2 不是崩溃**，
    是模块三结构化地把球踢回模块二，理由在 planning_feedback.blockers 里。

    产物位置：模块三把 outputs/ 写在 **run_dir** 下（不是 stage3 根）——
    run_dir 由 execution_config.run_dir 决定，默认 `<stage3_dir>/run`。
    """
    main_path = _resolve_skill_main("experiment")
    cmd = [
        sys.executable, str(main_path),
        "run",
        "--input", str(request),
        "--manifest", str(manifest),
        "--max-download-gb", str(max_download_gb),
        "--print-output",
    ]
    if allow_downloads:
        cmd.append("--allow-downloads")
    else:
        cmd.append("--no-downloads")

    run_dir = run_dir or (output_dir / "run")
    exp_output = _experiment_output.output_path(run_dir)

    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=_CHILD_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    wall = time.monotonic() - t0
    stderr_text = _safe_decode(stderr_b)
    stdout_text = _safe_decode(stdout_b)

    # 终态判定：优先读模块三自己写的 output（rc 只是粗粒度信号，rc=2 也有完整 payload）
    payload, read_note = _experiment_output.read_experiment_output(run_dir)
    if read_note:
        stderr_text += f"\n[{read_note}]"
    if payload is None:
        payload = _experiment_output.parse_json_object(stdout_text)

    errors: list[str] = []
    warnings: list[str] = []
    if payload is not None:
        inferred_status, errors, warnings = _experiment_output.interpret_payload(payload)
    else:
        inferred_status = "complete" if proc.returncode == 0 else "error"
        errors.append(
            f"experiment run rc={proc.returncode}，且拿不到 experiment-module-output.json"
            f"（找过 {exp_output}）"
        )
    if inferred_status == "error" and not errors:
        errors.append(f"experiment run rc={proc.returncode}")
    if stderr_text.strip():
        warnings.append(f"stderr_tail: {_tail(stderr_text, 300)}")

    return {"summary": {
        "status": inferred_status,
        "replan_requested": _experiment_output.requires_replan(payload),
        "planning_feedback": _experiment_output.planning_feedback(payload),
        "artifacts": _experiment_output.collect_artifacts(run_dir),
        "errors": errors,
        "warnings": warnings,
        "wall_time_seconds": round(wall, 2),
    }}


async def _run_experiment_stage(
    planning_dir: Path,
    output_dir: Path,
    *,
    base_dir: Path | None = None,
) -> dict:
    """experiment stage 编排：投影 stage2 产物 → 模块三严格输入 → 执行。

    2026-09-09 重写。原先走 adapt-planning → scaffold-manifest → run 三步串行，有死结：
    adapt fail-closed 要求 implementation manifest 先存在，而 manifest 要由已验证的
    request 来 scaffold，request 又只能从 adapt 出来。

    现在由 `_experiment_bootstrap.write_stage3_inputs()` 直接产标准 `ExperimentModuleInput`
    （模块三 SKILL.md 执行步骤 1 允许"标准输入直接校验"），并顺手 scaffold manifest，
    死结消失。投影层的每一次形状归一都记在 stage3/projection_report.json 里。

    base_dir 语义：模块三 `resolve_run_dir(run_dir, base_dir)` 把相对 run_dir 按 base_dir
    解析，而 CLI/SDK 都默认 base_dir=cwd。所以这里用 cwd 算相对 run_dir，
    **执行模块三时必须保持同一个 cwd**（C2 走 SDK 时要 os.chdir 到同一个根）。
    """
    planning_dir = planning_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # SDK/Agent 调用时进程 cwd 不一定等于 Jiuwen 项目根目录。允许调用方显式传入
    # 可信根目录，确保 request.json 内的相对 run_dir 与模块三解析到同一位置。
    base_dir = (base_dir or Path.cwd()).resolve()

    # 1) 投影：stage2 富字段产物 → 模块三严格 request.json + implementation-manifest.json
    t0 = time.monotonic()
    try:
        from _experiment_bootstrap import write_stage3_inputs

        projection = await asyncio.to_thread(
            write_stage3_inputs, planning_dir, output_dir, base_dir=base_dir,
        )
    except (OSError, ValueError, ImportError, FileNotFoundError) as exc:
        failed = {"summary": {
            "status": "error",
            "errors": [f"stage3 投影失败: {exc}"],
            "artifacts": [],
            "wall_time_seconds": round(time.monotonic() - t0, 2),
        }}
        _write_stage_status(output_dir, failed["summary"])
        return failed
    projection_wall = time.monotonic() - t0

    artifacts = [projection["request_path"], projection["report_path"]]
    for line in projection["warnings"]:
        log.info("  [projection] %s", line)

    if projection["status"] != "ready":
        # 模块二产物与模块三契约有无法确定性修复的漂移 —— 报出全部字段，别让它静默通过
        log.error(
            "stage3 投影不合规: %d 个错误（详见 %s）",
            len(projection["errors"]), projection["report_path"],
        )
        failed = {"summary": {
            "status": "error",
            "errors": [
                "stage2 产物无法投影为模块三合法输入（模块二需按下列字段重新生成）",
                *projection["errors"][:30],
            ],
            "warnings": projection["warnings"][:10],
            "artifacts": artifacts,
            "wall_time_seconds": round(projection_wall, 2),
        }}
        _write_stage_status(output_dir, failed["summary"])
        return failed

    log.info(
        "stage3 投影就绪: run_id=%s run_dir=%s（%d 项归一）",
        projection["run_id"], projection["run_dir"], len(projection["warnings"]),
    )

    # 2) 执行模块三。
    #
    # 默认走 agent（experiment-agent 模板）：模块三 run_all 在 resolve_implementation
    # 之后遇 approval_required 就停——`execution_approved` 只能由两阶段独立审查写入，
    # 而审查是 LLM 判断，确定性 CLI 永远走不到底（只会停在审查点 → partial）。
    #
    # PAPER_GEN_STAGE3_MODE=cli 可切回确定性 CLI：用于不想烧 token 的联调，
    # 代价是流程停在审查点。不做"agent 失败自动退 CLI"——那样会把 SDK 故障
    # 伪装成"卡在审查点"，把真问题藏起来。
    mode = os.environ.get("PAPER_GEN_STAGE3_MODE", "agent").strip().lower()
    run_dir = base_dir / projection["run_dir"]
    if mode == "cli":
        log.warning("stage3 走确定性 CLI（PAPER_GEN_STAGE3_MODE=cli），预期停在审查点")
        result = await run_experiment_run(
            request=Path(projection["request_path"]),
            manifest=Path(projection["manifest_path"]),
            output_dir=output_dir,
            run_dir=run_dir,
            allow_downloads=True,
        )
    else:
        from _experiment_agent import run_experiment_agent

        result = await run_experiment_agent(
            request_path=Path(projection["request_path"]),
            manifest_path=Path(projection["manifest_path"]),
            run_dir=run_dir,
            base_dir=base_dir,
            stage3_dir=output_dir,
            run_id=projection["run_id"],
        )
    summary = result["summary"]
    summary["artifacts"] = list(dict.fromkeys([*artifacts, *summary.get("artifacts", [])]))
    summary["warnings"] = [*projection["warnings"][:10], *summary.get("warnings", [])]
    summary["wall_time_seconds"] = round(
        projection_wall + summary.get("wall_time_seconds", 0.0), 2
    )
    _write_stage_status(output_dir, summary)
    return {"summary": summary}


def _write_stage_status(output_dir: Path, summary: dict) -> None:
    """模块三不是子进程协议入口，由 paper-gen 自己补齐可恢复的 status.json。"""
    path = output_dir / "status.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── helpers ──


def _tail(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else "..." + s[-n:]


def _safe_decode(b: bytes | None) -> str:
    """解子进程输出。

    Windows 上子进程按控制台代码页（中文机器是 cp936/GBK）写中文，用
    utf-8+errors="replace" 会把中文全变成 U+FFFD。所以先 utf-8 严格解，
    失败再试 gbk，最后才 errors="replace" 兜底。

    注：原实现 `b.decode("utf-8", errors="replace")` 配 latin-1 except 分支是
    死代码——errors="replace" 永不抛 UnicodeDecodeError。
    """
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


# ── 公开 alias（main_flow 用）──

async def run_conception_stage(input_dir: Path, output_dir: Path) -> dict:
    return await run_skill_subprocess(
        stage_name="conception", cli_module="conception",
        input_dir=input_dir, output_dir=output_dir,
    )


async def run_planning_stage(input_dir: Path, output_dir: Path, feedback_file: Path | None = None) -> dict:
    extra_args = ["--no-human-review"]  # agent 模式不人审
    if feedback_file is not None:
        extra_args.extend(["--feedback-file", str(feedback_file)])
    return await run_skill_subprocess(
        stage_name="planning", cli_module="planning",
        input_dir=input_dir, output_dir=output_dir,
        extra_args=extra_args,
    )


async def run_experiment_stage(
    planning_dir: Path,
    output_dir: Path,
    *,
    base_dir: Path | None = None,
) -> dict:
    return await _run_experiment_stage(
        planning_dir,
        output_dir,
        base_dir=base_dir,
    )


# ── writing stage：3 JSON 输入（m1/m2/m3）+ status 协议转换 ──

# writing 内部 status 值（main.py 写盘前的 success/partial/failed）→
# paper-gen 期望的 status 值（complete/partial/error/aborted）的映射。
# _VALID_STATUS 见 writing/scripts/_status.py:51-59。
_WRITING_STATUS_VALUE_MAP: dict[str, str] = {
    "success":           "complete",
    "partial":           "partial",
    "failed":            "error",
    "aborted":           "aborted",
    "preflight_failed":  "preflight_failed",
    "preflight_error":   "error",
    "load_inputs_failed": "load_inputs_failed",
    "complete":          "complete",   # main.py 写盘前已规范化的 status 透传
    "error":             "error",      # 同上
}


async def run_writing_stage(
    *,
    m1_path: Path,
    m2_path: Path,
    m3_path: Path,
    source_manifest: Path,
    output_dir: Path,
    dry_run: bool = False,
    timeout_seconds: int | None = None,
) -> dict:
    """Stage 4: writing 子进程调用（3 个独立 JSON 输入 + writing 自定义 status 映射）。

    与 run_skill_subprocess 的差异：
        - writing 接收 ``--module1/--module2/--module3`` 3 个独立 JSON 路径
          （不是 ``--input-dir``；writing 不读整个目录，是显式 3 文件）
        - writing 自己的 status 枚举（success/partial/failed）与 paper-gen 协议
          （complete/partial/error）不同，通过 status_value_map 二次映射
        - 默认加 ``--no-human-review``（paper-gen 端到端不需要人审）

    Args:
        m1_path:         module1 (Conception) JSON 绝对路径
        m2_path:         module2 (Planning)   JSON 绝对路径
        m3_path:         module3 (Execution)  JSON 绝对路径
        output_dir:      writing 产物根目录（paper.pdf/.tex/.json + sections/）
        timeout_seconds: 子进程超时秒数（None = 不超时，从 _STAGE_CONFIG 读）

    Returns:
        {"summary": {"status": ..., "artifacts": [...], "errors": [...],
                     "wall_time_seconds": float, "warnings": [...], ...}}
    """
    log.info(f"  → run_writing_stage: m1={m1_path}, m2={m2_path}, m3={m3_path}, manifest={source_manifest}, "
             f"output_dir={output_dir}, timeout={timeout_seconds}")
    output_dir.mkdir(parents=True, exist_ok=True)
    main_path = _resolve_skill_main("writing")
    status_file = output_dir / "status.json"
    progress_file = output_dir / "progress.json"
    timeout = timeout_seconds or _STAGE_CONFIG.get("writing", {}).get("timeout")

    cmd: list[str] = [
        sys.executable, str(main_path),
        "--module1", str(m1_path),
        "--module2", str(m2_path),
        "--module3", str(m3_path),
        "--source-manifest", str(source_manifest),
        "--output-dir", str(output_dir),
        "--status-file", str(status_file),
        "--progress-file", str(progress_file),
        "--no-human-review",   # paper-gen 端到端模式不人审
    ]
    if dry_run:
        cmd.append("--dry-run")

    return await _execute_subprocess(
        stage_name="writing",
        cmd=cmd,
        output_dir=output_dir,
        status_file=status_file,
        progress_file=progress_file,
        timeout=timeout,
        status_value_map=_WRITING_STATUS_VALUE_MAP,
    )
