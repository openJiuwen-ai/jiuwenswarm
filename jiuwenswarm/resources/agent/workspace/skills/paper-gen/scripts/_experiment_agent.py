# -*- coding: utf-8 -*-
"""_experiment_agent.py — SDK 直连 experiment-agent（模块三执行体）。

## 为什么必须走 agent，不能只走确定性 CLI

模块三是 fail-closed 的：`run_all` 在 resolve_implementation 之后遇到
`approval_required` 就停下，而 `implementation-manifest.json` 的
`execution_approved` 只能由**两阶段独立审查**写入。审查是 LLM 判断（"实现是否忠实
于方法设计"、"执行结果是否可信"），确定性 CLI 永远走不到底——它只能停在审查点。
experiment-agent 模板的根 Agent 就是这个独立审查者。

## 集成方式：SDK 直连，不起 server、不过 marketplace

`create_deep_agent(...)` + `agent.load_agent_template(<模板目录>)`：
模板的 persona / 编排 skill / ExperimentControlTool / 4 个子 Agent 一次挂齐，
子 Agent 自动继承父 Agent 的 model。不需要 agent server，也不需要模板被"安装"。

## 三个必须守住的前提

1. **cwd 决定可信根**：`ExperimentControlTool._trusted_workspace_root()` 读的是
   `openjiuwen...cwd.get_cwd()`。request.json 里的 run_dir 是**相对路径**（模块三
   `_validate_relative_path` 禁止绝对路径），按 cwd 解析。所以这里既 `os.chdir`
   也写 CwdState 三层（project_root / original_cwd / cwd），并且必须和投影阶段
   算 run_dir 时用的 base_dir 是同一个目录，否则 run_dir 会指到别处。
2. **硬超时**：2026-09-08 planning 阶段出现过 LLM 调用静默挂死（CPU 0%、心跳无输出、
   12min+ 无进展）。agent 路径同样吃这个风险，而且它跑得更久、更难人工判断"是在
   想还是死了"。所以 invoke 一律包 `asyncio.wait_for`，超时后 `agent.abort()` 并按
   模块三已落盘的产物给部分结论——不假装成功，也不丢掉已有进展。
3. **沙箱**：`restrict_to_work_dir=True` 时 sandbox_root 默认取
   `[workspace, project_root]`，两者都在项目根内，够模块三读写 `out/...`，
   又不让 agent 漫游整机。shell allowlist 用框架默认（含 python/pip/git）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from _experiment_output import (
    collect_artifacts,
    describe_stage,
    interpret_payload,
    output_path,
    read_experiment_output,
)

log = logging.getLogger("paper_gen.experiment_agent")

#: experiment-agent 模板位置。仓库内副本优先（跟代码同源，改了立刻生效），
#: 其次找已安装的 built_in 副本。
_TEMPLATE_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "plugins" / "agent_templates" / "experiment-agent",
    Path(__file__).resolve().parents[3] / "plugins" / "agent_templates" / "built_in" / "experiment-agent",
    Path.home() / ".jiuwenswarm" / "agent" / "workspace" / "plugins"
    / "agent_templates" / "built_in" / "experiment-agent",
)

#: 模块三流程长（4 个子 Agent + 2 轮审查 + 门禁重试），15 轮远不够。
DEFAULT_MAX_ITERATIONS = 50

#: 默认硬超时 90min，可用 PAPER_GEN_STAGE3_AGENT_TIMEOUT（秒）覆盖。
DEFAULT_TIMEOUT_SECONDS = 5400

#: SDK task-loop **单轮**超时（``DeepAgentConfig.completion_timeout``，SDK 默认 600s）。
#: 600s 对模块三远远不够：一轮里下载 2.7GB 数据就超过 600s，而 SDK 的单轮超时**不是
#: 重试**——``task_loop_event_handler._cancel_timed_out_round()`` 会
#: ``coordinator.request_abort()``，整条 task loop 立即终止
#: （日志：``Task loop round timed out after 600 seconds.``）。数据/实现/执行/分析
#: 任一阶段撞上都会被整段砍掉。
#:
#: 为什么不在 config.yaml 里配：paper-gen 是 **SDK 直连**（``create_deep_agent`` +
#: ``load_agent_template``，不起 server、不进 team），只认调用时传的 kwargs——
#: ``react.completion_timeout`` 只有 server adapter 读（``interface_deep.py``），
#: ``agents.*.completion_timeout`` 只对 team 成员生效，而 agent 模板 manifest.json 里
#: 的 control 字段在 ``load_agent_template_package`` 里根本没有对应字段，静默忽略。
#:
#: 默认 None = **不限单轮**，整段预算统一交给外层硬超时
#: （PAPER_GEN_STAGE3_AGENT_TIMEOUT，默认 5400s）：只有一个时间预算，不会出现
#: "外层还没到点、内层先 abort"。需要限制单轮时长时设
#: PAPER_GEN_STAGE3_COMPLETION_TIMEOUT（秒，>0）。
DEFAULT_COMPLETION_TIMEOUT_SECONDS: float | None = None

#: 单轮超时的环境变量名。
COMPLETION_TIMEOUT_ENV = "PAPER_GEN_STAGE3_COMPLETION_TIMEOUT"

#: 心跳间隔：既是"还活着"的信号，也是 9-08 那种静默挂死的唯一可观测线索。
_HEARTBEAT_SECONDS = 60

_TOKEN_LINE_PATTERN = re.compile(r"tokens=\{input=(\d+), output=(\d+)\}")


class _TokenUsageLogHandler(logging.Handler):
    """从 SDK 的标准 LLM 完成日志收集用量。

    某些 SDK invoke 返回值不含 usage（尤其子 Agent 调用），但框架每个完成响应都
    记录 ``tokens={input=N, output=N}``。用一个仅在本次 invoke 存活的 handler
    收集它，避免 stage3 状态误报 0 token。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            match = _TOKEN_LINE_PATTERN.search(record.getMessage())
        except Exception:  # pragma: no cover - logging 不能反过来中断实验
            return
        if match is not None:
            prompt, completion = (int(value) for value in match.groups())
            self.records.append({"prompt_tokens": prompt, "completion_tokens": completion})

    def summary(self) -> dict[str, Any]:
        prompt = sum(item["prompt_tokens"] for item in self.records)
        completion = sum(item["completion_tokens"] for item in self.records)
        return {
            "total": {
                "request_count": len(self.records),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
            "source": "agent_sdk_logs",
            "records": list(self.records),
        }


def resolve_template_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        if not (explicit / "manifest.json").is_file():
            raise FileNotFoundError(f"experiment-agent 模板缺 manifest.json: {explicit}")
        return explicit.resolve()
    for candidate in _TEMPLATE_CANDIDATES:
        if (candidate / "manifest.json").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到 experiment-agent 模板，找过: "
        + "; ".join(str(c) for c in _TEMPLATE_CANDIDATES)
    )


