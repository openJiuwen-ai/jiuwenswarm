"""Smoke coverage for the installed SDK API required by AgentServer startup."""

import subprocess
import sys
from pathlib import Path


def test_installed_sdk_supports_agentserver_meeting_imports() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer; "
            "from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingRoute, MeetingState; "
            "print('agentserver meeting imports ready')",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "agentserver meeting imports ready" in result.stdout
