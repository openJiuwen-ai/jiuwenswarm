"""已注册技能的只读声明索引；不授权执行、不读盘、不调用模型。"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FullPipelineSpec:
    plan_code: str
    id: str = "__full__"


class AcceleratorRegistry:
    def __init__(self, env: SkillTurboEnvironment):
        self._skills = {}
        ambiguous = set()
        self.ok = getattr(env, "external_scan_ok", True)
        for skill in env.skills.values():
            name = skill.external_name or skill.name
            if name in self._skills:
                ambiguous.add(name)
            self._skills[name] = copy.deepcopy(skill)
        for name in ambiguous:
            self._skills.pop(name)
            logger.warning("加速器精确索引拒绝歧义external_name=%s", name)

    def list_for(self, skill: str) -> tuple[AcceleratorSpec, ...]:
        record = self._skills.get(skill)
        if record is None:
            return ()
        return copy.deepcopy(tuple(sorted((s for s in record.accelerators if s.enabled), key=lambda s: s.order)))

    def get(self, skill: str, accelerator_id: str) -> AcceleratorSpec | FullPipelineSpec | None:
        record = self._skills.get(skill)
        if record is None:
            return None
        if accelerator_id == "__full__":
            return FullPipelineSpec(record.full_plan_code) if record.full_plan_code else None
        return next((s for s in self.list_for(skill) if s.id == accelerator_id), None)