def build_query(*, request_path: Path, manifest_path: Path, run_dir: Path) -> str:
    """组装任务描述。

    模板 `defaultInitInput` 只说"读取模块二的实验规划JSON和实现清单"——没有路径，
    headless 下 agent 无从下手。这里补足绝对路径 + 终态契约 + 禁止编造的约束。
    """
    return (
        "请执行模块三（实验）完整流程。输入已由模块二投影为模块三标准契约，路径如下：\n"
        f"- 实验规划契约（ExperimentModuleInput）：{request_path}\n"
        f"- 实现清单（implementation-manifest.json）：{manifest_path}\n"
        f"- 运行目录（execution_config.run_dir 解析结果）：{run_dir}\n"
        "\n要求：\n"
        "1. 先用 experiment_control 初始化流程，再按 agent-state.json 的 next_agent "
        "依次调用数据 / 实现 / 执行 / 分析四个子 Agent，每一步都以状态文件为准，不要跳步。\n"
        "2. 两阶段独立审查（实现审查、执行审查）由你本人完成，审查不通过就打回重做，"
        "不要为了推进流程放水。\n"
        "3. 严禁编造实验数值。数据拿不到、算力不够或契约本身有问题时，走模块三的 REPLAN "
        "通道把 blockers 写清楚（带 [data]/[compute]/[baseline]/[method]/[metric]/[schema]/"
        "[experiment]/[budget] 类别前缀——experiment_plan 自身字段的问题用 [experiment]，"
        "别用 [method]，后者会把整轮送去重跑方法设计），这是正确结果，不是失败。\n"
        f"4. 收尾必须让 {output_path(run_dir)} 落盘——这是模块四写作阶段唯一的输入。\n"
        "5. 不要询问用户，headless 环境下没有人能回答；需要决策时按上述规则自己判断并记录理由。\n"
    )


