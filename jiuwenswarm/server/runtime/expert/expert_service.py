# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家会话操作编排（experts.list / expert.load / expert.unload 的业务内核）。

fetch+校验与错误码映射、session metadata 读写时机（先应用成功才写）、
root/child 适配器定位、BUSY 守卫与持锁复验（ExpertApplyBusyError）映射，
全部收敛到本模块；WS handler 只剩 params 提取与 AgentResponse 发包
（见 ``agent_ws_server.py`` 的 ``_handle_expert_*`` 薄壳）。

结果以 :class:`ExpertOpResult` 返回，``payload`` 直接作 ``AgentResponse.payload``：
成功含业务字段（expert_id/applied/pending/previous_expert_id/warnings），
失败含 ``error`` + ``code``（BAD_REQUEST / NOT_FOUND / INVALID_PACKAGE / BUSY /
LOAD_FAILED / REPO_UNAVAILABLE / INTERNAL_ERROR）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.server.runtime.expert import expert_store as _expert_store

logger = logging.getLogger(__name__)

_BUSY_MESSAGE = "当前回合执行中，请等回合结束"

# 专家团专用基础模板：config modes.team.expert_group 存在时优先，否则回退默认模板
_EXPERT_GROUP_TEMPLATE_ID = "expert_group"


def read_package_type(package_dir: Path) -> str:
    """按落盘 manifest 判定包类型（"team" | "agent"）；不信 list 元数据。"""
    try:
        manifest = json.loads(
            (Path(package_dir) / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "agent"
    if isinstance(manifest, dict) and manifest.get("package_type") == "agent_group":
        return "team"
    return "agent"


# ---- 历史消息专家身份快照（按消息落盘"当时是谁答的"） ----

# expert_id → 显示名解析缓存（包显示名进程内基本不变；写盘路径要轻，不重复读盘）
_expert_name_cache: dict[str, str] = {}


def _resolve_expert_display_name(expert_id: str, expert_type: str) -> str:
    """解析专家显示名快照：team → 主理人花名（缺省团名）；agent → agentCard.name。

    缓存包目录优先（get_cached_expert_package_dir），本地目录 override 回退
    本地 experts 目录；尽力而为，失败返回 ""（不阻塞历史落盘）。
    """
    cached = _expert_name_cache.get(expert_id)
    if cached:
        return cached
    name = ""
    try:
        package_dir = _expert_store.get_cached_expert_package_dir(expert_id)
        if package_dir is None:
            # 本地目录 override（JIUWEN_EXPERT_LOCAL_DIRS）不落缓存，直接看本地目录
            from jiuwenswarm.common.utils import get_agent_experts_dir

            candidate = get_agent_experts_dir() / expert_id
            if (candidate / "manifest.json").is_file():
                package_dir = candidate
        if package_dir is not None:
            if expert_type == "team":
                from jiuwenswarm.server.runtime.expert.agent_group import (
                    read_group_display,
                    read_group_members,
                )

                members = read_group_members(package_dir)
                lead = next((m for m in members if m.get("role") == "lead"), None)
                name = str((lead or {}).get("name") or "").strip()
                if not name:
                    name = read_group_display(package_dir)["name"]
            else:
                manifest = json.loads(
                    (package_dir / "manifest.json").read_text(encoding="utf-8")
                )
                card = manifest.get("agentCard") if isinstance(manifest, dict) else None
                if isinstance(card, dict) and card.get("name"):
                    name = str(card["name"])
    except Exception:  # noqa: BLE001 - 显示名解析失败不阻塞历史落盘
        name = ""
    if name:
        _expert_name_cache[expert_id] = name
    return name


def current_expert_identity_extra(session_id: str) -> dict[str, str]:
    """assistant 历史落盘的专家身份 extra：按写盘时刻的会话绑定记录
    "当时是谁答的"（expert.load/unload 改绑定不影响已落盘记录）。

    返回 {"expert_id", "expert_type", "expert_name"}；无绑定返回 {}。
    expert_name 是写盘时刻的显示名快照（保真：包改名/删除后历史身份仍准）。
    """
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
    )

    metadata = get_session_metadata(session_id) or {}
    expert_id = str(metadata.get("expert_id") or "").strip()
    if not expert_id:
        return {}
    expert_type = str(metadata.get("expert_type") or "agent")
    extra = {"expert_id": expert_id, "expert_type": expert_type}
    name = _resolve_expert_display_name(expert_id, expert_type)
    if name:
        extra["expert_name"] = name
    return extra


def history_expert_identity_extra(session_id: str) -> dict[str, str]:
    """assistant 主应答落盘的身份 extra（始终非空）。

    与 current_expert_identity_extra 的差异：未绑定专家时显式写
    {"expert_id": "", "expert_type": "agent"}——区分"该消息由默认角色作答"
    与"存量消息无字段"，前端据此不再把历史消息身份回落到会话当前绑定
    （首轮默认角色作答、中途才绑专家的会话，旧消息身份不应变成新专家）。
    """
    extra = current_expert_identity_extra(session_id)
    if extra:
        return extra
    return {"expert_id": "", "expert_type": "agent"}


async def fetch_and_classify_expert(expert_id: str) -> tuple[str, Path, list[str]]:
    """fetch + 校验 + 判型，返回 (expert_type, package_dir, warnings)。

    异常原样上抛（ExpertNotFound / ExpertRepoUnavailable / InvalidExpertPackage），
    由调用方映射错误码。供 expert.load 与 session.create 共用。
    """
    package_dir = await _expert_store.get_expert_source().fetch(expert_id)
    warnings = _expert_store.validate_expert_package(package_dir)
    return read_package_type(package_dir), package_dir, warnings


def resolve_expert_group_template_id() -> str:
    """专家团基础模板 id：modes.team.expert_group 存在则用之，否则 ""（默认模板）。"""
    from jiuwenswarm.agents.harness.team.config_loader import (
        list_team_template_summaries,
    )

    for summary in list_team_template_summaries():
        if summary.get("template_id") == _EXPERT_GROUP_TEMPLATE_ID:
            return _EXPERT_GROUP_TEMPLATE_ID
    return ""


def cleanup_expert_group_team_db(team_name: str) -> None:
    """删除专家团 team DB 目录（team_home(team_name)）。

    调用时机：显式退团（expert.unload）/会话删除——**换团不调用**（换回同团
    依赖同 team_name 的 DB 现场存活）。目录不存在时静默跳过。
    """
    if not team_name:
        return
    import shutil

    from openjiuwen.agent_teams.paths import team_home

    home = team_home(team_name)
    if home.is_dir():
        shutil.rmtree(home, ignore_errors=True)
        logger.info("[ExpertService] 团队 DB 已清理: team_name=%s", team_name)


def build_expert_group_team_name(expert_id: str, session_id: str) -> str:
    """会话级唯一 team_name，避免多会话共用 team DB 目录。

    会话 id 形如 ``{channel}_{hex时间戳}_{uuid12}``（agent_manager.create_session）。
    直接 ``session_id[:8]`` 会切到恒定渠道前缀（如 "desktop_"），同渠道所有会话
    撞名共用 team DB——必须剥掉首个下划线前的渠道段再截断。空 session_id 显式
    拒绝（残缺 team_name 一旦写入 metadata 即固化，后续多会话串台）。
    """
    if not session_id:
        raise ValueError(
            f"build_expert_group_team_name 要求非空 session_id（expert_id={expert_id}）"
        )
    sid_body = session_id.split("_", 1)[1] if "_" in session_id else session_id
    return f"expert-group-{expert_id}-{sid_body[:8]}"


@dataclass
class ExpertOpResult:
    """一次专家操作的结果：ok + 直接可作 AgentResponse.payload 的 dict。"""

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)


