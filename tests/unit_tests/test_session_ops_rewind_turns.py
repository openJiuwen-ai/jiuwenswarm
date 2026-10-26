from __future__ import annotations

import json


def test_list_session_turns_includes_project_file_change_stats(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    project_dir = tmp_path / "project"
    session_id = "session-1"
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)

    (session_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "content": "change the file",
                        "timestamp": 1000.0,
                        "id": "u1",
                        "request_id": "r1",
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "done",
                        "timestamp": 1002.0,
                    }
                ),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    history_dir = project_dir / ".agent_history"
    history_dir.mkdir(parents=True)
    changed_file = project_dir / "demo.txt"
    (history_dir / "file_ops_jiuwenswarm.json").write_text(
        json.dumps(
            {
                str(changed_file): [
                    {
                        "action": "write",
                        "timestamp": "1970-01-01T00:16:41+00:00",
                        "old_content": "before\n",
                        "new_content": "before\nafter\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: tmp_path / "agent-workspace",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: tmp_path / "user-workspace",
    )

    from jiuwenswarm.agents.harness.common.session_ops_service import list_session_turns

    result = list_session_turns(session_id=session_id, project_dir=str(project_dir))

    assert result["turns"][0]["stats"] == {
        "filesChanged": 1,
        "linesAdded": 1,
        "linesRemoved": 0,
    }
    assert result["turns"][0]["files"] == [
        {
            "path": str(changed_file.resolve()),
            "linesAdded": 1,
            "linesRemoved": 0,
            "isNewFile": False,
        }
    ]