def _prepare_agent_inputs(
    workspace_root: Path, request_path: Path, manifest_path: Path,
    base_dir: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Materialize an agent-local copy of the strict input contract.

    ``Workspace(root_path=...)`` becomes ExperimentControlTool's trusted root.
    Module 3 consequently resolves a relative run_dir against *that* root, not
    paper-gen's process cwd.  Give the agent a self-contained view where both
    the request and manifest are inside the sandbox.  The local directory is
    also keyed by run_id, preventing a reused stage directory from mixing two
    planning revisions before synchronization.
    The caller mirrors its persisted run back to paper-gen's canonical stage3
    directory after execution.
    """
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    config = payload.get("execution_config")
    if not isinstance(config, dict):
        raise ValueError("ExperimentModuleInput 缺 execution_config，无法准备 agent workspace")
    payload = dict(payload)
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("ExperimentModuleInput 缺有效 run_id，无法隔离 agent run")
    local_run = workspace_root / "runs" / run_id
    trusted_base = (base_dir or workspace_root).resolve()
    try:
        # 校验：agent 沙箱必须落在 paper-gen 可信根内
        local_run.resolve().relative_to(trusted_base)
    except ValueError as exc:
        raise ValueError("agent workspace must be inside paper-gen trusted root") from exc
    # run_dir 必须相对 **agent workspace** 写。ExperimentControlTool 的可信根是
    # ``Workspace(root_path=workspace_root)``，模块三按它解析相对 run_dir；写成相对
    # paper-gen cwd 的形式会被再拼一层 workspace 前缀，产物落到
    # ``agent_workspace/<paper-gen 相对路径>``，paper-gen 的 _sync_agent_run 取不到。
    local_run_relative = local_run.resolve().relative_to(workspace_root.resolve()).as_posix()
    payload["execution_config"] = {**config, "run_dir": local_run_relative}
    local_request = workspace_root / "request.json"
    local_manifest = local_run / "implementation-manifest.json"
    local_run.mkdir(parents=True, exist_ok=True)
    local_request.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(manifest_path, local_manifest)
    return local_request, local_manifest, local_run


def _sync_agent_run(agent_run_dir: Path, canonical_run_dir: Path) -> None:
    """Copy only persisted Module-3 artifacts back to paper-gen's stage contract."""
    if not agent_run_dir.is_dir():
        return
    canonical_run_dir.mkdir(parents=True, exist_ok=True)
    for source in agent_run_dir.iterdir():
        target = canonical_run_dir / source.name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


async def _heartbeat(run_dir: Path, started: float) -> None:
    """每分钟报一次"还活着 + 跑到哪了"。

    9-08 的静默挂死之所以难诊断，就是因为当时没有任何周期性信号：既看不出是 LLM
    在等，也看不出模块三推进到哪一步。心跳同时打 elapsed 和 agent-state 阶段，
    卡住时能立刻区分"agent 在想"和"模块三根本没动"。
    """
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            log.info(
                "[stage3-agent] 已运行 %.0f min，%s",
                (time.monotonic() - started) / 60.0, describe_stage(run_dir),
            )
    except asyncio.CancelledError:
        pass


def _prepare_cwd(base_dir: Path) -> None:
    """把 cwd 三层状态钉到 base_dir。

    `get_cwd()` 读的是 ContextVar（`cwd` → `original_cwd` → `os.getcwd()`），
    工具链和 sandbox 默认根都从这里取。只 os.chdir 不写 ContextVar 在多数情况下
    也能work（fallback 到 os.getcwd），但一旦上游谁写过 CwdState 就会读到旧值，
    所以三层一起写死，别留歧义。
    """
    os.chdir(base_dir)
    try:
        from openjiuwen.core.sys_operation.cwd import (
            set_cwd, set_original_cwd, set_project_root,
        )
    except ImportError:  # 老版本 SDK：os.chdir 已经够用
        log.warning("[stage3-agent] 无 cwd 状态 API，仅依赖 os.chdir(%s)", base_dir)
        return
    set_project_root(str(base_dir))
    set_original_cwd(str(base_dir))
    set_cwd(str(base_dir))


def _create_model() -> Any:
    """建模型，并把"没配模型"这件事说清楚。

    `config.yaml` 里默认模型是 `${MODEL_NAME}` / `${API_BASE}` 这类占位符，值由
    环境变量注入（TUI / server 启动时加载 .env）。裸 shell 里跑就会拿到空串，
    框架只会抛一句 "missing model_name"，看不出是"没配"还是"配错"。
    """
    from jiuwenswarm.symphony.llm import LLMConfig

    try:
        return LLMConfig.from_default_model().create_model()
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} —— stage3 走 agent 需要可用的默认模型。"
            "config.yaml 的默认模型用 ${MODEL_NAME}/${API_BASE}/${MODEL_PROVIDER} 占位符，"
            "值来自环境变量（TUI/server 启动时读 .env）。裸 shell 联调请先导出这些变量，"
            "或用 PAPER_GEN_STAGE3_MODE=cli 走确定性 CLI（会停在审查点）。"
        ) from exc


