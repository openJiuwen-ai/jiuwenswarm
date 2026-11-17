# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt rail for reporting real team workspace artifact paths."""

from __future__ import annotations

from pathlib import Path

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail


class TeamWorkspaceReportPathRail(DeepAgentRail):
    """Inject guidance for reporting real shared-workspace artifact paths."""

    priority = 5

    def __init__(
        self,
        *,
        root_dir: str,
        team_id: str | None = None,
        language: str = "cn",
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._root_dir = str(Path(root_dir))
        self._team_id = team_id or ""
        self._language = language

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("team_workspace_report_paths")
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return

        mount = ".team/"
        # Background: `.team/` is the tool mount onto the shared team workspace. When the
        # mount is healthy, realpath(`.team/foo`) equals `{root_dir}/foo`. OfficeAce may
        # leave a real `.team` directory under the session project instead — then inventing
        # a path under `root_dir` (often on another drive) breaks open-in-chat. Always pass
        # the absolute path of the file that actually exists (write_file / realpath).
        content = (
            "# Team Workspace Artifact Paths\n\n"
            f"- Team workspace absolute root (`.team/` mount target when linked): `{self._root_dir}`\n"
            f"- Internal mount path: `{mount}`\n"
            "- Use the internal mount path only for tool read/write operations.\n"
            "- For the user-facing final deliverable, call `send_file_to_user` once with the real absolute "
            "path of the file on disk (prefer the absolute path returned by `write_file`, or realpath of "
            "the `.team/...` path you wrote). Do not invent a path under another root.\n"
            "- A file name or path in `send_message`, the final response, or a task-completion summary does not "
            "deliver the file and must not replace `send_file_to_user`.\n"
            "- After `send_file_to_user` succeeds, report that same real absolute path to teammates "
            "(not a bare `.team/...` mount string, and not a reconstructed path under a different root).\n"
            "- The mount is already the shared workspace root; do not add a team id or `team-workspace` "
            "segment after `.team/`.\n"
            "- Example: after writing `.team/prd-review-memo.md`, pass the absolute path from the write "
            "result to `send_file_to_user` — one user-facing path, once.\n"
        )
        self.system_prompt_builder.add_section(
            PromptSection(
                name="team_workspace_report_paths",
                content={"cn": content, "en": content},
                priority=67,
            )
        )
