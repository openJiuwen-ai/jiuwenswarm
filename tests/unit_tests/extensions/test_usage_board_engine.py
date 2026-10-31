"""usage_board 引擎单元测试：解析、计总口径、费用估算、导出与结论目录。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jiuwenswarm.extensions.usage_board import usage_engine
from jiuwenswarm.extensions.usage_board.usage_engine import (
    FINDING_DEFS,
    FINDINGS_BY_CODE,
    CallUsage,
    PriceTable,
    build_export,
    cost_for_call,
    load_price_table,
    parse_epoch,
    parse_session_lines,
    render_csv,
    render_markdown,
    scan_sessions,
    session_detail,
)

REPO_ROOT = Path(usage_engine.__file__).resolve().parents[3]
FIXTURE_CONTEXT_USAGE = (
    REPO_ROOT
    / "jiuwenswarm"
    / "channels"
    / "web"
    / "frontend"
    / "tests"
    / "fixtures"
    / "contextUsage.v1.json"
)

TS = 1_700_000_000.0  # 2023-11-14T22:13:20Z，远离自然日边界
DAY = usage_engine.day_of(TS)


# --------------------------------------------------------------------------
# 构造器：完全照 history.jsonl 的真实记录形状
# --------------------------------------------------------------------------


def summary_record(
    *,
    ts: float = TS,
    model: str | None = "gpt-5",
    input_tokens: int = 100,
    output_tokens: int = 50,
    total_tokens: int | None = 150,
    cost: float | None = None,
    session_id: str = "s1",
    extra_usage: dict | None = None,
) -> dict:
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    if cost is not None:
        usage["total_cost"] = cost
    if extra_usage:
        usage.update(extra_usage)
    return {
        "id": "rid:assistant",
        "role": "assistant",
        "request_id": "rid",
        "channel_id": "web",
        "timestamp": ts,
        "content": "",
        "event_type": "chat.usage_summary",
        "session_id": session_id,
        "model": model,
        "usage": usage,
    }


def metadata_record(
    *,
    ts: float = TS,
    model: str | None = "gpt-5",
    input_tokens: int = 100,
    output_tokens: int = 50,
    total_tokens: int = 150,
    cost: float | None = None,
    cache_tokens: int = 0,
) -> dict:
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_tokens": cache_tokens,
    }
    if cost is not None:
        usage["total_cost"] = cost
    return {
        "id": "rid:assistant",
        "role": "assistant",
        "timestamp": ts,
        "content": "",
        "event_type": "chat.usage_metadata",
        "session_id": "s1",
        "metadata": {"usage_metadata": usage, "model": model},
    }


def context_record(
    *,
    ts: float = TS,
    limit: int = 2000,
    used: int = 1000,
    occupancy_rate: float | None = 0.5,
    model: str = "probe-model",
    provider: str = "openai",
    agent_id: str | None = None,
) -> dict:
    return {
        "id": "rid:assistant",
        "role": "assistant",
        "timestamp": ts,
        "content": "",
        "event_type": "context.usage",
        "context_window": {
            "limit_tokens": limit,
            "input_tokens": used,
            "occupancy_rate": occupancy_rate,
        },
        "model": model,
        "provider": provider,
        "agent_id": agent_id,
    }


def write_history(sessions_dir: Path, session_id: str, records: list[dict | str]) -> Path:
    folder = sessions_dir / session_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / usage_engine.HISTORY_FILENAME
    lines = [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 让文件 mtime 与日志里最新的时间戳一致：宿主是追加写，引擎会按 mtime 粗筛过期会话。
    stamps = [
        float(record["timestamp"])
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("timestamp"), (int, float))
        and not isinstance(record.get("timestamp"), bool)
    ]
    if stamps:
        os.utime(path, (max(stamps), max(stamps)))
    return path


def write_prices(root: Path, payload: object) -> Path:
    folder = root / "usage_board"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "prices.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def totals_of(lines: list[str]) -> dict:
    session = parse_session_lines(lines, session_id="s1")
    return session


# --------------------------------------------------------------------------
# 价格表
# --------------------------------------------------------------------------


def test_missing_price_table_is_not_zero(tmp_path: Path) -> None:
    table = load_price_table(tmp_path / "nope" / "prices.json")

    assert table.configured is False
    assert table.match("gpt-5") is None
    assert table.error is None
    assert table.as_dict()["models"] == 0


def test_price_table_parses_and_prefers_exact_match(tmp_path: Path) -> None:
    path = write_prices(
        tmp_path,
        {
            "currency": "USD",
            "models": {
                "gpt-*": {"input_per_1m": 1.0, "output_per_1m": 2.0},
                "gpt-5": {"input_per_1m": 5.0, "output_per_1m": 15.0},
            },
        },
    )

    table = load_price_table(path)

    assert table.configured is True
    assert table.currency == "USD"
    assert table.match("GPT-5").input_per_1m == 5.0
    assert table.match("gpt-5-mini").input_per_1m == 1.0
    assert table.match("qwen-max") is None


def test_price_table_wildcard_in_the_middle(tmp_path: Path) -> None:
    path = write_prices(
        tmp_path,
        {"models": {"claude-*-sonnet": {"input_per_1m": 3.0, "output_per_1m": 15.0}}},
    )

    table = load_price_table(path)

    assert table.match("claude-3-5-sonnet") is not None
    assert table.match("claude-sonnet") is None


def test_price_table_survives_corrupt_file(tmp_path: Path) -> None:
    path = write_prices(tmp_path, "{not json")

    table = load_price_table(path)

    assert table.configured is False
    assert table.error is not None


def test_price_table_rejects_bad_entries(tmp_path: Path) -> None:
    path = write_prices(
        tmp_path,
        {
            "models": {
                "negative": {"input_per_1m": -1, "output_per_1m": 2},
                "text": {"input_per_1m": "5", "output_per_1m": None},
                "empty": {},
                "only_output": {"output_per_1m": 3},
            }
        },
    )

    table = load_price_table(path)
    patterns = {entry.pattern for entry in table.entries}

    # 非数字整条丢弃；单边负值按 0 计，另一边仍然可用。
    assert patterns == {"negative", "only_output"}
    assert table.match("negative").input_per_1m == 0.0
    assert table.match("negative").output_per_1m == 2.0
    assert table.match("only_output").input_per_1m == 0.0
    assert table.match("text") is None
    assert table.match("empty") is None


def test_price_table_non_mapping_root_reports_error(tmp_path: Path) -> None:
    path = write_prices(tmp_path, [1, 2, 3])

    table = load_price_table(path)

    assert table.configured is False
    assert table.error is not None


def test_price_table_without_models_reports_error(tmp_path: Path) -> None:
    path = write_prices(tmp_path, {"currency": "USD"})

    table = load_price_table(path)

    assert table.configured is False
    assert "models" in str(table.error)


# --------------------------------------------------------------------------
# 费用
# --------------------------------------------------------------------------


def _call(**kwargs) -> CallUsage:
    defaults = {
        "source": "usage_summary",
        "ts": TS,
        "day": DAY,
        "model": "gpt-5",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_tokens": 0,
        "cost_reported": None,
    }
    defaults.update(kwargs)
    return CallUsage(**defaults)


def test_estimate_uses_per_million_units() -> None:
    prices = PriceTable(
        entries=(usage_engine.PriceEntry("gpt-5", 5.0, 15.0, "USD"),),
        currency="USD",
    )

    cost, source = cost_for_call(
        _call(input_tokens=1_000_000, output_tokens=1_000_000), prices
    )

    assert source == "estimated"
    assert cost == 20.0


def test_reported_cost_wins_over_estimate() -> None:
    prices = PriceTable(entries=(usage_engine.PriceEntry("gpt-5", 5.0, 15.0, "USD"),))

    cost, source = cost_for_call(
        _call(input_tokens=1_000_000, cost_reported=0.01), prices
    )

    assert (cost, source) == (0.01, "reported")


def test_unpriced_call_stays_missing() -> None:
    cost, source = cost_for_call(_call(input_tokens=1234), PriceTable())

    assert cost is None
    assert source == "missing"


def test_reported_zero_cost_is_not_replaced_by_an_estimate() -> None:
    """上报 0（免费/按积分计量）是有效金额，不能回退成价格表估算。"""

    prices = PriceTable(entries=(usage_engine.PriceEntry("gpt-5", 5.0, 15.0, "USD"),))
    call = _call(input_tokens=1_000_000, output_tokens=1_000_000, cost_reported=0.0)

    assert cost_for_call(call, prices) == (0.0, "reported")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, (0.0, False)),
        (0.0, (0.0, False)),
        ("0", (0.0, False)),
        (None, (None, False)),
        ("", (None, False)),
        (-1, (None, True)),
        (float("nan"), (None, True)),
        ("abc", (None, True)),
    ],
)
def test_usage_cost_separates_zero_from_missing(value: Any, expected: tuple) -> None:
    assert usage_engine._usage_cost(value) == expected


# --------------------------------------------------------------------------
# 解析与计总口径
# --------------------------------------------------------------------------


def test_summary_is_authoritative_when_both_sources_exist() -> None:
    session = parse_session_lines(
        [
            json.dumps(summary_record(total_tokens=150, cost=0.03)),
            json.dumps(metadata_record(total_tokens=150, cost=0.03)),
        ],
        session_id="s1",
    )

    assert session.totals_source == "usage_summary"
    assert [call.total_tokens for call in session.billing_calls] == [150]
    assert session.calls == 1
    assert session.summary_tokens == 150
    assert session.metadata_tokens == 150
    cross = usage_engine.session_cross_check(session)
    assert cross["relative_gap"] == 0.0


def test_metadata_used_when_summary_absent() -> None:
    session = parse_session_lines(
        [json.dumps(metadata_record(total_tokens=150))], session_id="s1"
    )

    assert session.totals_source == "usage_metadata"
    assert [call.total_tokens for call in session.billing_calls] == [150]
    assert usage_engine.session_cross_check(session) is None


def test_total_tokens_falls_back_to_input_plus_output() -> None:
    session = parse_session_lines(
        [json.dumps(summary_record(input_tokens=7, output_tokens=3, total_tokens=0))],
        session_id="s1",
    )

    assert session.billing_calls[0].total_tokens == 10


def test_negative_and_non_finite_values_are_zeroed_and_counted() -> None:
    session = parse_session_lines(
        [
            json.dumps(
                summary_record(
                    input_tokens=-5,
                    output_tokens=10,
                    total_tokens=10,
                    extra_usage={"cache_tokens": "abc"},
                )
            ),
            json.dumps(
                summary_record(
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    cost=-3,
                )
            ),
        ],
        session_id="s1",
    )

    assert session.billing_calls[0].input_tokens == 0
    assert session.billing_calls[0].cache_tokens == 0
    assert session.billing_calls[1].cost_reported is None
    assert session.invalid_fields == 3
    codes = {item.code for item in usage_engine.session_findings(session, PriceTable())}
    assert "UB007" in codes


def test_corrupt_and_foreign_lines_are_skipped() -> None:
    session = parse_session_lines(
        [
            "{not json",
            "[1,2,3]",
            "",
            "   ",
            json.dumps({"event_type": "chat.delta", "content": "hi"}),
            json.dumps(summary_record()),
        ],
        session_id="s1",
    )

    assert session.skipped_lines == 2
    assert len(session.summary_calls) == 1
    codes = {item.code for item in usage_engine.session_findings(session, PriceTable())}
    assert "UB004" in codes


def test_overlong_line_is_skipped_without_parsing() -> None:
    session = parse_session_lines(
        ["{" + "x" * (usage_engine.MAX_LINE_CHARS + 10)],
        session_id="s1",
    )

    assert session.skipped_lines == 1


def test_cross_check_flags_gap_above_one_percent() -> None:
    session = parse_session_lines(
        [
            json.dumps(summary_record(total_tokens=1000)),
            json.dumps(metadata_record(total_tokens=1500)),
        ],
        session_id="s1",
    )

    cross = usage_engine.session_cross_check(session)
    codes = {item.code for item in usage_engine.session_findings(session, PriceTable())}

    assert cross["relative_gap"] == 0.5
    assert "UB003" in codes


def test_cross_check_tolerates_small_gap() -> None:
    session = parse_session_lines(
        [
            json.dumps(summary_record(total_tokens=1000)),
            json.dumps(metadata_record(total_tokens=1005)),
        ],
        session_id="s1",
    )

    codes = {item.code for item in usage_engine.session_findings(session, PriceTable())}

    assert "UB003" not in codes


def test_context_snapshot_keeps_latest_window() -> None:
    session = parse_session_lines(
        [
            json.dumps(context_record(ts=TS, limit=1000, used=100, occupancy_rate=0.1)),
            json.dumps(
                context_record(
                    ts=TS + 60,
                    limit=2000,
                    used=1000,
                    occupancy_rate=0.5,
                    agent_id="probe-agent",
                )
            ),
            json.dumps(summary_record(total_tokens=10)),
        ],
        session_id="s1",
    )

    assert session.context["limit_tokens"] == 2000
    assert session.context["input_tokens"] == 1000
    assert session.context["occupancy_rate"] == 0.5
    assert session.context["provider"] == "openai"
    assert session.context["agent_id"] == "probe-agent"


def test_context_zero_occupancy_is_zero_not_none() -> None:
    session = parse_session_lines(
        [json.dumps(context_record(occupancy_rate=0))], session_id="s1"
    )

    assert session.context["occupancy_rate"] == 0.0


def test_truncation_by_max_events() -> None:
    lines = [json.dumps(summary_record()) for _ in range(10)]

    session = parse_session_lines(lines, session_id="s1", max_events=3)

    assert session.truncated is True
    assert len(session.summary_calls) == 3
    codes = {item.code for item in usage_engine.session_findings(session, PriceTable())}
    assert "UB006" in codes


# --------------------------------------------------------------------------
# 总览
# --------------------------------------------------------------------------


def test_scan_totals_buckets_and_ordering(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "s-big",
        [
            json.dumps(summary_record(ts=TS, total_tokens=300, cost=0.3)),
            json.dumps(summary_record(ts=TS + 60, total_tokens=100, cost=0.1, model="gpt-4o")),
            json.dumps(metadata_record(ts=TS + 60, total_tokens=100, cost=0.1, model="gpt-4o")),
        ],
    )
    write_history(sessions_dir, "s-small", [json.dumps(summary_record(total_tokens=10))])

    overview = scan_sessions(sessions_dir, now=TS + 3600)

    assert overview["totals"]["sessions"] == 2
    assert overview["totals"]["turns"] == 3
    assert overview["totals"]["total_tokens"] == 410
    assert overview["totals"]["cost_reported"] == 0.4
    assert overview["totals"]["cost_source"] == "reported"
    assert [row["session_id"] for row in overview["sessions"]] == ["s-big", "s-small"]
    assert [row["model"] for row in overview["by_model"]] == ["gpt-5", "gpt-4o"]
    assert overview["by_day"][0]["day"] == DAY
    # 模型调用次数只来自 chat.usage_metadata：gpt-4o 有 1 条，gpt-5 没有任何一条
    by_model = {row["model"]: row for row in overview["by_model"]}
    assert by_model["gpt-4o"]["calls"] == 1
    assert by_model["gpt-5"]["calls"] is None
    assert overview["data_dir"] == str(tmp_path)
    assert overview["schema_version"] == usage_engine.SCHEMA_VERSION
    assert overview["findings"] == sorted(
        overview["findings"], key=lambda item: (item["code"], item["subject"])
    )


def test_scan_days_filter_skips_old_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    old = write_history(sessions_dir, "s-old", [json.dumps(summary_record(ts=TS))])
    write_history(sessions_dir, "s-new", [json.dumps(summary_record(ts=TS + 10 * 86400))])
    os.utime(old, (TS, TS))
    folder = sessions_dir / "s-old"
    os.utime(folder, (TS, TS))

    overview = scan_sessions(sessions_dir, days=1, now=TS + 10 * 86400)

    assert overview["scanned"]["skipped_by_days"] == 1
    assert [row["session_id"] for row in overview["sessions"]] == ["s-new"]


def test_scan_respects_session_limit_and_flags_ub006(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    for index in range(3):
        write_history(sessions_dir, f"s{index}", [json.dumps(summary_record())])

    overview = scan_sessions(sessions_dir, session_limit=2, now=TS + 60)
    codes = {item["code"] for item in overview["findings"]}

    assert overview["scanned"]["sessions"] == 2
    assert "UB006" in codes


def test_scan_limit_caps_session_rows(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    for index in range(4):
        write_history(sessions_dir, f"s{index}", [json.dumps(summary_record())])

    overview = scan_sessions(sessions_dir, limit=2, now=TS + 60)

    assert len(overview["sessions"]) == 2
    assert overview["scanned"]["sessions"] == 4


def test_scan_missing_directory_reports_ub009(tmp_path: Path) -> None:
    overview = scan_sessions(tmp_path / "agent" / "sessions", now=TS)
    codes = {item["code"] for item in overview["findings"]}

    assert "UB009" in codes
    assert overview["totals"]["sessions"] == 0


def test_scan_empty_directory_reports_ub001(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    sessions_dir.mkdir(parents=True)

    overview = scan_sessions(sessions_dir, now=TS)
    codes = {item["code"] for item in overview["findings"]}

    assert "UB001" in codes


def test_scan_skips_directories_without_history(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])
    (sessions_dir / "no-history").mkdir(parents=True)

    overview = scan_sessions(sessions_dir, now=TS + 60)

    assert overview["scanned"]["skipped_no_history"] == 1
    assert overview["scanned"]["sessions"] == 1


def test_scan_reports_missing_price_table(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])

    overview = scan_sessions(sessions_dir, now=TS + 60)
    codes = {item["code"] for item in overview["findings"]}

    assert "UB002" in codes
    assert overview["totals"]["cost_total"] == 0.0
    assert overview["totals"]["cost_source"] == "missing"


def test_scan_with_price_table_estimates_cost(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "s1",
        [json.dumps(summary_record(input_tokens=1_000_000, output_tokens=0, total_tokens=1_000_000))],
    )
    prices = load_price_table(
        write_prices(tmp_path, {"models": {"gpt-5": {"input_per_1m": 5, "output_per_1m": 15}}})
    )

    overview = scan_sessions(sessions_dir, prices=prices, now=TS + 60)
    codes = {item["code"] for item in overview["findings"]}

    assert overview["totals"]["cost_estimated"] == 5.0
    assert overview["totals"]["cost_source"] == "estimated"
    assert "UB002" not in codes


def test_scan_flags_unpriced_turns(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])
    prices = load_price_table(
        write_prices(tmp_path, {"models": {"other-model": {"input_per_1m": 1, "output_per_1m": 1}}})
    )

    overview = scan_sessions(sessions_dir, prices=prices, now=TS + 60)
    codes = {item["code"] for item in overview["findings"]}

    assert "UB005" in codes
    assert overview["totals"]["unpriced_turns"] == 1


def test_scan_reports_broken_price_table(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])
    prices = load_price_table(write_prices(tmp_path, "{broken"))

    overview = scan_sessions(sessions_dir, prices=prices, now=TS + 60)
    codes = {item["code"] for item in overview["findings"]}

    assert "UB008" in codes


def test_scan_uses_placeholder_bucket_for_missing_model(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record(model=None))])

    overview = scan_sessions(sessions_dir, now=TS + 60)

    assert overview["by_model"][0]["model"] == "(未标注模型)"


def test_scan_reported_zero_cost_stays_reported(tmp_path: Path) -> None:
    """会话上报 0 费用时：不估算、总计口径仍是 reported。"""

    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "free",
        [summary_record(input_tokens=1000, output_tokens=500, cost=0.0)],
    )
    write_prices(
        tmp_path,
        {"currency": "USD", "models": {"gpt-5": {"input_per_1m": 5.0, "output_per_1m": 15.0}}},
    )
    prices = usage_engine.load_price_table(tmp_path / "usage_board" / "prices.json")

    overview = scan_sessions(sessions_dir, prices=prices, now=TS + 60)

    assert overview["totals"]["cost_total"] == 0.0
    assert overview["totals"]["cost_estimated"] == 0.0
    assert overview["totals"]["cost_source"] == "reported"
    assert overview["sessions"][0]["cost_source"] == "reported"
    assert overview["by_model"][0]["cost_source"] == "reported"
    assert "0.0（模型上报）" in render_markdown(overview)


def test_scan_counts_metadata_calls_for_models_absent_from_summary(
    tmp_path: Path,
) -> None:
    """by_model 的 calls 必须涵盖 summary 里没出现的模型，否则分桶 calls 总和 < totals.calls。"""

    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "mixed",
        [
            summary_record(model="gpt-5", total_tokens=150),
            metadata_record(model="aux-model", total_tokens=10),
        ],
    )

    overview = scan_sessions(sessions_dir, now=TS + 60)
    rows = {row["model"]: row for row in overview["by_model"]}

    assert rows["aux-model"]["calls"] == 1
    assert rows["aux-model"]["turns"] == 0
    assert rows["aux-model"]["total_tokens"] == 0
    assert sum(row["calls"] or 0 for row in overview["by_model"]) == overview["totals"]["calls"]


def test_scan_skips_expired_sessions_by_mtime_without_parsing(tmp_path: Path) -> None:
    """过期会话按 mtime 直接跳过：既不计入扫描，也不整文件解析。"""

    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "old", [summary_record(ts=TS)])
    fresh_ts = TS + 10 * 86400
    write_history(sessions_dir, "fresh", [summary_record(ts=fresh_ts)])

    overview = scan_sessions(sessions_dir, days=1, now=fresh_ts + 60)

    assert overview["scanned"]["files_read"] == 1
    assert overview["scanned"]["skipped_by_days"] == 1
    assert [row["session_id"] for row in overview["sessions"]] == ["fresh"]


def test_scan_reports_invalid_parameters_with_defaults(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])

    overview = scan_sessions(
        sessions_dir, days="bad", limit=-3, session_limit=None, now=TS + 60
    )

    assert overview["days"] == 0
    assert overview["limit"] == 1
    assert overview["session_limit"] == 1


def test_scan_is_deterministic_across_runs(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])

    first = scan_sessions(sessions_dir, now=TS + 60)
    second = scan_sessions(sessions_dir, now=TS + 60)

    assert first == second


def test_scan_carries_budget_payload(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record())])
    budget = {"configured": True, "token_budget": 100, "source": "modes.team.jiuwen_team.swarmflow_budget"}

    overview = scan_sessions(sessions_dir, now=TS + 60, budget=budget)

    assert overview["budget"] == budget


# --------------------------------------------------------------------------
# 会话详情
# --------------------------------------------------------------------------


def test_session_detail_rows_and_buckets(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "s1",
        [
            json.dumps(summary_record(total_tokens=150, cost=0.03)),
            json.dumps(summary_record(ts=TS + 5, total_tokens=50, model="gpt-4o")),
            json.dumps(context_record()),
        ],
    )

    detail = session_detail(sessions_dir, "s1")

    assert detail["totals"]["turns"] == 2
    assert detail["totals"]["total_tokens"] == 200
    assert len(detail["turns"]) == 2
    assert detail["turns"][0]["source"] == "usage_summary"
    assert [row["model"] for row in detail["models"]] == ["gpt-5", "gpt-4o"]
    assert detail["by_day"][0]["day"] == DAY
    assert detail["context"]["limit_tokens"] == 2000
    assert detail["turns_truncated"] is False


def test_session_detail_truncates_long_turn_lists(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    records = [
        summary_record(ts=TS + index, total_tokens=1)
        for index in range(usage_engine.MAX_DETAIL_TURNS + 5)
    ]
    write_history(sessions_dir, "s1", records)

    detail = session_detail(sessions_dir, "s1")

    assert detail["turns_truncated"] is True
    assert len(detail["turns"]) == usage_engine.MAX_DETAIL_TURNS
    assert detail["turns"][-1]["total_tokens"] == 1


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "../escape", "a/b", "a\\b", "x" * 129, None, "with space", "s1\u0000"],
)
def test_session_detail_rejects_unsafe_ids(tmp_path: Path, bad_id: object) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    sessions_dir.mkdir(parents=True)

    with pytest.raises(ValueError):
        session_detail(sessions_dir, bad_id)


def test_session_detail_missing_session_raises(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    sessions_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="会话不存在"):
        session_detail(sessions_dir, "nope")


def test_session_detail_accepts_dotted_and_dashed_ids(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "a.b-c_1", [json.dumps(summary_record())])

    detail = session_detail(sessions_dir, "a.b-c_1")

    assert detail["session_id"] == "a.b-c_1"


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


def _overview_with_data(tmp_path: Path) -> dict:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(
        sessions_dir,
        "s1",
        [json.dumps(summary_record(total_tokens=150, cost=0.03))],
    )
    return scan_sessions(sessions_dir, now=TS + 60)


def test_render_markdown_contains_totals_and_findings(tmp_path: Path) -> None:
    overview = _overview_with_data(tmp_path)

    text = render_markdown(overview)

    assert "# 用量与费用看板" in text
    assert "| 合计 token | 150 |" in text
    assert "UB002" in text
    assert "s1" in text


def test_render_markdown_marks_missing_cost(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [json.dumps(summary_record(total_tokens=150))])

    text = render_markdown(scan_sessions(sessions_dir, now=TS + 60))

    assert "未上报且无价格表" in text


def test_render_csv_has_header_and_quotes(tmp_path: Path) -> None:
    overview = _overview_with_data(tmp_path)
    overview["sessions"][0]["models"] = ["gpt-5", "a,b"]

    text = render_csv(overview)

    assert text.startswith("session_id,turns,calls,")
    assert '"gpt-5; a,b"' in text
    assert text.endswith("\n")


def test_build_export_supports_three_formats(tmp_path: Path) -> None:
    overview = _overview_with_data(tmp_path)

    for fmt, suffix in (("json", "json"), ("markdown", "md"), ("csv", "csv")):
        payload = build_export(overview, fmt, stamp="2026-10-22T01:02:03+08:00")
        assert payload["format"] == fmt
        assert payload["filename"].endswith("." + suffix)
        assert payload["bytes"] == len(payload["content"].encode("utf-8"))
    assert json.loads(build_export(overview, "json")["content"])["totals"]["total_tokens"] == 150


def test_build_export_rejects_unknown_format(tmp_path: Path) -> None:
    overview = _overview_with_data(tmp_path)

    with pytest.raises(ValueError, match="不支持的导出格式"):
        build_export(overview, "xml")


def test_build_export_falls_back_to_generic_filename(tmp_path: Path) -> None:
    overview = _overview_with_data(tmp_path)
    overview["generated_at"] = None

    payload = build_export(overview, "csv")

    assert payload["filename"] == "usage-board-now.csv"


# --------------------------------------------------------------------------
# 结论目录与时间工具
# --------------------------------------------------------------------------


def test_exports_do_not_leak_absolute_paths(tmp_path: Path) -> None:
    """导出内容不得包含本机绝对路径（data_dir/sessions_dir/单会话 path）。"""

    overview = _overview_with_data(tmp_path)
    root = str(tmp_path)

    json_payload = json.loads(build_export(overview, "json")["content"])
    assert "data_dir" not in json_payload
    assert "sessions_dir" not in json_payload
    assert all("path" not in row for row in json_payload["sessions"])

    for fmt in ("json", "markdown", "csv"):
        assert root not in build_export(overview, fmt)["content"]

    markdown = build_export(overview, "markdown")["content"]
    assert "…/agent/sessions" in markdown


def test_finding_catalog_is_sorted_and_unique() -> None:
    codes = [item.code for item in FINDING_DEFS]

    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    for item in FINDING_DEFS:
        assert item.severity in usage_engine.SEVERITIES
        assert item.code.startswith("UB")
        assert item.title and item.trigger and item.evidence


def test_make_finding_takes_severity_from_catalog() -> None:
    finding = usage_engine.make_finding("UB002", "x", "y")

    assert finding.severity == FINDINGS_BY_CODE["UB002"].severity
    assert finding.evidence == FINDINGS_BY_CODE["UB002"].evidence


def test_every_finding_evidence_points_into_the_repo() -> None:
    missing: list[str] = []
    self_documented: list[str] = []
    for item in FINDING_DEFS:
        for chunk in item.evidence.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.startswith("自身："):
                # 插件自定义语义（没有宿主依据）必须显式标成「自身：…」
                self_documented.append(f"{item.code}:{chunk}")
                continue
            relative, _, line_part = chunk.partition(":")
            target = REPO_ROOT / relative
            if not target.is_file():
                missing.append(f"{item.code}:{chunk} 文件不存在")
                continue
            if line_part:
                first = int(line_part.split("-")[0].split(",")[0])
                total = len(target.read_text(encoding="utf-8").splitlines())
                if first > total:
                    missing.append(f"{item.code}:{chunk} 行号越界（共 {total} 行）")

    assert missing == []
    assert "UB008:自身：" in "".join(self_documented)


def test_finding_defs_match_readme_table() -> None:
    readme = Path(usage_engine.__file__).resolve().parent / "README.md"
    if not readme.is_file():
        pytest.skip("README.md 未生成")
    text = readme.read_text(encoding="utf-8")

    for item in FINDING_DEFS:
        assert f"| {item.code} |" in text, f"{item.code} 未出现在 README 结论表里"


def test_parse_epoch_accepts_epoch_and_iso() -> None:
    assert parse_epoch(TS) == TS
    assert parse_epoch("1700000000") == TS
    assert parse_epoch("2023-11-14T22:13:20+00:00") == TS
    assert parse_epoch("not-a-time") is None
    assert parse_epoch(None) is None
    assert parse_epoch(float("nan")) is None
    assert parse_epoch(True) is None


def test_day_of_groups_by_local_calendar_day() -> None:
    assert usage_engine.day_of(TS) == DAY
    assert usage_engine.day_of(None) == ""


def test_repo_context_usage_fixture_is_parsed_like_the_host() -> None:
    fixture = json.loads(FIXTURE_CONTEXT_USAGE.read_text(encoding="utf-8"))
    record = dict(fixture)
    record["timestamp"] = TS

    session = parse_session_lines([json.dumps(record)], session_id="pr5623_contract_probe")

    assert session.context["limit_tokens"] == 2000
    assert session.context["input_tokens"] == 1000
    assert session.context["occupancy_rate"] == 0.5
    assert session.context["model"] == "probe-model"
    assert session.context["provider"] == "openai"
    assert session.billing_calls == []
