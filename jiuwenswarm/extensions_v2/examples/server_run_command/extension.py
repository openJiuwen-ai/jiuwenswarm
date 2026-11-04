# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Worker slash command served entirely by an installed USER Handler."""

from jiuwenswarm.extensions_v2.event_types.server_run_command import ServerRunCommandResult


class ProjectCommand:
    def register(self, registrar):
        registrar.register_event_handler(
            "server.run_command", self.project_status, {"command": "project_status"},
        )

    async def project_status(self, event, ops, next_handler):
        del next_handler
        cwd = await ops.session.cwd()
        label = event.event_input.args or "current project"
        return ServerRunCommandResult(
            text=f"{label}: {cwd or 'workspace not provided'}",
            context=(f"The project command selected workspace {cwd or 'unknown'} for {label}.",),
        )


def create_extension():
    return ProjectCommand()
