from jiuwenswarm.server.runtime.agent_adapter.cron_reply import cron_empty_reply_fallback


def test_cron_empty_reply_fallback_returns_visible_history_message() -> None:
    assert cron_empty_reply_fallback(
        {"cron": {"job_id": "weather"}}
    ) == "[cron] 任务执行完成但未返回结果内容"


def test_cron_empty_reply_fallback_does_not_change_normal_chat() -> None:
    assert cron_empty_reply_fallback({"query": "hello"}) is None
