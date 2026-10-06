from jiuwenswarm.benchmarks.interruptbench_replay import decision_rows, _mean


def test_replay_preserves_decision_time_and_remaining_task_time():
    rows = decision_rows([
        {"event": "run_start", "elapsed_seconds": 10},
        {"event": "route_decision", "elapsed_seconds": 14, "latency_ms": 500,
         "message_ids": ["m1"], "attempts": 1, "action": "INTERRUPT", "status": "stale"},
        {"event": "run_end", "elapsed_seconds": 20},
    ])
    assert rows[0]["decision_seconds"] == 0.5
    assert rows[0]["called_at_seconds"] == 3.5
    assert rows[0]["task_seconds_from_call"] == 6.5
    assert rows[0]["stale"] is True


def test_incomplete_run_has_no_invented_completion_time():
    rows = decision_rows([{"event": "route_decision", "elapsed_seconds": 14, "latency_ms": 500}])
    assert rows[0]["task_seconds_from_call"] is None
    assert _mean(row["task_seconds_from_call"] for row in rows) is None
