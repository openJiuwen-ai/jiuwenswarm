"""Fix-6 contract: get_sandbox_team_fail_closed parses string booleans."""
import jiuwenswarm.common.config as cfg_mod


def test_fail_closed_string_false_is_off(monkeypatch):
    monkeypatch_target = "jiuwenswarm.common.config.get_config"
    import pytest
    for raw, expected in [("false", False), ("0", False), ("no", False),
                          ("true", True), (True, True), (False, False), (None, False)]:
        cfg = {"sandbox": {"team_fail_closed": raw}} if raw is not None else {}
        monkeypatch.setattr(cfg_mod, "get_config", lambda cfg=cfg: cfg)
        assert cfg_mod.get_sandbox_team_fail_closed() == expected, f"{raw!r} -> {expected}"