def _reset_skill_cache(agent: Any) -> None:
    """清掉 SkillUseRail 在 hot-load 时缓存的降级 skill 描述。

    `load_agent_template` 发生在第一次 invoke 之前，而 rail 的 sys_operation 是随
    rail 注册（首次 invoke）才注入的。所以 hot-load 期间读 SKILL.md 会失败
    （`'NoneType' object has no attribute 'fs'`），描述降级成
    "Skill located in <dir>"。rail 的增量刷新以 SKILL.md 的 mtime 为键，
    命中缓存就不再重读——降级描述会一路带到整个 session，根 Agent 看不到编排 skill
    到底能干什么。清一次缓存，让首次 invoke 的刷新重新读出真描述。
    """
    try:
        from openjiuwen.harness.rails import SkillUseRail

        rails = agent.find_rails_by_type((SkillUseRail,))
    except Exception as exc:  # pragma: no cover — SDK 结构变化不该拖垮执行
        log.warning("[stage3-agent] 无法定位 SkillUseRail，跳过描述缓存重置: %s", exc)
        return
    for rail in rails:
        clear = getattr(rail, "clear_skills", None)
        if callable(clear):
            clear()
    log.debug("[stage3-agent] 已重置 %d 个 SkillUseRail 的描述缓存", len(rails))


