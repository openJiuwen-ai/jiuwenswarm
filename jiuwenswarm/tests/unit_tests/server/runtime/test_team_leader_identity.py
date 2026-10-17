from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import jiuwenswarm.server.runtime.extension_package_manager as package_manager
import jiuwenswarm.server.runtime.session.session_metadata as session_metadata
from jiuwenswarm.server.runtime.agent_adapter import team_helpers


def test_leader_identity_normalizes_unsafe_avatar_without_losing_name() -> None:
    assert package_manager.normalize_agent_group_leader_identity(
        {
            "agent_template_id": " leader-template ",
            "display_name": "  专家团负责人 ",
            "avatar": "javascript:alert(1)",
        }
    ) == {
        "agent_template_id": "leader-template",
        "display_name": "专家团负责人",
        "avatar": "",
    }
    assert package_manager.normalize_agent_group_leader_identity(
        {
            "agent_template_id": "leader-template",
            "display_name": "专家团负责人",
            "avatar": "https://example.test/leader.png",
        }
    )["avatar"] == "https://example.test/leader.png"


def test_leader_identity_preserves_localized_display_name() -> None:
    assert package_manager.normalize_agent_group_leader_identity(
        {
            "agent_template_id": "leader-template",
            "display_name": "中文负责人",
            "display_name_i18n": {
                "zh": "中文负责人",
                "en": "English Lead",
            },
            "avatar": "",
        }
    ) == {
        "agent_template_id": "leader-template",
        "display_name": "中文负责人",
        "display_name_i18n": {
            "zh": "中文负责人",
            "en": "English Lead",
        },
        "avatar": "",
    }


def test_local_leader_avatar_uses_the_existing_data_url_resolver(tmp_path) -> None:
    member_dir = tmp_path / "leader"
    member_dir.mkdir()
    (member_dir / "avatar.png").write_bytes(b"png-bytes")

    safe_avatar_reference = getattr(package_manager, "_safe_avatar_reference")
    resolved = safe_avatar_reference(
        "avatar.png",
        package_dir=member_dir,
    )

    assert resolved == "data:image/png;base64,cG5nLWJ5dGVz"


def test_agent_group_leader_identity_prefers_member_display_name(
    tmp_path,
    monkeypatch,
) -> None:
    group_dir = tmp_path / "group-a"
    leader_dir = group_dir / "agents" / "leader"
    leader_dir.mkdir(parents=True)
    (group_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "group-a",
                "package_type": "agent_group",
                "member_templates": {"leader": "leader-template"},
            }
        ),
        encoding="utf-8",
    )
    (leader_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "leader-template",
                "display_name": {"zh": "中文负责人", "en": "Chinese Lead"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        package_manager,
        "resolve_agent_group_dir",
        lambda _name: group_dir,
    )
    import jiuwenswarm.agents.swarm.agent_group as agent_group

    monkeypatch.setattr(
        agent_group,
        "load_agent_group_package",
        lambda _path: {
            "leader": SimpleNamespace(
                agent_card=SimpleNamespace(name="leader-template")
            )
        },
    )

    identity = package_manager.resolve_agent_group_leader_identity("group-a")

    assert identity is not None
    assert identity["agent_template_id"] == "leader-template"
    assert identity["display_name"] == "中文负责人"
    assert identity["display_name_i18n"] == {
        "zh": "中文负责人",
        "en": "Chinese Lead",
    }


def test_runtime_ready_gets_identity_but_chat_final_does_not() -> None:
    identity = {
        "agent_template_id": "leader-template",
        "display_name": "专家团负责人",
        "avatar": "",
    }
    attach_team_leader_identity = getattr(
        team_helpers, "_attach_team_leader_identity"
    )
    ready = attach_team_leader_identity(
        {"event_type": "team.runtime_ready"}, identity
    )
    final = attach_team_leader_identity(
        {"event_type": "chat.final", "content": "answer"}, identity
    )

    assert ready["team_leader_identity"] == identity
    assert "team_leader_identity" not in final


def test_runtime_ready_without_identity_keeps_ordinary_team_payload() -> None:
    attach_team_leader_identity = getattr(
        team_helpers, "_attach_team_leader_identity"
    )
    ready = attach_team_leader_identity(
        {"event_type": "team.runtime_ready"}, None
    )

    assert "team_leader_identity" not in ready


def test_session_leader_identity_is_written_once(monkeypatch) -> None:
    stored = {
        "session_id": "session-1",
        "agent_group_name": "group-a",
        "team_leader_identity": {
            "agent_template_id": "old-leader",
            "display_name": "旧负责人",
            "avatar": "",
        },
    }
    writes: list[dict] = []
    monkeypatch.setattr(session_metadata, "_read_metadata", lambda *_args, **_kwargs: copy.deepcopy(stored))
    monkeypatch.setattr(
        session_metadata,
        "_enqueue_write",
        lambda _session_id, metadata, **_kwargs: writes.append(copy.deepcopy(metadata)),
    )

    session_metadata.update_session_metadata(
        session_id="session-1",
        agent_group_name="group-a",
        team_leader_identity={
            "agent_template_id": "new-leader",
            "display_name": "新负责人",
            "avatar": "https://example.test/new.png",
        },
        touch_last_message_at=False,
    )

    assert writes[-1]["team_leader_identity"] == stored["team_leader_identity"]


def test_sync_team_identity_metadata_forwards_snapshot(monkeypatch) -> None:
    calls: list[dict] = []
    reads: list[dict] = []
    monkeypatch.setattr(
        team_helpers,
        "get_session_metadata",
        lambda *_args, **kwargs: reads.append(kwargs) or {},
    )
    monkeypatch.setattr(
        team_helpers,
        "update_session_metadata",
        lambda **kwargs: calls.append(kwargs),
    )

    identity = {
        "agent_template_id": "leader-template",
        "display_name": "专家团负责人",
        "avatar": "",
    }
    team_helpers.sync_team_identity_metadata(
        channel_id="web",
        session_id="session-1",
        ready_team_name="team-a",
        activation_kind="initial",
        team_leader_identity=identity,
    )

    assert calls[-1]["team_leader_identity"] == identity
    assert calls[-1]["sync_write"] is True
    assert calls[-1]["cache_bust"] is True
    assert reads[-1]["cache_bust"] is True
