# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboEnvironment -- 灵魂、工具、模型、技能配置环境。"""

from __future__ import annotations

import hashlib
import logging

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.runner import Runner

from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidator

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.skill_turbo.tools_loader import ToolLoaderContext

logger = logging.getLogger(__name__)

# 当 config 未显式指定时，使用本仓库内置 skill_codes 目录。
_DEFAULT_SKILL_CODES_DIR = str((Path(__file__).resolve().parent / "skill_codes"))
_DEFAULT_SKILLS_DIR = str((Path(__file__).resolve().parent / "skills"))

# skill_code 文件系统目录 -> import 包前缀（用于 Validator/Executor 白名单）。
# 注意：plan_code 里只允许 ``from {prefix}xxx import root`` 形式的 import。
_DEFAULT_SKILL_CODE_IMPORT_PACKAGE = (
    "jiuwenswarm.server.runtime.skill_turbo.skill_codes"
)

# [TEMP-EXTERNAL-SKILL] MD校时排除的目录/文件模式
_CHECKSUM_EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git"}
_CHECKSUM_EXCLUDE_FILES = {".gitkeep"}


def _compute_dir_checksum(dir_path: str) -> str:
    """[TEMP-EXTERNAL-SKILL] 计算目录的确定性 SHA256。

    递归遍历目录所有文件（排除 node_modules/__pycache__/.git/.gitkeep），
    按相对路径排序，对每个文件内容算 SHA256，
    拼接所有 ``relative_path:sha256`` 后对整体再算一次 SHA256。

    此函数位于框架层（environment.py），不受 skill_code 安全校验约束，
    可以自由使用 hashlib、rglob、read_bytes 等被沙箱禁止的能力。
    """
    root = Path(dir_path).resolve()
    entries: list[str] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        # 排除特定目录和文件
        if any(part in _CHECKSUM_EXCLUDE_DIRS for part in file_path.relative_to(root).parts):
            continue
        if file_path.name in _CHECKSUM_EXCLUDE_FILES:
            continue
        rel = str(file_path.relative_to(root))
        content_sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
        entries.append(f"{rel}:{content_sha256}")
    combined = "\n".join(entries)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _verify_skill_checksum(pptx_root: str, expected_checksum: str) -> bool:
    """[TEMP-EXTERNAL-SKILL] 校验外部 skill 目录的 SHA256。

    expected_checksum 为空时跳过校验（返回 True）。
    校验失败时打 WARNING 日志但不阻塞执行（临时方案）。

    此函数位于框架层（environment.py），不受 skill_code 安全校验约束。
    """
    if not expected_checksum:
        logger.info("[SkillTurboEnvironment] skill_checksum 为空，跳过 SHA256 校验")
        return True

    actual = _compute_dir_checksum(pptx_root)
    if actual == expected_checksum:
        logger.info("[SkillTurboEnvironment] skill_checksum 校验通过: %s", actual)
        return True

    logger.warning(
        "[SkillTurboEnvironment] skill_checksum 校验失败！期望=%s 实际=%s pptx_root=%s",
        expected_checksum, actual, pptx_root,
    )
    return False


@dataclass
class Skill:
    """技能定义 -- 包含描述和预规划的 plan_code。"""

    name: str
    description: str
    skill_md: str
    plan_code: str | None = None
    match_keywords: list[str] = field(default_factory=list)

    # match() 方法已移除：实际路由走 planner 的 LLM 通道，
    # match_keywords 字段保留，仅作为 LLM 路由 payload 的描述输入。


