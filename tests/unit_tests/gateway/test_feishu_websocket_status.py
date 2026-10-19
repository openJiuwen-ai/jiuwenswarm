import logging
from unittest.mock import MagicMock

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu import feishu_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
    _FeishuWebSocketLogHandler,
)


def _channel() -> FeishuChannel:
    return FeishuChannel(
        FeishuConfig(enabled=True, app_id="app-test"),
        MagicMock(),
    )


def _sdk_record(message: str, *, thread_id: int, level: int = logging.INFO) -> logging.LogRecord:
    record = logging.LogRecord("Lark", level, "lark/ws/client.py", 1, message, (), None)
    record.thread = thread_id
    return record


def test_sdk_connected_log_marks_handshake_without_logging_connection_url(monkeypatch):
    channel = _channel()
    handler = _FeishuWebSocketLogHandler(channel, thread_id=17)
    logged = MagicMock()
    monkeypatch.setattr(feishu_connect.logger, "info", logged)

    handler.emit(_sdk_record("connected to wss://example.invalid/?secret=hidden", thread_id=17))

    assert channel._websocket_connected.is_set()
    logged.assert_called_once()
    log_message = logged.call_args.args
    assert "握手成功" in log_message[0]
    assert "app-test" in log_message
    assert "wss://example.invalid" not in str(log_message)
    assert "secret=hidden" not in str(log_message)


def test_sdk_failure_is_reported_but_unrelated_thread_is_ignored(monkeypatch):
    channel = _channel()
    handler = _FeishuWebSocketLogHandler(channel, thread_id=17)
    channel._websocket_connected.set()
    logged = MagicMock()
    monkeypatch.setattr(feishu_connect.logger, "error", logged)

    handler.emit(_sdk_record("connected to wss://unrelated", thread_id=99))
    assert channel._websocket_connected.is_set()
    logged.assert_not_called()

    handler.emit(
        _sdk_record(
            "connect failed, err: credentials should not be copied to gateway logs",
            thread_id=17,
            level=logging.ERROR,
        )
    )

    assert not channel._websocket_connected.is_set()
    logged.assert_called_once()
    log_message = logged.call_args.args
    assert "连接失败" in log_message[0]
    assert "app-test" in log_message
    assert "credentials should not be copied" not in str(log_message)


def test_message_callback_warns_when_main_loop_is_unavailable(monkeypatch):
    channel = _channel()
    logged = MagicMock()
    monkeypatch.setattr(feishu_connect.logger, "warning", logged)

    channel._on_message_sync(object())

    logged.assert_called_once()
    assert "主事件循环不可用" in logged.call_args.args[0]
    assert "app-test" in logged.call_args.args
