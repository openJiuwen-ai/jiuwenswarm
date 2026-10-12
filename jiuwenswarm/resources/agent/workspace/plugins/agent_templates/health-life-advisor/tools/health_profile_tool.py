"""用户健康档案管理工具。

提供建档、更新档案、读取档案、生成/迭代健康生活方案、记录执行情况、
阶段性复盘等功能。所有档案读写一律通过本工具完成，禁止手改档案文件。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.sys_operation.cwd import get_cwd


class HealthProfileTool(Tool):
    """用户健康档案管理工具，配合 health-life-planning skill 使用。"""

    AGENT_NAME = "health-life-advisor"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="health_profile_tool",
                name="health_profile_tool",
                description=(
                    "用户健康档案管理工具：建档、更新档案、读取档案、生成/迭代健康生活方案、"
                    "记录执行情况、阶段性复盘。配合 health-life-planning skill 使用。"
                    "所有档案读写一律通过本工具完成，禁止手改档案文件。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "create_profile",
                                "update_profile",
                                "get_profile",
                                "generate_plan",
                                "update_plan",
                                "log_progress",
                                "review_progress",
                            ],
                            "description": (
                                "操作类型：create_profile=建档, update_profile=更新档案, "
                                "get_profile=读取档案, generate_plan=生成方案, "
                                "update_plan=迭代方案, log_progress=记录执行, "
                                "review_progress=复盘"
                            ),
                        },
                        "profile_data": {
                            "type": "object",
                            "description": (
                                "用户健康档案数据（建档/更新时传入），"
                                "包含 basic_info/diet/sleep/emotion 等字段"
                            ),
                        },
                        "plan_data": {
                            "type": "object",
                            "description": (
                                "健康方案数据（生成/迭代方案时传入），"
                                "包含 diet/sleep/emotion/exercise/daily_actions/"
                                "weekly_actions/duration_weeks/season 等字段"
                            ),
                        },
                        "plan_feedback": {
                            "type": "string",
                            "description": "方案迭代时的用户执行反馈",
                        },
                        "progress_data": {
                            "type": "object",
                            "description": (
                                "执行记录数据（记录执行时传入），"
                                "包含 date/actions_completed/notes 等字段"
                            ),
                        },
                        "review_data": {
                            "type": "object",
                            "description": (
                                "复盘数据（复盘时传入），"
                                "包含 period/summary/blockers/adjustments 等字段"
                            ),
                        },
                    },
                    "required": ["action"],
                },
            )
        )

    def _get_profile_path(self, kwargs: dict[str, Any]) -> Path:
        """获取当前 session 的健康档案路径。"""
        session = kwargs.get("session")
        session_id = "default"
        if session is not None:
            get_session_id = getattr(session, "get_session_id", None)
            if callable(get_session_id):
                session_id = get_session_id()
        base = Path(get_cwd())
        profile_dir = base / self.AGENT_NAME / session_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir / "health_profile.json"

    @staticmethod
    def _empty_profile() -> dict[str, Any]:
        """返回空档案模板。"""
        now = datetime.now(timezone.utc).isoformat()
        return {
            "basic_info": {},
            "diet": {},
            "sleep": {},
            "emotion": {},
            "plans": [],
            "progress_logs": [],
            "reviews": [],
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _load_profile(path: Path) -> dict[str, Any]:
        """加载健康档案，不存在则返回空模板。"""
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text(encoding="utf-8"))
        return HealthProfileTool._empty_profile()

    @staticmethod
    def _save_profile(path: Path, data: dict[str, Any]) -> None:
        """原子写入健康档案。"""
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            action = inputs.get("action", "")
            path = self._get_profile_path(kwargs)

            if action == "create_profile":
                return self._create_profile(path, inputs)
            elif action == "update_profile":
                return self._update_profile(path, inputs)
            elif action == "get_profile":
                return self._get_profile_data(path)
            elif action == "generate_plan":
                return self._generate_plan(path, inputs)
            elif action == "update_plan":
                return self._update_plan(path, inputs)
            elif action == "log_progress":
                return self._log_progress(path, inputs)
            elif action == "review_progress":
                return self._review_progress(path, inputs)
            else:
                return {"success": False, "error": f"未知操作类型: {action}"}
        except Exception as e:
            return {"success": False, "error": f"操作失败: {e}"}

    async def stream(self, inputs: dict[str, Any], **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _create_profile(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        if path.exists():
            return {
                "success": False,
                "error": "档案已存在，请使用 update_profile 更新",
            }
        data = inputs.get("profile_data", {})
        profile = self._empty_profile()
        for key in ("basic_info", "diet", "sleep", "emotion"):
            if key in data:
                profile[key] = data[key]
        self._save_profile(path, profile)
        return {"success": True, "message": "健康档案已创建", "profile": profile}

    def _update_profile(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self._load_profile(path)
        data = inputs.get("profile_data", {})
        for key in ("basic_info", "diet", "sleep", "emotion"):
            if key in data:
                if isinstance(profile.get(key), dict) and isinstance(
                    data[key], dict
                ):
                    profile[key].update(data[key])
                else:
                    profile[key] = data[key]
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_profile(path, profile)
        return {"success": True, "message": "健康档案已更新", "profile": profile}

    def _get_profile_data(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {
                "success": False,
                "error": "档案不存在，请先使用 create_profile 建档",
            }
        profile = self._load_profile(path)
        return {"success": True, "profile": profile}

    def _generate_plan(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self._load_profile(path)
        plan_data = inputs.get("plan_data", {})
        now = datetime.now(timezone.utc).isoformat()
        version = len(profile.get("plans", [])) + 1
        plan: dict[str, Any] = {
            "version": version,
            "created_at": now,
            "duration_weeks": plan_data.get("duration_weeks", 2),
            "diet": plan_data.get("diet", {}),
            "sleep": plan_data.get("sleep", {}),
            "emotion": plan_data.get("emotion", {}),
            "exercise": plan_data.get("exercise", {}),
            "daily_actions": plan_data.get("daily_actions", []),
            "weekly_actions": plan_data.get("weekly_actions", []),
            "season": plan_data.get("season", ""),
            "status": "active",
        }
        for old_plan in profile.get("plans", []):
            old_plan["status"] = "archived"
        profile.setdefault("plans", []).append(plan)
        profile["updated_at"] = now
        self._save_profile(path, profile)
        return {
            "success": True,
            "message": f"健康方案 v{version} 已生成",
            "plan": plan,
        }

    def _update_plan(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self._load_profile(path)
        plans = profile.get("plans", [])
        if not plans:
            return {
                "success": False,
                "error": "尚无方案，请先使用 generate_plan 生成",
            }
        active_plan = None
        for p in plans:
            if p.get("status") == "active":
                active_plan = p
                break
        if active_plan is None:
            active_plan = plans[-1]
        feedback = inputs.get("plan_feedback", "")
        plan_data = inputs.get("plan_data", {})
        now = datetime.now(timezone.utc).isoformat()
        version = len(plans) + 1
        new_plan: dict[str, Any] = {
            "version": version,
            "created_at": now,
            "duration_weeks": plan_data.get(
                "duration_weeks", active_plan.get("duration_weeks", 2)
            ),
            "diet": plan_data.get("diet", active_plan.get("diet", {})),
            "sleep": plan_data.get("sleep", active_plan.get("sleep", {})),
            "emotion": plan_data.get("emotion", active_plan.get("emotion", {})),
            "exercise": plan_data.get(
                "exercise", active_plan.get("exercise", {})
            ),
            "daily_actions": plan_data.get(
                "daily_actions", active_plan.get("daily_actions", [])
            ),
            "weekly_actions": plan_data.get(
                "weekly_actions", active_plan.get("weekly_actions", [])
            ),
            "season": plan_data.get("season", active_plan.get("season", "")),
            "status": "active",
            "feedback": feedback,
        }
        active_plan["status"] = "archived"
        plans.append(new_plan)
        profile["updated_at"] = now
        self._save_profile(path, profile)
        return {
            "success": True,
            "message": f"健康方案已迭代至 v{version}",
            "plan": new_plan,
        }

    def _log_progress(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self._load_profile(path)
        progress = inputs.get("progress_data", {})
        log_entry: dict[str, Any] = {
            "date": progress.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            "actions_completed": progress.get("actions_completed", []),
            "notes": progress.get("notes", ""),
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
        profile.setdefault("progress_logs", []).append(log_entry)
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_profile(path, profile)
        return {
            "success": True,
            "message": "执行记录已保存",
            "log": log_entry,
        }

    def _review_progress(
        self, path: Path, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self._load_profile(path)
        review_data = inputs.get("review_data", {})
        review: dict[str, Any] = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "period": review_data.get("period", ""),
            "summary": review_data.get("summary", ""),
            "blockers": review_data.get("blockers", []),
            "adjustments": review_data.get("adjustments", []),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        profile.setdefault("reviews", []).append(review)
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_profile(path, profile)
        logs = profile.get("progress_logs", [])
        return {
            "success": True,
            "message": "复盘记录已保存",
            "review": review,
            "progress_log_count": len(logs),
        }