class SkillTurboEnvironment:
    """Agent 环境 -- 灵魂、工具、模型、技能。

    职责：
    - 给 Planner 提供 ``skills`` / ``get_skill``。
    - 给 Executor 提供 ``tools`` / ``get_tool_function`` / ``model_client`` /
      ``skill_code_import_prefixes`` / ``skill_codes_parent_dir``。
    - 提供 ``register_skill`` 让上层、未来的扫描器和测试统一注册 skill。
    """

    # 类级 skill 扫描缓存：key=skill_codes_dir 绝对路径，
    # value=(目录内所有 .py 的最大 mtime, 注册的 Skill 列表)
    # 用于跨请求复用扫盘 + AST 校验结果，仅在文件 mtime 变化时重新扫描。
    _scan_cache: ClassVar[dict[str, tuple[float, list["Skill"]]]] = {}

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._soul: str = config.get("soul", "")
        self._tools: dict[str, Any] = {}
        self._tools_loaded: bool = False
        self._model_client: Model | None = None
        self._skills: dict[str, Skill] = {}

        # fallback_handler: 由 DeepAdapter 注入，替代 Executor 自建 ReActAgent。
        # 类型为 SkillTurboFallbackHandler | None，延迟 import 避免循环依赖。
        self._fallback_handler: Any = config.get("fallback_handler")

        # agent card: executor 创建 session 时需要它来初始化 checkpointer。
        # 没有 card 时 session.pre_run/post_run 会崩溃，导致 HITL resume_ctx 无法持久化。
        self._card: Any = config.get("card")

        # skills/skill_codes 路径默认兜底到包内目录，避免 Executor 无法 import。
        self._skills_dir: str = config.get("skills_dir") or _DEFAULT_SKILLS_DIR
        self._skill_codes_dir: str = (
            config.get("skill_codes_dir") or _DEFAULT_SKILL_CODES_DIR
        )
        # skill_code 的 Python 包前缀，独立于文件系统路径，由配置或默认值决定。
        self._skill_code_import_package: str = (
            config.get("skill_code_import_package")
            or _DEFAULT_SKILL_CODE_IMPORT_PACKAGE
        )
        # skill_root: 技能根目录，用于 skill_code 定位外部资源（如 pptx-craft）。
        # [TEMP-EXTERNAL-SKILL] 统一走 JIUWENSWARM_SHARED_SKILLS_DIRS 标准链路，
        # 不再保留 SKILL_ROOT 环境变量和 config.skill_root 两级优先级。
        # 解析优先级：skills_dir -> resolve_agent_registered_skill_dirs()
        self._skill_root: str = self._resolve_skill_root()
        # [TEMP-EXTERNAL-SKILL] skill_name: PPT skill 的外部目录名（默认 pptx-craft）。
        self._skill_name: str = config.get("skill_name") or "pptx-craft"
        # [TEMP-EXTERNAL-SKILL] skill_checksum: 外部 skill 目录的 SHA256 校验值（转测前手动填写）。
        self._skill_checksum: str = config.get("skill_checksum") or ""
        # [TEMP-EXTERNAL-SKILL] skill_checksum_ok: SHA256 校验结果，在 _scan_skills_dir 中计算。
        self._skill_checksum_ok: bool = False
        # skill_code 静态安全校验器，使用 builtin_skill_code profile：
        # 允许安全标准库和 skill_turbo 内部模块 import，禁止 os/subprocess 等危险模块，
        # 禁止 getattr/eval/open 等危险调用，禁止 Path 文件 IO 和 dunder 属性访问。
        # 未来 LLM 动态生成的 skill_code 应使用 for_generated_skill_code() 更严格规则。
        self._skill_code_validator = PlanCodeValidator.for_builtin_skill_code(
            allowed_import_prefixes=self.skill_code_import_prefixes
        )
        # 严格模式：True 时内置 skill_code 校验失败直接抛 ValueError 阻止启动；
        # False（默认）时仅打 WARNING 日志并跳过该 skill 注册，不影响其他 skill。
        # 过渡期默认关闭，待所有内置 skill 整改通过后可在 config 中开启。
        self._strict_builtin_skill_validation: bool = bool(
            config.get("strict_builtin_skill_validation", False)
        )
        self._load(config)

    def _resolve_skill_root(self) -> str:
        """解析 skill_root 路径。

        skill_root 是技能根目录，用于 skill_code 定位外部资源（如 pptx-craft）。
        每个 skill（如 pptx-craft/）是一个包含 SKILL.md + 代码的完整目录。

        [TEMP-EXTERNAL-SKILL] 统一走 JIUWENSWARM_SHARED_SKILLS_DIRS 标准链路。
        不再保留 config["skill_root"] 和 SKILL_ROOT 环境变量两级优先级。

        解析优先级：
        1. skills_dir - 如果 skills_dir 存在且是有效目录
        2. DeepAgent 的 skills 目录 - 通过 resolve_agent_registered_skill_dirs() 获取
           （SkillTurbo 由 interface_deep.py 调用，此时 DeepAgent 已初始化，
            其 skills 目录通过 JIUWENSWARM_SHARED_SKILLS_DIRS 环境变量由 OfficeClaw 注入）
        """
        # 1. 使用 skills_dir（如果存在且是有效目录）
        if self._skills_dir:
            skills_path = Path(self._skills_dir)
            if skills_path.is_dir():
                logger.info(
                    "[SkillTurboEnvironment] skill_root from skills_dir: %s",
                    self._skills_dir,
                )
                return str(skills_path.resolve())

        # 2. 使用 DeepAgent 的 skills 目录（运行时由 jiuwenswarm 管理）
        try:
            from jiuwenswarm.common.utils import resolve_agent_registered_skill_dirs

            skill_dirs = resolve_agent_registered_skill_dirs()
            if skill_dirs:
                first_dir = str(skill_dirs[0].resolve())
                logger.info(
                    "[SkillTurboEnvironment] skill_root from DeepAgent skills_dir: %s",
                    first_dir,
                )
                return first_dir
        except Exception as exc:
            logger.debug(
                "[SkillTurboEnvironment] resolve_agent_registered_skill_dirs unavailable: %s",
                exc,
            )

        logger.info("[SkillTurboEnvironment] skill_root not resolved")
        return ""

    def _resolve_sys_operation(self) -> Any:
        """解析 sys_operation。

        sys_operation 是 openjiuwen 提供的系统操作抽象层，bash/filesystem
        等工具依赖它。DeepAgent 在初始化时已将 sys_operation 注册到全局
        Runner.resource_mgr 中，SkillTurbo 可直接获取，无需 interface_deep.py
        显式传入。

        解析优先级：
        1. config["sys_operation"] - 显式配置（最高优先级）
        2. Runner.resource_mgr - 从全局资源管理器获取 DeepAgent 已注册的实例
        """
        # 1. 显式配置
        config_value = self._config.get("sys_operation")
        if config_value is not None:
            return config_value

        # 2. 从全局资源管理器获取（DeepAgent 已注册）
        try:
            from openjiuwen.core.runner import Runner as _Runner

            sys_ops = _Runner.resource_mgr.get_sys_operation()
            if sys_ops is not None:
                # get_sys_operation() 无参数时可能返回列表或单个实例
                if isinstance(sys_ops, list):
                    if sys_ops:
                        logger.info(
                            "[SkillTurboEnvironment] sys_operation from Runner.resource_mgr: %s",
                            sys_ops[0],
                        )
                        return sys_ops[0]
                else:
                    logger.info(
                        "[SkillTurboEnvironment] sys_operation from Runner.resource_mgr: %s",
                        sys_ops,
                    )
                    return sys_ops
        except Exception as exc:
            logger.debug(
                "[SkillTurboEnvironment] sys_operation from Runner.resource_mgr unavailable: %s",
                exc,
            )

        logger.info("[SkillTurboEnvironment] sys_operation not resolved")
        return None

    # ────────────────────── 只读属性 ──────────────────────

    @property
    def soul(self) -> str:
        return self._soul

    @property
    def model_client(self) -> Model | None:
        return self._model_client

    @property
    def config(self) -> dict[str, Any]:
        """对外暴露原始配置 dict（含 ``permissions``），供 PermissionInterruptRail 使用。"""
        return self._config

    @property
    def tools(self) -> dict[str, Any]:
        return self._tools

    @property
    def fallback_handler(self) -> Any:
        """节点级 fallback 委托 handler，由 DeepAdapter 注入。"""
        return self._fallback_handler

    @property
    def card(self) -> Any:
        """agent card，executor 创建 session 时用于初始化 checkpointer。"""
        return self._card

    @property
    def skills(self) -> dict[str, Skill]:
        return self._skills

    @property
    def skills_dir(self) -> str:
        return self._skills_dir

    @property
    def skill_root(self) -> str:
        """技能根目录，用于 skill_code 定位外部资源（如 pptx-craft）。"""
        return self._skill_root

    @property
    def skill_name(self) -> str:
        """[TEMP-EXTERNAL-SKILL] PPT skill 的外部目录名。"""
        return self._skill_name

    @property
    def skill_checksum(self) -> str:
        """[TEMP-EXTERNAL-SKILL] 外部 skill 目录的 SHA256 校验值。"""
        return self._skill_checksum

    @property
    def skill_checksum_ok(self) -> bool:
        """[TEMP-EXTERNAL-SKILL] SHA256 校验是否通过（空值时为 True）。"""
        return self._skill_checksum_ok

    @property
    def skill_codes_dir(self) -> str:
        return self._skill_codes_dir

    @property
    def skill_code_import_package(self) -> str:
        return self._skill_code_import_package

    @property
    def skill_code_import_prefixes(self) -> list[str]:
        """Validator 用：允许的 import 包前缀（基于包名，非文件系统路径）。"""
        package = self._skill_code_import_package.strip().strip(".")
        if not package:
            return []
        return [f"{package}."]

    @property
    def skill_codes_parent_dir(self) -> str:
        """skill_codes 包的父目录，用于 sys.path 注入。"""
        if not self._skill_codes_dir:
            return ""
        return str(Path(self._skill_codes_dir).resolve().parent)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    # ────────────────────── 工具相关 ──────────────────────

    def get_tool_function(
        self, tool_name: str
    ) -> Callable[..., Awaitable[Any]] | None:
        """获取工具的可调用函数。

        统一返回签名：``async def fn(**kwargs) -> Any``，调用方只关心 kwargs。

        匹配优先级（兼容多种工具协议）：
        1. ``tool.invoke(inputs: dict, **kwargs)``  ← openjiuwen ``Tool`` 标准协议
        2. ``tool.run(**kwargs)``                   ← 老式 callable 工具
        """
        tool_card = self._tools.get(tool_name)
        tool_id = getattr(tool_card, "id", None)
        if not tool_id:
            return None
        # 注意：这里按全局 tool_id 从共享的 Runner.resource_mgr 现取实例。
        # 与 deep agent 不同--deep 每个 session 持有独立 adapter/ability_manager 实例，
        # 工具实例天然按 session 隔离；而 skill_turbo 各 session 注册的工具用同一个固定 tool_id，
        # 后注册者会覆盖前者，并发请求下这里必然取到"最后注册"的那个实例。
        # 因此对 send_file_to_user 这类携带 session 路由信息的工具，绝不能依赖其实例字段，
        # 必须在工具执行时从请求级 ContextVar 解析 session（见 send_file_to_user._resolve_route）。
        tool = Runner.resource_mgr.get_tool(tool_id)
        if tool is None:
            return None

        invoke_fn = getattr(tool, "invoke", None)
        if callable(invoke_fn):
            async def _invoke_adapter(**kwargs: Any) -> Any:
                # Tool 协议：invoke 第一参数是 inputs dict
                return await invoke_fn(kwargs)

            return _invoke_adapter

        run_fn = getattr(tool, "run", None)
        if callable(run_fn):
            return run_fn

        logger.warning(
            "[SkillTurboEnvironment] tool=%s has no invoke/run callable", tool_name
        )
        return None

    def get_tool_info_list(self) -> list[dict[str, Any]]:
        """获取工具描述列表（供 Planner prompt 使用）。"""
        return [
            {
                "name": name,
                "description": getattr(card, "description", ""),
            }
            for name, card in self._tools.items()
        ]

    # ────────────────────── Skill 注册/查询 ──────────────────────

    def register_skill(self, skill: Skill) -> None:
        """注册或覆盖一个 skill。统一入口，便于扫描器、测试、上层注入。"""
        if not skill or not skill.name:
            logger.warning("[SkillTurboEnvironment] register_skill ignored: invalid skill")
            return
        if skill.name in self._skills:
            logger.info(
                "[SkillTurboEnvironment] register_skill overwrite skill=%s", skill.name
            )
        self._skills[skill.name] = skill

    def has_skill(self, name: str) -> bool:
        return name in self._skills

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def reload(self) -> None:
        """重新加载 skills：清空并重新执行加载流程（硬编码 + 目录扫描）。"""
        logger.info("[SkillTurboEnvironment] reload skills")
        self._skills.clear()
        self._load(self._config)

    # ────────────────────── Tool 注册/查询 ──────────────────────

    def register_tool(self, tool_card: Any) -> None:
        """注册一个工具 ToolCard。重复注册以最新一次为准并写 info 日志。"""
        if tool_card is None:
            logger.warning("[SkillTurboEnvironment] register_tool ignored: None card")
            return
        name = getattr(tool_card, "name", None)
        if not name:
            logger.warning(
                "[SkillTurboEnvironment] register_tool ignored: card without name"
            )
            return
        if name in self._tools:
            logger.info("[SkillTurboEnvironment] register_tool overwrite tool=%s", name)
        self._tools[name] = tool_card

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    async def register_tools(self, context: "ToolLoaderContext | None" = None) -> None:
        """装载 jiuwenswarm + openjiuwen 工具到 environment。

        - 静态工具仅在首次调用时加载，重复调用幂等；
        - ``send_file_to_user`` 每次请求刷新（依赖 request_id / session_id）；
        - 所有可用性判断 100% 复用上游已有逻辑（``tools_loader`` 内不重写）；
        - 通过 ``ToolLoaderContext`` 透传 sys_operation / 模型配置 / 各类开关。
        """
        from jiuwenswarm.server.runtime.skill_turbo.tools_loader import (
            ToolLoaderContext as _Ctx,
            load_all,
            load_send_file_tools,
        )

        ctx = context or self.build_tool_loader_context()

        if not self._tools_loaded:
            tools = await load_all(ctx)
            for tool in tools:
                card = getattr(tool, "card", None)
                if card is not None:
                    self.register_tool(card)
            self._tools_loaded = True
            logger.info(
                "[SkillTurboEnvironment] register_tools static done loaded=%d tool_names=%s",
                len(tools),
                list(self._tools.keys()),
            )

        self._refresh_send_file_tools(ctx, load_send_file_tools)

    def build_tool_loader_context(
        self,
        *,
        request_id: str = "",
        session_id: str = "",
        channel_id: str = "",
        request_metadata: dict[str, Any] | None = None,
    ) -> "ToolLoaderContext":
        from jiuwenswarm.server.runtime.skill_turbo.tools_loader import ToolLoaderContext

        return ToolLoaderContext(
            agent_id=str(self._config.get("agent_id") or "skill_turbo"),
            language=str(self._config.get("language") or "zh"),
            sys_operation=self._resolve_sys_operation(),
            vision_model_config=self._config.get("vision_model_config"),
            audio_model_config=self._config.get("audio_model_config"),
            video_model_enabled=bool(self._config.get("video_model_enabled", False)),
            image_gen_enabled=bool(self._config.get("image_gen_enabled", False)),
            skill_manager=self._config.get("skill_manager"),
            request_id=request_id,
            session_id=session_id,
            channel_id=channel_id,
            request_metadata=request_metadata,
        )

    def _refresh_send_file_tools(
        self,
        ctx: "ToolLoaderContext",
        loader: Callable[[Any], list[Any]],
    ) -> None:
        """按请求刷新 send_file_to_user（移除旧实例后重新注册）。"""
        removed: list[str] = []
        for name in list(self._tools.keys()):
            if not name.startswith("send_file_to_user"):
                continue
            card = self._tools.pop(name)
            tool_id = getattr(card, "id", None)
            if tool_id:
                try:
                    Runner.resource_mgr.remove_tool(tool_id)
                except Exception as exc:
                    logger.warning(
                        "[SkillTurboEnvironment] remove send_file tool failed id=%s: %s",
                        tool_id,
                        exc,
                    )
            removed.append(name)

        tools = loader(ctx)
        for tool in tools:
            card = getattr(tool, "card", None)
            if card is None:
                continue
            tool_id = getattr(card, "id", None)
            if tool_id and Runner.resource_mgr.get_tool(tool_id) is not None:
                try:
                    Runner.resource_mgr.remove_tool(tool_id)
                except Exception as exc:
                    logger.debug(
                        "[SkillTurboEnvironment] remove stale send_file tool ignored id=%s: %s",
                        tool_id,
                        exc,
                    )
            try:
                Runner.resource_mgr.add_tool(tool)
            except Exception as exc:
                logger.warning(
                    "[SkillTurboEnvironment] register send_file tool failed: %s",
                    exc,
                    exc_info=True,
                )
                continue
            self.register_tool(card)

        if tools or removed:
            logger.info(
                "[SkillTurboEnvironment] send_file refresh removed=%s registered=%d",
                removed,
                len(tools),
            )

    # ────────────────────── 私有加载逻辑 ──────────────────────

    def _load(self, config: dict[str, Any]) -> None:
        """从配置加载 soul / tools / model / skills / skill_codes。"""
        self._model_client = config.get("model_client")
        for card in config.get("tool_cards", []) or []:
            name = getattr(card, "name", "")
            if name:
                self._tools[name] = card

        # 扫描 skills_dir 注册所有 skill（内置 + 自定义）。
        self._scan_skills_dir()

    def _scan_skills_dir(self) -> None:
        """扫描 ``skill_codes_dir`` 目录注册自定义 skill。

        约定（设计文档）：
            {skill_codes_dir}/{name}/{*}_root.py  -> 入口文件

        扫描逻辑：
            1. 遍历 skill_codes_dir 下的每个子目录作为技能名；
            2. 查找入口文件（优先级：{name}_gen_root.py -> *_gen_root.py -> {name}_root.py -> *_root.py）；
            3. 自动注册技能到环境中。

        缓存策略：
            扫描结果按 skill_codes_dir 绝对路径缓存到类级 ``_scan_cache``，
            缓存 key 包含目录内所有 .py 的最大 mtime。后续请求若 mtime 未变，
            直接复用缓存的 Skill 实例，跳过磁盘 IO 和 AST 校验。
        """
        # 优先使用配置的路径，如果无效则回退到默认路径
        base = Path(self._skill_codes_dir or "")
        if not base.is_dir():
            logger.warning(
                "[SkillTurboEnvironment] _scan_skills_dir config path not found: %s",
                self._skill_codes_dir,
            )
            # 回退到默认路径
            default_path = Path(_DEFAULT_SKILL_CODES_DIR)
            if not default_path.is_dir():
                logger.info(
                    "[SkillTurboEnvironment] _scan_skills_dir skipped: both config and default paths not found"
                )
                return
            base = default_path
            logger.info(
                "[SkillTurboEnvironment] _scan_skills_dir fallback to default path: %s",
                base,
            )

        # mtime 缓存：未变则复用上次扫描结果，跳过扫盘 + AST 校验
        cache_key = str(base.resolve())
        current_mtime = self._compute_skill_codes_mtime(base)
        cached = self._scan_cache.get(cache_key)
        if cached is not None and cached[0] == current_mtime and current_mtime > 0.0:
            for skill in cached[1]:
                self.register_skill(skill)
            logger.debug(
                "[SkillTurboEnvironment] _scan_skills_dir cache hit: dir=%s skills=%d",
                cache_key,
                len(cached[1]),
            )
            return

        scanned_skills: list[Skill] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            
            skill_name = skill_dir.name
            if skill_name.startswith("_") or skill_name.startswith("."):
                continue

            try:
                # 查找入口文件
                root_file = self._find_skill_root_file(skill_dir)
                if root_file is None:
                    logger.debug(
                        "[SkillTurboEnvironment] _scan_skills_dir skip skill=%s: no root file found",
                        skill_name,
                    )
                    continue
                
                # 校验 skill_code 目录内所有 Python 代码是否符合安全规则
                if not self._validate_skill_code_dir(skill_name, skill_dir):
                    continue

                # 构建 plan_code
                plan_code = self._build_plan_code(skill_name, root_file)

                skill = Skill(
                    name=skill_name,
                    description=f"{skill_name} 任务流",
                    skill_md="",
                    plan_code=plan_code,
                    match_keywords=[skill_name],
                )
                # 注册技能（使用技能名作为默认描述和匹配关键词）
                self.register_skill(skill)
                scanned_skills.append(skill)
                logger.info(
                    "[SkillTurboEnvironment] _scan_skills_dir registered skill=%s root_file=%s",
                    skill_name,
                    root_file.name,
                )
            except Exception as e:
                logger.warning(
                    "[SkillTurboEnvironment] _scan_skills_dir failed to register skill=%s: %s",
                    skill_name,
                    e,
                )

        # 写入缓存（仅当成功计算到 mtime 时）
        if current_mtime > 0.0:
            self._scan_cache[cache_key] = (current_mtime, scanned_skills)

        # [TEMP-EXTERNAL-SKILL] 在框架层完成 SHA256 校验，结果注入到 inputs 供 skill_code 读取。
        # 校验目标：{skill_root}/{skill_name} 子目录（如 office-claw-skills/pptx-craft），
        # 而非整个 skill_root 根目录（根目录包含多个 skill，任一变更都会导致校验失败）。
        if self._skill_root:
            skill_dir = str(Path(self._skill_root) / self._skill_name)
            self._skill_checksum_ok = _verify_skill_checksum(
                skill_dir, self._skill_checksum
            )
        else:
            # skill_root 未解析时跳过校验（默认 True）
            self._skill_checksum_ok = True

    @staticmethod
    def _compute_skill_codes_mtime(base: Path) -> float:
        """计算 skill_codes 目录内所有 .py 文件的最大 mtime。

        失败时返回 0.0（表示禁用缓存，触发重新扫描）。
        """
        try:
            mtimes = [
                p.stat().st_mtime
                for p in base.rglob("*.py")
                if not any(part in {"__pycache__", ".git"} for part in p.parts)
            ]
        except OSError as e:
            logger.debug(
                "[SkillTurboEnvironment] _compute_skill_codes_mtime failed dir=%s error=%s",
                base,
                e,
            )
            return 0.0
        return max(mtimes, default=0.0)

    def _validate_skill_code_dir(self, skill_name: str, skill_dir: Path) -> bool:
        """加载 skill 时校验目录内所有 Python 代码。"""
        if not skill_dir.is_dir():
            logger.warning(
                "[SkillTurboEnvironment] skill_code validation failed: skill=%s dir not found: %s",
                skill_name,
                skill_dir,
            )
            return False

        success = True
        for code_file in sorted(skill_dir.rglob("*.py")):
            if any(part in {"__pycache__", ".git"} for part in code_file.parts):
                continue
            try:
                code = code_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(
                    "[SkillTurboEnvironment] skill_code read failed: skill=%s file=%s error=%s",
                    skill_name,
                    code_file,
                    e,
                )
                success = False
                continue

            errors = self._skill_code_validator.validate(code)
            if errors:
                logger.warning(
                    "[SkillTurboEnvironment] skill_code validation failed: skill=%s file=%s errors=%s",
                    skill_name,
                    code_file,
                    errors,
                )
                success = False

        if not success:
            message = f"skill_code validation failed: {skill_name}"
            logger.warning(
                "[SkillTurboEnvironment] skip skill registration due to validation errors: %s",
                skill_name,
            )
            if self._strict_builtin_skill_validation:
                raise ValueError(message)
        return success

    @staticmethod
    def _find_skill_root_file(skill_dir: Path) -> Path | None:
        """查找技能入口文件。

        优先级：
            1. {skill_name}_gen_root.py
            2. {skill_name}_root.py
            3. 任意 *_gen_root.py（按名称排序）
            4. 任意 *_root.py（按名称排序）
            5. plan_code.py（兜底入口，须与 planner._find_skill_root_file 保持一致）
        """
        skill_name = skill_dir.name
        candidates = [
            skill_dir / f"{skill_name}_gen_root.py",
            skill_dir / f"{skill_name}_root.py",
            *sorted(skill_dir.glob("*_gen_root.py")),
            *sorted(skill_dir.glob("*_root.py")),
            skill_dir / "plan_code.py",
        ]

        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
        return None

    def _build_plan_code(self, skill_name: str, root_file: Path) -> str:
        """构建技能的 plan_code。"""
        module = f"{self._skill_code_import_package}.{skill_name}.{root_file.stem}"
        return f"from {module} import root"
