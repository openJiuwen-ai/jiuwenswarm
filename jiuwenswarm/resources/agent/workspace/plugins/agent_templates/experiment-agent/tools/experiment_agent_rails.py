"""Runtime rails owned by the Module 3 experiment agent template."""

from __future__ import annotations

from pathlib import Path

from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail


class ExperimentSpecialistSkillRail(SkillUseRail):
    """Inject the shared orchestration skill without generic file/shell tools.

    OpenJiuwen normally adds ``skill_tool``, ``read_file`` and ``bash`` when a
    skill is mounted on an agent.  Module 3 specialists must instead operate
    exclusively through their stage tool, whose backend enforces the state
    machine and path/approval gates.  Subclassing ``SkillUseRail`` also keeps
    the factory from auto-adding its permissive default rail.
    """

    def __init__(self) -> None:
        skills_root = Path(__file__).resolve().parent.parent / "skills"
        super().__init__(
            skills_dir=str(skills_root),
            skill_mode=SkillUseRail.SKILL_MODE_ALL,
            include_tools=False,
            enabled_skills=["experiment-orchestration"],
        )

    def init(self, agent) -> None:
        """Attach prompt state only; intentionally register zero tools."""

        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self.attachment_manager = getattr(agent, "prompt_attachment_manager", None)