def resolve_completion_timeout(raw: str | None = None) -> float | None:
    """解析 SDK 单轮超时（秒）。None = 不限单轮（见 DEFAULT_COMPLETION_TIMEOUT_SECONDS）。

    Args:
        raw: 已读到的环境变量原文；None 时读 COMPLETION_TIMEOUT_ENV。
            空串/纯空白视同未设（便于 ``VAR= cmd`` 这种临时清空写法）。

    非法值（非数字 / ≤0）记 warning 后回退默认，不抛——超时配置写错不该让整个
    stage3 起不来，宁可退回"不限单轮 + 外层硬超时"这个安全默认。
    """
    if raw is None:
        raw = os.environ.get(COMPLETION_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_COMPLETION_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except ValueError:
        log.warning(
            "[stage3-agent] %s=%r 不是数字，回退默认 %s",
            COMPLETION_TIMEOUT_ENV, raw, DEFAULT_COMPLETION_TIMEOUT_SECONDS,
        )
        return DEFAULT_COMPLETION_TIMEOUT_SECONDS
    if value <= 0:
        log.warning(
            "[stage3-agent] %s=%r 必须 > 0（要「不限单轮」请留空），回退默认 %s",
            COMPLETION_TIMEOUT_ENV, raw, DEFAULT_COMPLETION_TIMEOUT_SECONDS,
        )
        return DEFAULT_COMPLETION_TIMEOUT_SECONDS
    return value


async def run_experiment_agent(
    *,
    request_path: Path,
    manifest_path: Path,
    run_dir: Path,
    base_dir: Path,
    stage3_dir: Path,
    run_id: str | None = None,
    timeout_seconds: int | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    template_dir: Path | None = None,
) -> dict:
    """驱动 experiment-agent 跑完模块三，返回 `{"summary": {...}}`。

    Args:
        request_path: 投影层产的 ExperimentModuleInput（绝对路径）。
        manifest_path: scaffold 出来的 implementation-manifest.json（绝对路径）。
        run_dir: `base_dir / execution_config.run_dir` 的绝对形式。
        base_dir: 相对 run_dir 的解析基准 == agent 进程 cwd == 可信根。
        stage3_dir: stage3 输出目录，agent workspace 落在它下面。
    """
    timeout = timeout_seconds or int(
        os.environ.get("PAPER_GEN_STAGE3_AGENT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    )
    completion_timeout = resolve_completion_timeout()
    if completion_timeout is not None and completion_timeout >= timeout:
        # 内层 ≥ 外层 = 内层永不触发，是死配置。说出来，别让人以为单轮有上限。
        log.warning(
            "[stage3-agent] 单轮超时 %.0fs >= 外层硬超时 %ds：外层会先触发，单轮上限实际不生效",
            completion_timeout, timeout,
        )
    template = resolve_template_dir(template_dir)
    workspace_root = stage3_dir / "agent_workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    agent_request_path, agent_manifest_path, agent_run_dir = _prepare_agent_inputs(
        workspace_root, request_path, manifest_path, base_dir,
    )

    # 依赖在函数内导入：paper-gen 的确定性路径（投影、CLI 回退）不该被 SDK 拖累
    from openjiuwen.core.single_agent import AgentCard, create_agent_session
    from openjiuwen.harness import Workspace, create_deep_agent

    previous_cwd = Path.cwd()
    _prepare_cwd(base_dir)

    errors: list[str] = []
    warnings: list[str] = []
    timed_out = False
    started = time.monotonic()
    heartbeat: asyncio.Task[None] | None = None
    token_handler = _TokenUsageLogHandler()
    logging.getLogger().addHandler(token_handler)
    session = None
    agent = None
    invoke_result: Any = None

    try:
        model = _create_model()
        agent = create_deep_agent(
            model=model,
            card=AgentCard(
                id="paper-gen-stage3",
                name="paper-gen-stage3",
                description="paper-gen 模块三执行体（experiment-agent 模板）",
            ),
            workspace=Workspace(root_path=str(workspace_root), language="cn"),
            max_iterations=max_iterations,
            # SDK task-loop 单轮超时，必须在这里显式传：create_deep_agent 只认调用
            # 时的 kwargs（经 **config_kwargs → setattr 到 DeepAgentConfig），配置文件
            # 与 agent 模板都到不了这条路径。不传就是 SDK 默认 600s，撞上即
            # request_abort() 终止整条 task loop。详见 DEFAULT_COMPLETION_TIMEOUT_SECONDS。
            completion_timeout=completion_timeout,
            enable_task_loop=True,
            enable_task_planning=True,
            # 模板带一个编排 skill（experiment-orchestration），而 create_deep_agent 只在
            # `skills` 非空或 enable_skill_discovery=True 时才装 SkillUseRail。
            # 不开这个开关，load_agent_template 会直接抛
            # "No SkillUseRail registered on the agent; cannot bind a skill."
            # （子 Agent 不受影响：extension_binder 绑定 subagent 时会自动补 SubagentRail。）
            enable_skill_discovery=True,
        )
        load_record = await agent.load_agent_template(str(template))
        log.info("[stage3-agent] 已加载模板 %s（%s）", template.name, load_record)
        _reset_skill_cache(agent)

        session = create_agent_session(
            session_id=f"stage3-{run_id or 'run'}", card=agent.card,
        )
        query = build_query(
            request_path=agent_request_path, manifest_path=agent_manifest_path, run_dir=agent_run_dir,
        )
        log.info(
            "[stage3-agent] 开始执行（硬超时=%ds 单轮超时=%s max_iterations=%d cwd=%s）",
            timeout,
            "不限" if completion_timeout is None else f"{completion_timeout:g}s",
            max_iterations,
            base_dir,
        )
        heartbeat = asyncio.create_task(_heartbeat(agent_run_dir, started))

        try:
            invoke_result = await asyncio.wait_for(
                agent.invoke({"query": query}, session=session), timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            errors.append(
                f"experiment-agent 超过硬超时 {timeout}s 未收尾（{describe_stage(run_dir)}）；"
                "已 abort，结论按已落盘产物给出"
            )
            log.error("[stage3-agent] %s", errors[-1])
            await _abort(agent, session)
        else:
            output_text = (
                invoke_result.get("output") if isinstance(invoke_result, dict) else None
            ) or ""
            if output_text:
                log.info("[stage3-agent] agent 收尾说明: %s", str(output_text)[:600])
    except Exception as exc:  # SDK / 模型 / 模板任一环出错都在这里收口
        errors.append(f"experiment-agent 执行异常: {type(exc).__name__}: {exc}")
        log.exception("[stage3-agent] 执行异常")
    finally:
        logging.getLogger().removeHandler(token_handler)
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        os.chdir(previous_cwd)

    wall = time.monotonic() - started

    # experiment-agent 在按 run_id 隔离的 workspace 内执行；对外同步到请求记录的
    # content-addressed canonical run_dir。
    try:
        _sync_agent_run(agent_run_dir, run_dir)
    except OSError as exc:
        errors.append(f"同步 experiment-agent 产物失败: {exc}")
        log.exception("[stage3-agent] 同步 agent run 失败")

    # 终态一律以模块三自己落盘的 output 为准——agent 说完成了不算数
    payload, read_note = read_experiment_output(run_dir)
    if read_note:
        warnings.append(read_note)
    if payload is not None:
        status, payload_errors, payload_warnings = interpret_payload(payload)
        errors.extend(payload_errors)
        warnings.extend(payload_warnings)
        if timed_out and status == "complete":
            # 超时后又发现 output 是 PASS：说明它在超时窗口边缘刚写完，
            # 但审查/收尾可能没做全，降一级并把疑点留在 warnings 里
            status = "partial"
            warnings.append("超时 abort 后才读到 PASS 产物，已降级为 partial 待人工确认")
    else:
        status = "error"
        errors.append(
            f"模块三未产出 {output_path(run_dir)}（{describe_stage(run_dir)}）"
        )

    artifacts = collect_artifacts(run_dir)
    log_usage = token_handler.summary()
    llm_token_usage = log_usage if log_usage["total"]["request_count"] else _extract_token_usage(invoke_result)
    usage_total = llm_token_usage.get("total") if isinstance(llm_token_usage, dict) else {}
    if not isinstance(usage_total, dict) or not int(usage_total.get("request_count") or 0):
        llm_token_usage = {
            "total": {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "by_stage": {}, "by_operation": {}, "records": [],
            "measurement_status": "unavailable",
            "source": "experiment_agent_provider_usage_not_exposed",
            "note": "Experiment Agent ran, but its provider response did not expose token usage.",
        }
    else:
        llm_token_usage["measurement_status"] = "reported"
    return {"summary": {
        "status": status,
        "artifacts": artifacts,
        "errors": errors[:20],
        "warnings": warnings[:20],
        "wall_time_seconds": round(wall, 2),
        "timed_out": timed_out,
        "llm_token_usage": llm_token_usage,
    }}


def _extract_token_usage(value: Any) -> dict[str, Any]:
    """从 SDK 返回值递归汇总 usage；SDK 版本不同，字段可能位于不同层级。"""
    totals = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    seen: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, (dict, list, tuple)):
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
        if isinstance(node, dict):
            keys = set(node)
            if keys & {"prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"}:
                prompt = int(node.get("prompt_tokens") or node.get("input_tokens") or 0)
                completion = int(node.get("completion_tokens") or node.get("output_tokens") or 0)
                total = int(node.get("total_tokens") or prompt + completion)
                totals["request_count"] += 1
                totals["prompt_tokens"] += max(prompt, 0)
                totals["completion_tokens"] += max(completion, 0)
                totals["total_tokens"] += max(total, 0)
                return
            for child in node.values():
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return {"total": totals, "source": "agent_sdk_result"}


def _abort(agent: Any, session: Any) -> Any:
    """尽力 abort：老版本 SDK 可能没有这个方法，别让清理反过来吃掉超时结论。"""
    async def _noop() -> None:
        return None

    abort = getattr(agent, "abort", None)
    if abort is None:
        return _noop()

    async def _run() -> None:
        try:
            await abort(session=session)
        except Exception as exc:  # pragma: no cover — abort 失败不改变超时结论
            log.warning("[stage3-agent] abort 失败: %s", exc)

    return _run()


def main() -> int:
    """手动跑一次 stage3 agent（调试用）。

    先用 _experiment_bootstrap 产 request.json/manifest，再：
        uv run python .../paper-gen/scripts/_experiment_agent.py \
            --stage3-dir out/run1/stage3_experiment
    """
    import argparse

    parser = argparse.ArgumentParser(description="单跑 stage3 experiment-agent")
    parser.add_argument("--stage3-dir", required=True, type=Path)
    parser.add_argument("--base-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--template-dir", type=Path, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    base_dir = (args.base_dir or Path.cwd()).resolve()
    stage3_dir = args.stage3_dir.resolve()
    report_path = stage3_dir / "projection_report.json"
    if not report_path.is_file():
        print(f"缺 {report_path}——先跑 _experiment_bootstrap 产投影输入")
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "ready":
        print(f"投影未就绪（status={report.get('status')}），先修模块二产物")
        return 2

    result = asyncio.run(run_experiment_agent(
        request_path=Path(report["request_path"]),
        manifest_path=Path(report["manifest_path"]),
        run_dir=base_dir / report["run_dir"],
        base_dir=base_dir,
        stage3_dir=stage3_dir,
        run_id=report.get("run_id"),
        timeout_seconds=args.timeout,
        template_dir=args.template_dir,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["status"] in ("complete", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
