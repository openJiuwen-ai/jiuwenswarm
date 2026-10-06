from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common import utils


@pytest.fixture
def file_logging(tmp_path, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(utils, "get_logs_dir", lambda: tmp_path)
        scoped.setattr(utils, "get_gateway_log_dir", lambda: None)
        utils.setup_logger("INFO")
        yield tmp_path
    # Restore runtime destinations and close test file handles via normal setup.
    utils.setup_logger()


def flush_logs():
    for handler in logging.getLogger("jiuwenswarm").handlers:
        handler.flush()


@pytest.mark.parametrize("name,message", [
    ("jiuwenswarm.common.duplex_clef", "Clef decision request_id=c1 action=INTERRUPT"),
    ("jiuwenswarm.common.duplex_jev", "Jev decision request_id=j1 action=APPEND"),
    ("jiuwenswarm.common.duplex_router", "duplex freshness check bypassed stage=observation"),
    ("jiuwenswarm.agents.harness.team.duplex_shadow", "duplex route entry message_id=m1"),
    ("jiuwenswarm.agents.harness.team.duplex_native", "duplex interrupt committed message_id=m1"),
    ("jiuwenswarm.server.runtime.agent_adapter.team_helpers", "duplex user followup dispatch request_id=u1"),
])
def test_supervisor_events_are_collected_in_one_file(file_logging, name, message):
    logging.getLogger(name).info(message)
    flush_logs()
    text = (file_logging / "duplex.log").read_text(encoding="utf-8")
    assert text.count(message) == 1
    assert "pid=" in text
    assert "duplex logging ready" in text


def test_unrelated_user_payloads_are_not_collected(file_logging):
    logging.getLogger("jiuwenswarm.server.runtime.agent_adapter.team_helpers").info(
        "[TeamHelpers] user query=private-user-text")
    logging.getLogger("jiuwenswarm.gateway").info("private-gateway-text")
    flush_logs()
    text = (file_logging / "duplex.log").read_text(encoding="utf-8")
    assert "private-user-text" not in text
    assert "private-gateway-text" not in text


def test_duplex_file_reuses_sensitive_data_filter(file_logging):
    logging.getLogger("jiuwenswarm.common.duplex_jev").warning(
        'Jev request failed {"api_key":"private-unit-token"}')
    flush_logs()
    text = (file_logging / "duplex.log").read_text(encoding="utf-8")
    assert "Jev request failed" in text
    assert "private-unit-token" not in text


def test_repeated_setup_does_not_duplicate_duplex_handler(file_logging):
    root = utils.setup_logger("INFO")
    files = [h for h in root.handlers if getattr(h, "baseFilename", "").endswith("duplex.log")]
    assert len(files) == 1
    assert files[0].max_bytes == 20 * 1024 * 1024
    assert files[0].backup_count == 20
    logging.getLogger("jiuwenswarm.common.duplex_clef").info("Clef unique-event")
    flush_logs()
    assert (file_logging / "duplex.log").read_text(encoding="utf-8").count("Clef unique-event") == 1


def test_cmd_style_process_persists_logs_without_console_redirection(tmp_path):
    script = """
import logging, sys
from pathlib import Path
from jiuwenswarm.common import utils
utils.get_logs_dir = lambda: Path(sys.argv[1])
utils.get_gateway_log_dir = lambda: None
utils.setup_logger('INFO')
logging.getLogger('jiuwenswarm.common.duplex_clef').info('Clef cmd-process-event')
logging.getLogger('jiuwenswarm.agents.harness.team.duplex_native').info('duplex interrupt committed cmd-test')
logging.shutdown()
"""
    # Console streams are discarded, not redirected into a log file.
    subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                   cwd=Path(__file__).resolve().parents[3],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, check=True)
    text = (tmp_path / "duplex.log").read_text(encoding="utf-8")
    assert "Clef cmd-process-event" in text
    assert "duplex interrupt committed cmd-test" in text
