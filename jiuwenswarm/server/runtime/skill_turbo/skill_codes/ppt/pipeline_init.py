from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode

logger = logging.getLogger(__name__)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    BashResult as _BashResult,
    cli_path as _cli_path,
    combined_output as _combined_output,
    normalize_tool_text as _normalize_tool_text,
    parse_bash_payload as _parse_bash_payload,
    quote_path as _quote_path,
    run_bash as _run_bash,
)

_PLAYWRIGHT_INSTALL_TIMEOUT = 600

_NPM_INSTALL_MARKERS = (
    "npm 依赖未安装",
    "npm 依赖缺失",
    "node_modules 目录为空",
    "playwright 依赖缺失",
    "npm install",
)
_PLAYWRIGHT_INSTALL_MARKERS = (
    "Chromium 未安装",
    "Chromium Headless Shell 未安装",
    "chromium 未安装",
    "npx playwright install",
    "浏览器安装必须尝试",
)

_DEFAULT_SKILL_NAME = "pptx-craft"


class PipelineInitError(RuntimeError):
    """P0 流水线初始化失败。"""


def _resolve_pptx_root(inputs: dict[str, Any]) -> str:
    """解析 pptx_root，只使用已注册的新版外部 skill 目录。

    优先级：
    1. inputs["pptx_root"] — 显式指定（最高优先级）
    2. inputs["skill_root"] + inputs["skill_name"] — 外部 skill 目录拼接
    3. 找不到 → raise PipelineInitError
    """
    # 1. 显式指定 pptx_root
    pptx_root = inputs.get("pptx_root")
    if pptx_root:
        root = Path(str(pptx_root)).expanduser().resolve()
        if not root.is_dir():
            raise PipelineInitError(f"pptx_root 不存在: {root}")
        resolved = str(root)
        logger.info("[P0] pptx_root resolved (source=pptx_root): %s", resolved)
        return resolved

    # 2. skill_root + skill_name
    skill_root = inputs.get("skill_root")
    skill_name = inputs.get("skill_name") or _DEFAULT_SKILL_NAME
    if skill_root:
        skill_root_path = Path(str(skill_root)).expanduser().resolve()
        # skill_root 本身就是 skill 目录（目录名 == skill_name）
        if skill_root_path.name == skill_name and skill_root_path.is_dir():
            resolved = str(skill_root_path)
            logger.info("[P0] pptx_root resolved (source=skill_root_is_skill_dir): %s", resolved)
            return resolved
        # skill_root 是 skills 根目录，拼接 skill_name 子目录
        candidate = skill_root_path / skill_name
        if candidate.is_dir():
            resolved = str(candidate)
            logger.info("[P0] pptx_root resolved (source=skill_root+skill_name): %s", resolved)
            return resolved

    raise PipelineInitError(
        f"缺少 pptx_root 或 skill_root 配置，无法定位 {skill_name} 根目录。"
        f"请确保 JIUWENSWARM_SHARED_SKILLS_DIRS 环境变量指向包含 {skill_name} 子目录的路径。"
    )


_NPM_DEPS = {
    "commander": "^13.0.0",
    "express": "^5.2.1",
    "get-port": "^7.2.0",
    "playwright": "1.57.0",
    "jszip": "^3.10.1",
}

_PACKAGE_JSON_CONTENT = (
    '{"name":"pptx-craft-runtime","version":"2.0.0","private":true,"type":"module",'
    f'"bin":{{"pptx-craft-cli":"./packages/cli/dist/cli.js"}},'
    f'"dependencies":{json.dumps(_NPM_DEPS)}}}\n'
)


async def _ensure_package_json(node: PlanNode, pptx_root: str) -> None:
    pkg_path = Path(pptx_root) / "package.json"
    if pkg_path.is_file():
        return
    if not node.has_tool("write_file"):
        raise PipelineInitError("write_file 工具不可用，无法生成 package.json")
    await node.call_tool(
        "write_file",
        file_path=str(pkg_path),
        content=_PACKAGE_JSON_CONTENT,
    )
    logger.info("[P0.1] 已生成 package.json: %s", pkg_path)


