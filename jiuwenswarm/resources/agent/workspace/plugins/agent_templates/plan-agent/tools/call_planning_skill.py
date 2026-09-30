"""CallPlanningSkillTool —— 把 planning-skill 当黑盒调。

输入 action + input_dir + output_dir + 可选 feedback_file / 透传参数
处理 subprocess 跑 ``uv run python -m scripts.main --input-dir X --output-dir Y
                       --status-file Y/status.json --progress-file Y/progress.json
                       [--feedback-file Z] [透传 flag]``

返回（最终 result chunk，等价于旧 invoke 返回值）：status.json 内容 + 退出码 / stderr 摘要。

设计要点：
    - 黑盒调：agent 看不到 skill 内部 4 sub-agent，agent 只看产物 + status.json
    - 复用 planning skill 的 _llm_backend / load_inputs / write_outputs / _subagent
      / iterate_method_design / plan_experiment_with_data / check_feasibility_and_downgrade
      —— **0 改动**这些子文件（agent 切换不影响 skill 内部）
    - 状态读取优先 status.json（新协议，Phase A4 引入）；若 status.json 不存在则
      fallback 到退出码映射（兼容旧 skill 行为）
    - REPLAN 反馈走 --feedback-file（替代 --replan-from，TODO §3.3）

Phase 4 改造（vs Phase A4）：
    - 新增 --progress-file：skill 端 ``phase()`` 调用通过真 ``progress_sink`` 写
      progress.json（5 阶段：方法设计 / 实验规划 / 门禁校验 / 执行配置 / 写产物）
    - ``stream()`` 改为真多 yield：
        * 轮询 progress.json mtime（500ms），变化时 yield ``{type: "phase", ...}`` chunk
        * 收尾时 yield 一次 ``{type: "result", ...status_payload}`` 终态 chunk
    - 框架 ``_ToolMeta`` 自动把每个 yield 包装为 ``chat.tool_update`` 事件，
      最终 yield → ``chat.tool_result``，前端 PhaseProgress 组件渲染任务列表
    - ``invoke()`` 退化为 ``stream()`` 单次 collect，返回最后一个 result chunk
      （保留对直接调用方 / 旧测试的向后兼容）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from openjiuwen.core.foundation.tool import Tool, ToolCard


# 注：直接调 sys.executable + main.py（experiment-agent 同模式），不通过 uv / PATH。
# 见 ``_resolve_planning_skill_main()``。


def _resolve_planning_skill_main() -> Path:
    """定位 planning-skill 的 main.py 入口（兼容源码 / 部署两种 layout）。

    策略 1：与 experiment-agent 的 ``_experiment_main()`` 同模式——
    从 ``__file__`` 向上 walk parent，找 ``skills/planning/scripts/main.py``。
    适用于源码路径（tool 与 skill 在同一 workspace 树）。

    策略 2：通过 ``jiuwenswarm`` 包路径反推项目源码根，定位
    ``<project>/jiuwenswarm/resources/agent/workspace/skills/planning/scripts/main.py``。
    适用于 user-workspace 部署路径（tool 在 ``built_in/`` 下，skill 在源码）。

    都不命中抛 FileNotFoundError。
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "skills" / "planning" / "scripts" / "main.py"
        if candidate.is_file():
            return candidate
    # 策略 2：jiuwenswarm editable install → project root
    try:
        import jiuwenswarm as _pkg
        pkg_init = Path(_pkg.__file__).resolve()
        project_root = pkg_init.parent.parent
        candidate = (
            project_root / "jiuwenswarm" / "resources" / "agent" / "workspace"
            / "skills" / "planning" / "scripts" / "main.py"
        )
        if candidate.is_file():
            return candidate
    except (ImportError, OSError, ValueError):
        pass
    raise FileNotFoundError(
        "cannot locate workspace/skills/planning/scripts/main.py"
        f" (walked up from {current})"
    )

# 默认不设子进程超时（None = 一直等到子进程自己退出）。
# 4 阶段 LLM 流程真实耗时远超当初估算的 1-3 分钟，600s 看门狗会误杀正常长跑；
# 只有用方显式传 timeout_seconds 才限时限。
DEFAULT_TIMEOUT_SECONDS: int | None = None

# progress.json 轮询间隔（秒）——500ms 是肉眼"实时"感 + 不浪费 CPU 的折中
PROGRESS_POLL_INTERVAL = 0.5

# 透传给 planning-skill 的 flag 白名单（与 main.py _parse_args 对齐）
_PASSTHROUGH_FLAGS = (
    "max_method_rounds",
    "no_human_review",
    "strict",
    "seeds",
    "max_retries",
    "timeout_seconds",
    "dry_run",
)


