# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import sys
from pathlib import Path

from jiuwenclaw.agentserver.skilldev_agent.utils.gate_obs_upload import pop_gate_obs_upload

_SKILL_VERIFIER_ROOT = (
    Path(__file__).resolve().parents[4]
    / "jiuwenclaw"
    / "agentserver"
    / "skilldev_agent"
    / "skills"
    / "skill-verifier"
)
if str(_SKILL_VERIFIER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_VERIFIER_ROOT))

from scripts.upload_skill import (  # noqa: E402
    GATE_OBS_STATE_FILE,
    gate_obs_state_path,
    record_gate_obs_upload,
)


def test_record_and_pop_gate_obs_upload(tmp_path: Path) -> None:
    packaged = tmp_path / "output" / "demo.skill"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"zip")

    record_gate_obs_upload(tmp_path, "https://obs.example/demo.skill", packaged)

    state_path = gate_obs_state_path(tmp_path)
    assert state_path.name == GATE_OBS_STATE_FILE
    assert state_path.is_file()

    record = pop_gate_obs_upload(tmp_path)
    assert record == {
        "url": "https://obs.example/demo.skill",
        "filename": "demo.skill",
        "mimeType": "application/zip",
    }
    assert not state_path.exists()


def test_pop_gate_obs_upload_missing_file_returns_none(tmp_path: Path) -> None:
    assert pop_gate_obs_upload(tmp_path) is None


def test_pop_gate_obs_upload_invalid_json_deletes_file(tmp_path: Path) -> None:
    state_path = gate_obs_state_path(tmp_path)
    state_path.write_text("{not-json", encoding="utf-8")

    assert pop_gate_obs_upload(tmp_path) is None
    assert not state_path.exists()


def test_pop_gate_obs_upload_empty_url_returns_none(tmp_path: Path) -> None:
    state_path = gate_obs_state_path(tmp_path)
    state_path.write_text(
        json.dumps({"url": "", "filename": "demo.skill", "mimeType": "application/zip"}),
        encoding="utf-8",
    )

    assert pop_gate_obs_upload(tmp_path) is None
    assert not state_path.exists()


def test_record_gate_obs_upload_skips_empty_url(tmp_path: Path) -> None:
    record_gate_obs_upload(tmp_path, "", tmp_path / "demo.skill")
    assert not gate_obs_state_path(tmp_path).exists()