def _node_modules_ready(pptx_root: str) -> bool:
    nm = Path(pptx_root) / "node_modules"
    if not nm.is_dir():
        return False
    for dep in _NPM_DEPS:
        if not (nm / dep).is_dir():
            return False
    return True


async def _bash(
    node: PlanNode,
    command: str,
    *,
    timeout_seconds: int = 300,
    required: bool = True,
    workdir: str | None = None,
) -> _BashResult:
    try:
        return await _run_bash(
            node,
            command,
            timeout_seconds=timeout_seconds,
            required=required,
            workdir=workdir,
        )
    except BashExecError as exc:
        raise PipelineInitError(str(exc)) from exc


def _needs_npm_install(check_output: str) -> bool:
    if "→ 安装: cd " in check_output and "npm install" in check_output:
        return True
    return any(marker in check_output for marker in _NPM_INSTALL_MARKERS)


def _needs_playwright_install(check_output: str) -> bool:
    if "→ 安装: npx playwright install chromium" in check_output:
        return True
    if "环境就绪" in check_output:
        return False
    return any(marker in check_output for marker in _PLAYWRIGHT_INSTALL_MARKERS)


def _parse_cli_path(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("[ERROR]"):
            continue
        candidate = Path(line).expanduser()
        if candidate.is_absolute() or candidate.parts:
            return str(candidate.resolve()) if candidate.exists() else str(candidate)
    match = re.search(r"([A-Za-z]:\\[^\s\"']+|/[^\s\"']+)", output)
    if match:
        return str(Path(match.group(1)).expanduser().resolve())
    raise PipelineInitError(f"无法从命令输出解析路径:\n{output}")


def _normalize_ppt_session_parent(path: Path) -> Path:
    """PPT 时间戳目录的父路径：智能体工作空间本身，不再套一层 workspace。

    智能体工作空间是 ``.../agent/jiuwenclaw_workspace``（历史布局为 ``.../agent/workspace``）。
    历史兜底 ``{cwd}/workspace`` 会在工作空间已叫 workspace 时变成
    ``.../agent/workspace/workspace``。
    """
    resolved = path.expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        resolved = resolved.absolute()
    agent_names = {"workspace", "jiuwenclaw_workspace"}
    if resolved.name.lower() == "workspace" and resolved.parent.name.lower() in agent_names:
        return resolved.parent
    if resolved.name.lower() == "projects" and resolved.parent.name.lower() in agent_names:
        return resolved.parent
    return resolved


def _default_ppt_session_parent() -> Path:
    """最后兜底：相对 cwd 的 workspace，经 normalize 避免套一层 workspace。

    不直接 import ``jiuwenswarm.common.utils``：builtin skill_code 校验只允许
    安全标准库与 ``skill_turbo`` 内部模块；上游通常已通过 ``workspace_base`` /
    ``effective_project_dir`` 传入正确目录，此处仅作无输入时的兜底。
    """
    return _normalize_ppt_session_parent(Path("workspace"))


def _resolve_explicit_output_dir(inputs: dict[str, Any]) -> str | None:
    """上游显式指定的最终产物目录（完整路径，不再追加时间戳）。"""
    explicit = inputs.get("output_dir")
    if not explicit:
        return None
    text = str(explicit).strip()
    if not text:
        return None
    return str(_normalize_ppt_session_parent(Path(text)))


def _resolve_timestamp_parent_dir(inputs: dict[str, Any]) -> str:
    """generate-timestamp-dir 的父目录。

    优先级：
    1. workspace_base（显式指定的 output 父目录，避免路径重建导致的不一致）
    2. effective_project_dir + user_id/chat_id 重建路径（与 interface.prepare_files_for_agent 一致）
    3. fallback 到 ./workspace
    """
    # 优先使用显式指定的 workspace_base（通常来自 SkillTurbo 工具传入的 output_dir）
    workspace_base = inputs.get("workspace_base") or inputs.get("workspace")
    if workspace_base:
        return str(_normalize_ppt_session_parent(Path(str(workspace_base))))

    # fallback：从 effective_project_dir 重建路径
    project_dir = inputs.get("effective_project_dir")
    if project_dir:
        session = str(inputs.get("conversation_id") or inputs.get("session_id") or "default")
        # user_id / chat_id 由上游从 metadata.routing 写入 inputs，此处不再二次兜底。
        user_id = str(inputs.get("user_id") or "")
        chat_id = str(inputs.get("chat_id") or "")

        # 去重：两者都空或相同时不重复拼两层，避免 files/{session}/{session}/output。
        if user_id and chat_id and user_id != chat_id:
            ns_path = Path(str(project_dir)) / "files" / user_id / chat_id / "output"
        else:
            ns = user_id or chat_id or session
            ns_path = Path(str(project_dir)) / "files" / ns / "output"
        return str(_normalize_ppt_session_parent(ns_path))
    return _resolve_workspace_base(inputs)


def _resolve_workspace_base(inputs: dict[str, Any]) -> str:
    workspace_base = inputs.get("workspace_base")
    if workspace_base:
        return str(_normalize_ppt_session_parent(Path(str(workspace_base))))
    workspace = inputs.get("workspace")
    if workspace:
        return str(_normalize_ppt_session_parent(Path(str(workspace))))
    return str(_default_ppt_session_parent())


class P01EnvDepsNode(PlanNode):
    """P0.1 — check-env、npm install、playwright install。

    预期输入（ctx / inputs）:
        必填（二选一）: pptx_root | skill_root + skill_name

    预期输出（写入同一 ctx）:
        pptx_root: str — pptx-craft 根目录绝对路径
        env_ok: bool — 环境检测与 npm 依赖就绪
        playwright_ready: bool — Chromium 是否可用（安装失败时为 False，不阻塞后续节点）
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p0_1_env_deps",
            instruction=(
                "## P0.1 环境依赖检测与安装\n"
                "\n"
                "### 节点职责\n"
                "检测 Node/npm/playwright 是否就绪，缺失时自动安装。\n"
                "禁止读取 cli.js 源码。\n"
                "\n"
                "### 前置条件\n"
                "- `bash` 工具可用\n"
                "- 必填（二选一）: `pptx_root` | `skill_root`\n"
                "\n"
                "### 输入\n"
                "- `pptx_root`（可选）: pptx-craft 根目录绝对路径\n"
                "- `skill_root`（可选）: 技能根目录（从中解析 pptx_root）\n"
                "\n"
                "### 输出\n"
                "- `pptx_root`: str — pptx-craft 根目录绝对路径（由 skill_root 或内置路径解析）\n"
                "- `env_ok`: bool=True — npm 依赖就绪（npm install 失败时应 raise 而非设 False）\n"
                "- `playwright_ready`: bool — Chromium 可用；安装失败时为 False，不阻塞\n"
                "\n"
                "### 执行流程\n"
                "1. 解析 pptx_root（skill_root → 内置路径兜底）\n"
                "2. node cli.js check-env 检测环境\n"
                "3. node_modules 缺失时 cd pptx_root && npm install（必须成功）\n"
                "4. Chromium 缺失时 npx playwright install chromium（超时/失败不阻塞）\n"
                "\n"
                "### 失败兜底\n"
                "- npm install 失败: raise PipelineInitError\n"
                "- playwright install 失败: playwright_ready=False，继续执行\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pptx_root = _resolve_pptx_root(inputs)
        inputs["pptx_root"] = pptx_root

        # skill_checksum_ok 兜底：Executor 正常注入时已有值，未注入时默认 True。
        inputs.setdefault("skill_checksum_ok", True)

        await _ensure_package_json(self, pptx_root)

        if not _node_modules_ready(pptx_root):
            logger.info("[P0.1] npm install 开始: %s", pptx_root)
            await _bash(
                self, "npm install",
                timeout_seconds=600, required=True, workdir=pptx_root,
            )
            logger.info("[P0.1] npm install 完成")

        check_cmd = _cli_path("check-env", pptx_root)
        check_result = await _bash(
            self, check_cmd, required=False, workdir=pptx_root,
        )
        check_output = _combined_output(check_result)

        inputs["env_ok"] = True

        if _needs_playwright_install(check_output):
            logger.info("[P0.1] playwright install chromium 开始")
            try:
                await _bash(
                    self, "npx playwright install chromium",
                    timeout_seconds=_PLAYWRIGHT_INSTALL_TIMEOUT,
                    required=True, workdir=pptx_root,
                )
                inputs["playwright_ready"] = True
                logger.info("[P0.1] playwright install 完成")
            except PipelineInitError:
                inputs["playwright_ready"] = False
                logger.warning("[P0.1] playwright install 失败，继续执行")
        else:
            inputs["playwright_ready"] = True

        return inputs

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {
            "node": self.plan_name,
            "status": "progress",
            "message": "正在检测 pptx-craft 环境依赖...",
        }
        result = await self._execute(inputs)
        yield {
            "node": self.plan_name,
            "status": "ok",
            "message": "pptx-craft 环境依赖检测完成",
            **result,
        }


class P02WorkspaceInitNode(PlanNode):
    """P0.2 — 解析 pptx_root、创建 output_dir / pages_dir、初始化会话变量。

    预期输入（ctx / inputs）:
        必填（二选一）: pptx_root | skill_root（通常由 P0.1 写入）
        可选: output_dir — 用户指定输出目录绝对路径；有则跳过 generate-timestamp-dir
        可选: workspace_base | workspace — 自动生成时间戳目录时的父路径（默认 ./workspace）

    预期输出（写入同一 ctx）:
        pptx_root: str
        output_dir: str — 会话产物根目录绝对路径
        pages_dir: str — HTML 页面目录绝对路径（{output_dir}/pages）
        session_dir: str — 等于 output_dir
        output_dir_user_specified: bool — 是否使用了上游传入的 output_dir
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p0_2_workspace_init",
            instruction=(
                "## P0.2 工作区初始化\n"
                "\n"
                "### 节点职责\n"
                "解析 pptx_root、创建 output_dir 与 pages_dir、初始化会话变量。\n"
                "\n"
                "### 前置条件\n"
                "- `bash` 工具可用\n"
                "- 必填（二选一）: `pptx_root` | `skill_root`（通常由 P0.1 已写入）\n"
                "\n"
                "### 输入\n"
                "- `pptx_root`（可选）: pptx-craft 根目录（P0.1 已写入时复用）\n"
                "- `skill_root`（可选）: 技能根目录\n"
                "- `output_dir`（可选）: 用户指定输出目录；有则跳过 generate-timestamp-dir\n"
                "- `workspace_base` | `workspace`（可选）: 自动生成时间戳目录时的父路径（默认 ./workspace）\n"
                "\n"
                "### 输出\n"
                "- `pptx_root`: str — pptx-craft 根目录绝对路径\n"
                "- `output_dir`: str — 会话产物根目录绝对路径（目录已物理创建且可写入）\n"
                "- `pages_dir`: str — HTML 页面目录绝对路径（`{output_dir}/pages`，已物理创建）\n"
                "- `session_dir`: str — 等于 output_dir\n"
                "- `output_dir_user_specified`: bool — 是否使用了上游传入的 output_dir\n"
                "\n"
                "### 执行流程\n"
                "1. 解析 pptx_root（复用 P0.1 或重新解析）\n"
                "2. 确定 output_dir（用户指定则沿用，否则 cli.js generate-timestamp-dir）\n"
                "3. cli.js ensure-output-dir 创建 pages 子目录\n"
                "4. 初始化 session_dir = output_dir, pages_dir = {output_dir}/pages\n"
                "\n"
                "### 失败兜底\n"
                "- output_dir 生成失败: raise PipelineInitError\n"
                "- pages_dir 创建失败: raise PipelineInitError\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pptx_root = _resolve_pptx_root(inputs)
        inputs["pptx_root"] = pptx_root

        output_dir = _resolve_explicit_output_dir(inputs)
        if output_dir:
            inputs["output_dir_user_specified"] = True
        else:
            workspace_base = _resolve_timestamp_parent_dir(inputs)
            gen_cmd = (
                f"{_cli_path('generate-timestamp-dir', pptx_root)} "
                f"{_quote_path(workspace_base)}"
            )
            gen_result = await _bash(
                self, gen_cmd, required=True, workdir=pptx_root,
            )
            output_dir = _parse_cli_path(_combined_output(gen_result))
            inputs["output_dir_user_specified"] = False

        output_path = Path(output_dir)
        output_dir = str(output_path.resolve())

        ensure_cmd = (
            f"{_cli_path('ensure-output-dir', pptx_root)} "
            f"{_quote_path(output_dir)}"
        )
        ensure_result = await _bash(
            self, ensure_cmd, required=True, workdir=pptx_root,
        )
        pages_dir = _parse_cli_path(_combined_output(ensure_result))

        inputs["output_dir"] = output_dir
        inputs["pages_dir"] = pages_dir
        inputs["session_dir"] = output_dir
        return inputs

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {
            "node": self.plan_name,
            "status": "progress",
            "message": "正在初始化 PPT 输出工作区...",
        }
        result = await self._execute(inputs)
        yield {
            "node": self.plan_name,
            "status": "ok",
            "message": "PPT 输出工作区初始化完成",
            **result,
        }


class PipelineInitNode(PlanNode):
    """P0 — 流水线启动 / 环境预置（P0.1 → P0.2）。

    预期输入（ctx / inputs）:
        必填（二选一）: pptx_root | skill_root
        可选: output_dir — 用户指定输出目录（由 SkillTurbo 入口结构化传入）
        可选: workspace_base | workspace — 未指定 output_dir 时的时间戳目录父路径

    预期输出（写入同一 ctx，为 P0.1 + P0.2 并集）:
        pptx_root, env_ok, playwright_ready,
        output_dir, pages_dir, session_dir, output_dir_user_specified
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p0_pipeline_init",
            instruction=(
                "## P0 流水线启动与环境预置\n"
                "\n"
                "### 节点职责\n"
                "串联 P0.1（环境依赖）与 P0.2（工作区初始化），为 P1-P10 准备运行环境与输出目录。\n"
                "不调 LLM，仅通过 cli.js 与 bash 完成。\n"
                "\n"
                "### 前置条件\n"
                "- `bash` 工具可用\n"
                "- Node.js >= 18 已安装（P0.1 会检测并 npm install）\n"
                "- 必填（二选一）: `pptx_root` | `skill_root`（P0.1 解析为 pptx_root）\n"
                "\n"
                "### 输入\n"
                "- `pptx_root`（可选）: pptx-craft 根目录绝对路径\n"
                "- `skill_root`（可选）: 技能根目录（P0.1 从中解析 pptx_root）\n"
                "- `output_dir`（可选）: 用户指定输出目录（有则跳过自动生成）\n"
                "- `workspace_base` | `workspace`（可选）: 自动生成时间戳目录时的父路径\n"
                "\n"
                "### 输出\n"
                "- `pptx_root`: str — pptx-craft 根目录绝对路径\n"
                "- `env_ok`: bool — Node/npm 依赖就绪（必须为 True）\n"
                "- `playwright_ready`: bool — Chromium 可用（False 不阻塞后续，但影响 P8 导出质量）\n"
                "- `output_dir`: str — 会话产物根目录绝对路径（目录已物理创建）\n"
                "- `pages_dir`: str — HTML 页面目录绝对路径（`{output_dir}/pages`，已物理创建）\n"
                "- `session_dir`: str — 等于 output_dir\n"
                "- `output_dir_user_specified`: bool — 用户是否指定了输出目录\n"
                "\n"
                "### 执行流程\n"
                "1. P0.1: 检测 Node/npm/playwright，缺失时安装\n"
                "2. P0.2: 解析 pptx_root → 生成/复用 output_dir → 创建 pages 子目录\n"
                "\n"
                "### 失败兜底\n"
                "- npm install 失败: raise PipelineInitError，不设 env_ok=False 继续\n"
                "- playwright install 超时/失败: playwright_ready=False，不阻塞\n"
                "- output_dir 创建失败: raise PipelineInitError\n"
            ),
            sub_plans=[
                P01EnvDepsNode(),
                P02WorkspaceInitNode(),
            ],
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        ctx = inputs
        await self.execute_subplan(self.sub_plans[0], ctx)
        await self.execute_subplan(self.sub_plans[1], ctx)
        return ctx

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        ctx = inputs
        for subplan in self.sub_plans:
            async for chunk in self.execute_subplan_stream(subplan, ctx):
                yield chunk
        yield {
            **ctx,
            "node": self.plan_name,
            "status": "ok",
            "message": "流水线启动与环境预置完成",
        }
