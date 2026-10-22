import faulthandler

from jiuwenswarm.common import debug_dump


def test_install_fault_log_enables_faulthandler(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(debug_dump, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(debug_dump, "_fault_log_handle", None)
    was_enabled = faulthandler.is_enabled()
    try:
        debug_dump.install_fault_log("agentserver")
        assert faulthandler.is_enabled()
        assert (tmp_path / "agentserver_fault.log").is_file()
        debug_dump.install_fault_log("agentserver")
        assert debug_dump._fault_log_handle is not None
    finally:
        handle = debug_dump._fault_log_handle
        debug_dump._fault_log_handle = None
        if not was_enabled:
            faulthandler.disable()
        if handle is not None:
            handle.close()