class ExpertService:
    """专家会话操作：list / load / unload。

    依赖经构造注入：``agent_manager`` 提供按定位键取 agent 的入口，
    ``adapter_resolver`` 从 agent 提取底层适配器（生产环境即
    ``AgentWebSocketServer._resolve_adapter``）。
    """

    def __init__(
            self,
            *,
            agent_manager: Any,
            adapter_resolver: Callable[[Any], Any],
    ) -> None:
        self._agent_manager = agent_manager
        self._adapter_resolver = adapter_resolver

    async def list_experts(self) -> ExpertOpResult:
        try:
            summaries = await _expert_store.get_expert_source().list()
            return ExpertOpResult(
                ok=True,
                payload={"experts": [asdict(s) for s in summaries]},
            )
        except _expert_store.ExpertRepoUnavailable as exc:
            logger.warning("[ExpertService] experts.list 仓库不可达: %s", exc)
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "REPO_UNAVAILABLE"}
            )
        except Exception as exc:
            logger.exception("[ExpertService] experts.list failed: %s", exc)
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "INTERNAL_ERROR"}
            )

    async def load_expert(
            self,
            *,
            channel_id: str,
            session_id: str,
            expert_id: str,
    ) -> ExpertOpResult:
        """召唤/切换专家：先 fetch+校验，再按子适配器是否存在走 applied/pending。

        顺序是「先应用、成功后才写 metadata」——装载失败不留脏 expert_id。
        """
        from jiuwenswarm.server.runtime.agent_adapter.expert_capability import (
            ExpertApplyBusyError,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            update_session_metadata,
        )

        if not session_id or not expert_id:
            return ExpertOpResult(
                ok=False,
                payload={"error": "missing session_id or expert_id", "code": "BAD_REQUEST"},
            )

        # 1) fetch + 校验 + 判型（此步之前不写任何状态；以落盘 manifest 为准）
        try:
            expert_type, package_dir, warnings = await fetch_and_classify_expert(expert_id)
        except _expert_store.ExpertNotFound as exc:
            logger.warning("[ExpertService] expert.load 专家不存在: %s", exc)
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "NOT_FOUND"}
            )
        except _expert_store.ExpertRepoUnavailable as exc:
            logger.warning("[ExpertService] expert.load 仓库不可达: %s", exc)
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "REPO_UNAVAILABLE"}
            )
        except _expert_store.InvalidExpertPackage as exc:
            logger.warning("[ExpertService] expert.load 包非法: %s", exc)
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "INVALID_PACKAGE"}
            )

        # 专家团走团队分支（team 线冷构造，非会话 DeepAgent 热加载）
        if expert_type == "team":
            return await self._load_expert_team(
                channel_id=channel_id,
                session_id=session_id,
                expert_id=expert_id,
                warnings=warnings,
            )

        # 2) 旧值
        metadata = get_session_metadata(session_id, cache_bust=True)
        if not metadata:
            logger.warning(
                "[session_id=%s] [ExpertService] expert.load: session 不存在", session_id
            )
            return ExpertOpResult(
                ok=False,
                payload={"error": f"session 不存在: {session_id}", "code": "NOT_FOUND"},
            )
        previous_expert_id = str(metadata.get("expert_id") or "")

        # 3) 子适配器不存在 → pending（首次装配由入口 create_instance() 生效）
        root, child = self._locate_session_adapter(
            channel_id,
            session_id,
            mode=metadata.get("mode"),
            project_dir=metadata.get("project_dir"),
        )
        if child is None or not child.has_live_instance():
            update_session_metadata(session_id=session_id, expert_id=expert_id, sync=True)
            return ExpertOpResult(
                ok=True,
                payload={
                    "expert_id": expert_id,
                    "type": "agent",
                    "applied": False,
                    "pending": True,
                    "previous_expert_id": previous_expert_id,
                    "warnings": warnings,
                },
            )

        # 4) BUSY 守卫：回合执行中直接拒绝，不排队
        if self._switch_busy(root, child, session_id):
            return ExpertOpResult(
                ok=False, payload={"error": _BUSY_MESSAGE, "code": "BUSY"}
            )

        # 5) 应用（成功后才写 metadata）
        try:
            await child.apply_expert(expert_id, package_dir=package_dir)
        except ExpertApplyBusyError as exc:
            # 持锁复验命中：守卫与 apply 之间的空隙里 chat 开始了回合
            logger.info(
                "[session_id=%s] [ExpertService] expert.load 持锁复验 BUSY: %s",
                session_id, exc,
            )
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "BUSY"}
            )
        except Exception as exc:
            logger.exception(
                "[session_id=%s] [ExpertService] expert.load 应用失败 expert=%s: %s",
                session_id, expert_id, exc,
            )
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "LOAD_FAILED"}
            )
        update_session_metadata(session_id=session_id, expert_id=expert_id, sync=True)
        return ExpertOpResult(
            ok=True,
            payload={
                "expert_id": expert_id,
                "type": "agent",
                "applied": True,
                "pending": False,
                "previous_expert_id": previous_expert_id,
                "warnings": warnings,
            },
        )

    async def _load_expert_team(
            self,
            *,
            channel_id: str,
            session_id: str,
            expert_id: str,
            warnings: list[str],
    ) -> ExpertOpResult:
        """召唤/切换专家团：写 metadata 绑定，team 线下次 chat 冷构建生效。

        与单专家的差异：不热加载到会话 DeepAgent，而是写 expert_type="team" +
        team_name/team_template_id/mode="team" 五字段；换绑先卸后装——
        旧团停 team 运行时、旧单专家走 apply_expert(None) 卸载。
        """
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            update_session_metadata,
        )

        metadata = get_session_metadata(session_id, cache_bust=True)
        if not metadata:
            logger.warning(
                "[session_id=%s] [ExpertService] expert.load(team): session 不存在",
                session_id,
            )
            return ExpertOpResult(
                ok=False,
                payload={"error": f"session 不存在: {session_id}", "code": "NOT_FOUND"},
            )
        previous_expert_id = str(metadata.get("expert_id") or "")
        previous_expert_type = str(metadata.get("expert_type") or "agent")

        # 幂等：同团重复 load 直接成功
        if previous_expert_id == expert_id and previous_expert_type == "team":
            return ExpertOpResult(
                ok=True,
                payload={
                    "expert_id": expert_id,
                    "type": "team",
                    "applied": True,
                    "pending": False,
                    "team_name": str(metadata.get("team_name") or ""),
                    "previous_expert_id": previous_expert_id,
                    "warnings": warnings,
                },
            )

        root, child = self._locate_session_adapter(
            channel_id,
            session_id,
            mode=metadata.get("mode"),
            project_dir=metadata.get("project_dir"),
        )

        # BUSY 守卫：单专家双侧信号 ∪ team 回合活跃信号
        if self._switch_busy(root, child, session_id) or self._team_round_active(session_id):
            return ExpertOpResult(
                ok=False, payload={"error": _BUSY_MESSAGE, "code": "BUSY"}
            )

        # 换绑先卸：旧团停运行时；旧单专家热卸载
        try:
            if previous_expert_id:
                if previous_expert_type == "team" or metadata.get("team_name"):
                    await self._stop_team_runtime(session_id, reason="expert.switch")
                elif child is not None and child.has_live_instance():
                    await child.apply_expert(None)
        except Exception as exc:
            logger.exception(
                "[session_id=%s] [ExpertService] expert.load(team) 先卸失败 previous=%s: %s",
                session_id, previous_expert_id, exc,
            )
            return ExpertOpResult(
                ok=False, payload={"error": str(exc), "code": "LOAD_FAILED"}
            )

        session_live = child is not None and child.has_live_instance()
        if session_live:
            # 有活会话：停（可能存在的）team 运行时，下次 chat 冷重建生效
            await self._stop_team_runtime(session_id, reason="expert.team.apply")

        team_name = build_expert_group_team_name(expert_id, session_id)
        team_template_id = resolve_expert_group_template_id()
        # 装团收敛：按会话 work_mode 查 WorkModeProfile 注册表得团队
        # canonical——work→team / code→code.team / design→design.team，
        # 不再写死 "team"（code/design 会话装团不再被拍平为 work profile）。
        from jiuwenswarm.common.mode_profiles import team_canonical_for_work_mode

        team_canonical = team_canonical_for_work_mode(metadata.get("work_mode"))
        update_session_metadata(
            session_id=session_id,
            expert_id=expert_id,
            expert_type="team",
            # 留痕：卸载后四字段全清，was_expert_type 保留"该会话用过团协作"
            # （前端「切换专家团将开启新对话」弹窗的跨重启判定依据）
            was_expert_type="team",
            # 最近绑定记录：卸载时不清，供退团后归档成员面板按包解析头像/展示名
            last_expert_id=expert_id,
            team_name=team_name,
            team_template_id=team_template_id,
            mode=team_canonical,
            sync=True,
        )
        logger.info(
            "[session_id=%s] [ExpertService] 专家团绑定: expert=%s team_name=%s "
            "template=%s previous=%s(%s)",
            session_id, expert_id, team_name, team_template_id or "default",
            previous_expert_id, previous_expert_type,
        )
        return ExpertOpResult(
            ok=True,
            payload={
                "expert_id": expert_id,
                "type": "team",
                "applied": session_live,
                "pending": not session_live,
                "team_name": team_name,
                "previous_expert_id": previous_expert_id,
                "warnings": warnings,
            },
        )

    async def unload_expert(
            self,
            *,
            channel_id: str,
            session_id: str,
    ) -> ExpertOpResult:
        """退出专家：回默认身份。无专家幂等；无活实例只清 metadata。"""
        from jiuwenswarm.server.runtime.agent_adapter.expert_capability import (
            ExpertApplyBusyError,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            update_session_metadata,
        )

        if not session_id:
            return ExpertOpResult(
                ok=False,
                payload={"error": "missing session_id", "code": "BAD_REQUEST"},
            )
        metadata = get_session_metadata(session_id, cache_bust=True)
        if not metadata:
            logger.warning(
                "[session_id=%s] [ExpertService] expert.unload: session 不存在", session_id
            )
            return ExpertOpResult(
                ok=False,
                payload={"error": f"session 不存在: {session_id}", "code": "NOT_FOUND"},
            )
        previous_expert_id = str(metadata.get("expert_id") or "")
        if not previous_expert_id:
            return ExpertOpResult(
                ok=True,
                payload={"type": "agent", "applied": False, "previous_expert_id": ""},
            )

        # 专家团分支：停 team 运行时 + 清四字段 + mode 回 agent
        if str(metadata.get("expert_type") or "agent") == "team":
            return await self._unload_expert_team(
                channel_id=channel_id,
                session_id=session_id,
                metadata=metadata,
                previous_expert_id=previous_expert_id,
            )

        root, child = self._locate_session_adapter(
            channel_id,
            session_id,
            mode=metadata.get("mode"),
            project_dir=metadata.get("project_dir"),
        )
        applied = False
        if child is not None and child.has_live_instance():
            if self._switch_busy(root, child, session_id):
                return ExpertOpResult(
                    ok=False, payload={"error": _BUSY_MESSAGE, "code": "BUSY"}
                )
            try:
                await child.apply_expert(None)
                applied = True
            except ExpertApplyBusyError as exc:
                logger.info(
                    "[session_id=%s] [ExpertService] expert.unload 持锁复验 BUSY: %s",
                    session_id, exc,
                )
                return ExpertOpResult(
                    ok=False, payload={"error": str(exc), "code": "BUSY"}
                )
            except Exception as exc:
                logger.exception(
                    "[session_id=%s] [ExpertService] expert.unload 应用失败 previous=%s: %s",
                    session_id, previous_expert_id, exc,
                )
                return ExpertOpResult(
                    ok=False, payload={"error": str(exc), "code": "LOAD_FAILED"}
                )
        update_session_metadata(session_id=session_id, expert_id="", sync=True)
        return ExpertOpResult(
            ok=True,
            payload={"type": "agent", "applied": applied, "previous_expert_id": previous_expert_id},
        )

    async def _unload_expert_team(
            self,
            *,
            channel_id: str,
            session_id: str,
            metadata: dict[str, Any],
            previous_expert_id: str,
    ) -> ExpertOpResult:
        """退出专家团：停 team 运行时、清绑定字段、mode 按 work_mode 恢复单 agent canonical。"""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            update_session_metadata,
        )

        root, child = self._locate_session_adapter(
            channel_id,
            session_id,
            mode=metadata.get("mode"),
            project_dir=metadata.get("project_dir"),
        )
        if self._switch_busy(root, child, session_id) or self._team_round_active(session_id):
            return ExpertOpResult(
                ok=False, payload={"error": _BUSY_MESSAGE, "code": "BUSY"}
            )
        stopped = await self._stop_team_runtime(session_id, reason="expert.unload")
        # 显式退团：级联清理 team DB（换团不清——换回同团依赖 DB 现场存活）
        cleanup_expert_group_team_db(str(metadata.get("team_name") or ""))
        # 退团恢复：按会话 work_mode 查注册表回该模式的单 agent canonical
        # （work→agent / code→code.normal / design→design），修掉"写死回 agent
        # 致 code/design 会话退团降级为 work"的缺陷。
        from jiuwenswarm.common.mode_profiles import single_canonical_for_work_mode

        restore_mode = single_canonical_for_work_mode(metadata.get("work_mode"))
        update_session_metadata(
            session_id=session_id,
            expert_id="",
            expert_type="agent",
            team_name="",
            team_template_id="",
            mode=restore_mode,
            # last_expert_id 不写（保留卸载前的团 id）——归档成员面板的 roster 解析源
            sync=True,
        )
        logger.info(
            "[session_id=%s] [ExpertService] 专家团退出: previous=%s stopped=%s",
            session_id, previous_expert_id, stopped,
        )
        return ExpertOpResult(
            ok=True,
            payload={
                "type": "team",
                "applied": True,
                "previous_expert_id": previous_expert_id,
            },
        )

    @staticmethod
    def _team_round_active(session_id: str) -> bool:
        """team 回合活跃信号（_stream_tasks 是回合级，待机态为 False）。"""
        try:
            from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

            return bool(get_team_manager().has_stream_task(session_id))
        except Exception:
            # TeamManager 不可用时不阻塞专家操作（与单专家守卫同语义）
            return False

    @staticmethod
    async def _stop_team_runtime(session_id: str, *, reason: str) -> bool:
        """停本会话 team 运行时（幂等；无运行时返回 False）。"""
        try:
            from jiuwenswarm.agents.harness.team.team_manager import (
                stop_team_session_runtime_across_managers,
            )

            return bool(
                await stop_team_session_runtime_across_managers(
                    session_id, reason=reason, stop_runner=True
                )
            )
        except Exception as exc:
            logger.warning(
                "[session_id=%s] [ExpertService] 停 team 运行时失败（按无运行时继续）: %s",
                session_id, exc,
            )
            return False

    def _locate_session_adapter(
            self,
            channel_id: str,
            session_id: str,
            *,
            mode: str | None = None,
            project_dir: str | None = None,
    ) -> tuple[Any, Any]:
        """定位 (root_adapter, session_child_adapter)；不强制创建任何实例。

        mode/project_dir 必须取自该会话的 metadata（与 chat 路径同一套定位键）：
        同一 channel 下不同 project_dir 会存在多个 root，只按 channel 定位会拿错
        root——表现为「装载/卸载返回成功但实际会话没变化」。

        注意 metadata 里的 mode 是 canonical（如 ``code.normal``），而
        AgentManager 按 **manager mode**（``code``）注册实例——直接拿 canonical
        查会命中失败、退化为「只清 metadata 不碰活实例」。
        这里统一先做 canonical → manager 换算（查 WorkModeProfile 注册表）。
        """
        from jiuwenswarm.common.mode_profiles import manager_mode_for_canonical

        manager_mode = manager_mode_for_canonical(mode) or mode
        agent = self._agent_manager.get_agent_nowait(
            channel_id=channel_id or "default",
            mode=manager_mode or None,
            project_dir=project_dir or None,
        )
        if agent is None:
            return None, None
        root = self._adapter_resolver(agent)
        if root is None:
            return None, None
        if root.is_session_scoped:
            return root, root
        return root, root.get_cached_child_adapter(session_id)

    @staticmethod
    def _switch_busy(root: Any, child: Any, session_id: str) -> bool:
        if root is not None and not root.is_session_scoped:
            return bool(root.expert_switch_blocked(session_id))
        return bool(child is not None and child.is_session_live(session_id))
