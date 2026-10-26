import logging

from jiuwenswarm.common import utils


def test_log_level_environment_enables_debug_for_all_runtime_handlers(monkeypatch):
    monkeypatch.setattr(
        utils,
        "_load_logging_config_from_yaml",
        lambda: {
            "level": "INFO",
            "console_level": "INFO",
            "gateway": "INFO",
            "channel": "INFO",
            "agent_server": "INFO",
            "full": "INFO",
        },
    )
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    levels = utils._resolve_logging_levels(None)

    assert levels == utils.LoggingLevels(
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
    )