class CallPlanningSkillTool(Tool):
    """调 planning-skill（subprocess 黑盒）作为 plan-supervisor agent 的唯一执行工具。

    本工具不读 / 写产物 .json，只负责拉起子进程 + 读 status.json + 转发 progress.json 事件。
    产物校验、4 检查点人审、REPLAN 反馈聚合由 agent persona 负责。
    """

    AGENT_NAME = "plan-supervisor"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="call_planning_skill",
                name="call_planning_skill",
                description=(
                    "把 planning-skill 当黑盒当子进程跑。"
                    "输入 action='run' + input_dir（模块一产物）+ output_dir（落产物的目录）"
                    "+ 可选 feedback_file（REPLAN 反馈 JSON）。"
                    "输出 status.json 内容（含 status / artifacts / errors / check_feasibility / "
                    "method_rounds_used / wall_time_seconds 等）。"
                    "本工具不解析产物 .json、不做 4 检查点人审——那是 plan-supervisor agent 的事。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["run"],
                            "description": "操作类型：run=调 planning-skill 跑一次",
                        },
                        "input_dir": {
                            "type": "string",
                            "description": (
                                "模块一产物目录（含 7 个 JSON：research_question / hypotheses / "
                                "gap_report / key_papers / resource_constraints / domain / ...）"
                            ),
                        },
                        "output_dir": {
                            "type": "string",
                            "description": (
                                "规划产物输出目录（生成 5 .json + 5 .md + status.json + "
                                "check_feasibility_output.json）；不存在则创建"
                            ),
                        },
                        "feedback_file": {
                            "type": "string",
                            "description": (
                                "可选：REPLAN 反馈 JSON 文件路径（含 blockers / hints / "
                                "previous_artifacts_dir）。"
                                "对应 skill 端 --feedback-file flag（替代旧 --replan-from）。"
                            ),
                        },
                        "passthrough": {
                            "type": "object",
                            "description": (
                                "可选：透传给 planning-skill 的 CLI 参数 dict，"
                                "键：max_method_rounds / strict / seeds / max_retries / "
                                "timeout_seconds / dry_run / no_human_review。"
                                "agent 模式下不要传 no_human_review（人审归 agent）。"
                            ),
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "可选：子进程超时（秒）。默认不限时——一直等到 planning-skill "
                                "自己跑完。仅当显式传入本参数时才限时，超时后 subprocess 被 "
                                "kill，返回 timeout 状态。"
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": (
                                "可选：子进程工作目录（默认 = planning-skill 所在项目根，"
                                "即 jiuwenswarm/resources/agent/workspace/）"
                            ),
                        },
                    },
                    "required": ["action", "input_dir", "output_dir"],
                },
            )
        )

    # ──────────────────── invoke（向后兼容：collect stream → return last result）────────────────────

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        """单次调用入口——内部 collect stream 全部 chunk，只 return 最后一个 result chunk。

        设计：旧测试 / 框架 fallback 路径可能直接调 invoke()。为不破坏既有协议，
        这里让 invoke() 走 stream() 全程，最后一个 result chunk（含 status.json 完整
        字段）作为返回值；phase chunks 仅作 log / debug 用。
        """
        last_result: dict[str, Any] = {"status": "error", "errors": ["stream() produced no result chunk"]}
        async for chunk in self.stream(inputs, **kwargs):
            if isinstance(chunk, dict) and chunk.get("type") == "result":
                last_result = chunk.get("payload", last_result)
        return last_result

    # ──────────────────── stream（真多 yield，Phase 4 核心）────────────────────

    async def stream(self, inputs: dict[str, Any], **kwargs) -> AsyncIterator[dict[str, Any]]:
        """调 subprocess 跑 planning-skill，**逐 chunk yield**。

        Yields:
            - ``{"type": "phase", "phase": {...}}`` — progress.json 内容变化时
            - ``{"type": "result", "payload": {...status.json 完整字段...}}`` — 子进程结束（最后一个）
        """
        try:
            action = inputs.get("action", "")
            if action != "run":
                yield {"type": "result",
                       "payload": {"success": False, "status": "error",
                                   "errors": [f"未知 action: {action!r}（仅支持 'run'）"]}}
                return

            input_dir = inputs.get("input_dir", "")
            output_dir = inputs.get("output_dir", "")
            feedback_file = inputs.get("feedback_file")
            passthrough = inputs.get("passthrough") or {}
            raw_timeout = inputs.get("timeout_seconds")
            timeout = int(raw_timeout) if raw_timeout else None
            cwd = inputs.get("cwd")

            # ── 路径校验 ──
            in_path = Path(input_dir).resolve()
            if not in_path.is_dir():
                yield {"type": "result",
                       "payload": {"success": False, "status": "error",
                                   "errors": [f"input_dir 不存在或不是目录: {in_path}"]}}
                return
            out_path = Path(output_dir).resolve()
            out_path.mkdir(parents=True, exist_ok=True)

            # ── 拼 CLI 命令（与 experiment-agent 的 _base_command 同模式）──
            try:
                skill_main = _resolve_planning_skill_main()
            except FileNotFoundError as e:
                yield {"type": "result",
                       "payload": {"success": False, "status": "error",
                                   "errors": [str(e),
                                              f"__file__={__file__}"]}}
                return
            cmd = [sys.executable, str(skill_main),
                   "--input-dir", str(in_path),
                   "--output-dir", str(out_path)]
            status_file = out_path / "status.json"
            cmd += ["--status-file", str(status_file)]
            progress_file = out_path / "progress.json"
            cmd += ["--progress-file", str(progress_file)]
            if feedback_file:
                fb_path = Path(feedback_file).resolve()
                if not fb_path.is_file():
                    yield {"type": "result",
                           "payload": {"success": False, "status": "error",
                                       "errors": [f"feedback_file 不存在: {fb_path}"]}}
                    return
                cmd += ["--feedback-file", str(fb_path)]
            for key in _PASSTHROUGH_FLAGS:
                if key not in passthrough:
                    continue
                val = passthrough[key]
                flag = f"--{key.replace('_', '-')}"
                if isinstance(val, bool):
                    if val:
                        cmd.append(flag)
                elif isinstance(val, list):
                    cmd += [flag] + [str(v) for v in val]
                else:
                    cmd += [flag, str(val)]

            # ── 工作目录（与 experiment-agent 的 _trusted_workspace_root 同模式）──
            # 默认 = skill 根目录（scripts/ 的父），与原 `python -m scripts.main` 行为一致。
            if cwd is None:
                cwd = str(skill_main.parent.parent)
            cwd_path = Path(cwd).resolve()
            if not (cwd_path / "scripts" / "main.py").is_file():
                yield {"type": "result",
                       "payload": {"success": False, "status": "error",
                                   "errors": [f"cwd 不是有效的 planning-skill 包根: {cwd_path}"]}}
                return

            # ── 跑子进程（async stream）──
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            t0 = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(cwd_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except FileNotFoundError as e:
                yield {"type": "result",
                       "payload": {"success": False, "status": "spawn_failed",
                                   "exit_code": 127,
                                   "errors": [f"subprocess 启动失败: {e}",
                                              f"cmd={cmd}"],
                                   "feedback_file": str(feedback_file) if feedback_file else None,
                                   "status_file": str(status_file),
                                   "wall_time_seconds": round(time.monotonic() - t0, 2)}}
                return

            # ── 并发：poll progress.json + 等 proc 完成 ──
            poll_task = asyncio.create_task(
                _poll_progress(progress_file, poll_interval=PROGRESS_POLL_INTERVAL)
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stderr_b = b""
                try:
                    stderr_b = await proc.stderr.read() if proc.stderr else b""
                except Exception:
                    pass
                poll_task.cancel()
                yield {"type": "result",
                       "payload": {"success": False, "status": "timeout",
                                   "exit_code": 124,
                                   "wall_time_seconds": round(time.monotonic() - t0, 2),
                                   "errors": [f"planning-skill 子进程超时（>{timeout}s）",
                                              f"cmd={cmd}",
                                              f"stderr_tail={_tail(_safe_decode(stderr_b), 1000)}"],
                                   "feedback_file": str(feedback_file) if feedback_file else None,
                                   "status_file": str(status_file)}}
                return

            wall_time = time.monotonic() - t0
            # 取最后一个 poll chunk（终态 progress）
            try:
                last_poll = await poll_task
            except (asyncio.CancelledError, Exception):
                last_poll = None
            if last_poll is not None:
                yield {"type": "phase", "phase": last_poll}

            # ── 收尾 result chunk ──
            stderr_text = _safe_decode(stderr_b)
            status_payload = _read_status_file(status_file)
            if status_payload is not None:
                payload = dict(status_payload)
                payload.setdefault("exit_code", proc.returncode)
                payload.setdefault("wall_time_seconds", round(wall_time, 2))
                payload.setdefault("feedback_file", str(feedback_file) if feedback_file else None)
                payload.setdefault("status_file", str(status_file))
                payload.setdefault("progress_file", str(progress_file))
                payload.setdefault("stderr_tail", _tail(stderr_text, 2000))
                yield {"type": "result", "payload": payload}
            else:
                # 旧协议 fallback：按退出码推 status
                proc_obj = _FakeCompletedProcess(proc.returncode, stderr_text)
                payload = _fallback_by_exit_code(
                    proc_obj, out_path, wall_time, feedback_file,
                    status_file, progress_file,
                )
                yield {"type": "result", "payload": payload}

        except Exception as e:  # noqa: BLE001
            import traceback as _tb
            yield {"type": "result",
                   "payload": {"success": False, "status": "spawn_failed",
                               "exit_code": 1,
                               "errors": [f"CallPlanningSkillTool.stream 异常: {e}",
                                          f"traceback={_tb.format_exc()}"]}}


# ──────────────────── helpers ────────────────────


async def _poll_progress(progress_file: Path, poll_interval: float) -> dict[str, Any] | None:
    """轮询 progress.json mtime，变化时 read + 返最后内容。

    Returns:
        最后一次成功 read 的 progress.json 内容 dict；从未 read 到返 None。
    """
    last_mtime: float | None = None
    last_payload: dict[str, Any] | None = None
    try:
        # 先读一次（已经存在的初始 progress.json，例如 REPLAN 场景）
        if progress_file.is_file():
            try:
                last_mtime = progress_file.stat().st_mtime
                last_payload = json.loads(progress_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        # 轮询循环
        while True:
            await asyncio.sleep(poll_interval)
            try:
                if not progress_file.is_file():
                    continue
                mtime = progress_file.stat().st_mtime
                if last_mtime is not None and mtime <= last_mtime:
                    continue  # mtime 未变（写盘失败或 no-op）
                last_mtime = mtime
                payload = json.loads(progress_file.read_text(encoding="utf-8"))
                last_payload = payload
            except (OSError, json.JSONDecodeError):
                # 写盘中途读到半截——跳过，等下一轮
                continue
    except asyncio.CancelledError:
        # 被外层取消（proc 结束时 await poll_task 会触发）
        pass
    return last_payload


def _read_status_file(status_file: Path) -> dict[str, Any] | None:
    """读 status.json（planning-skill 写的新协议）。"""
    if not status_file.is_file() or status_file.stat().st_size == 0:
        return None
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CallPlanningSkillTool] status.json 解析失败: {e}", file=sys.stderr)
        return None


def _fallback_by_exit_code(
    proc: "_FakeCompletedProcess",
    out_path: Path,
    wall_time: float,
    feedback_file: str | None,
    status_file: Path,
    progress_file: Path | None = None,
) -> dict[str, Any]:
    """status.json 缺失时按退出码推 status（旧协议 fallback）。"""
    artifacts = _list_artifacts(out_path)
    base: dict[str, Any] = {
        "exit_code": proc.returncode,
        "wall_time_seconds": round(wall_time, 2),
        "artifacts": artifacts,
        "stderr_tail": _tail(proc.stderr, 2000),
        "feedback_file": str(feedback_file) if feedback_file else None,
        "status_file": str(status_file),
    }
    if progress_file is not None:
        base["progress_file"] = str(progress_file)
    if proc.returncode == 0:
        required = ("method_design.json", "experiment_plan.json", "data_plan.json",
                    "execution_config.json")
        if all((out_path / r).is_file() for r in required):
            base.update({"success": True, "status": "complete",
                         "errors": [], "warnings": [],
                         "check_feasibility": {"passed": True, "downgraded_to": None,
                                                "issues": []}})
        else:
            missing = [r for r in required if not (out_path / r).is_file()]
            base.update({"success": False, "status": "error",
                         "errors": [f"退出码 0 但产物缺失: {missing}"]})
        return base
    return {
        **base,
        "success": False,
        "status": "error",
        "errors": [f"planning-skill 退出码 {proc.returncode}",
                   f"stderr_tail={_tail(proc.stderr, 500)}"],
        "warnings": [],
        "check_feasibility": {"passed": False, "downgraded_to": None, "issues": []},
    }


class _FakeCompletedProcess:
    """``_fallback_by_exit_code`` 用的最小 CompletedProcess 替身。"""
    __slots__ = ("returncode", "stderr")

    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def _list_artifacts(out_path: Path) -> list[str]:
    if not out_path.is_dir():
        return []
    return sorted(
        str(p) for p in out_path.iterdir()
        if p.is_file() and p.suffix in (".json", ".md") and p.stem != "status"
    )


def _tail(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else "..." + s[-n:]


def _safe_decode(b: bytes | None) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        return b.decode("latin-1", errors="replace")
