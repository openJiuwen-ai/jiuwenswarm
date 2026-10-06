import asyncio
import json

import pytest

from jiuwenswarm.benchmarks.duplex_live_review import LiveReviewer, observed_quotes, quoted_evidence
from jiuwenswarm.benchmarks.duplex_metrics import Events


async def settle(monitor):
    async with asyncio.timeout(2):
        while not monitor.idle:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_new_artifact_supersedes_slow_old_review(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    sent, checked = [], []

    async def review(evidence):
        checked.append(evidence["revision"])
        if evidence["revision"] == "old":
            entered.set()
            await release.wait()
            return {"status": "problem", "evidence": "Old artifact used Kafka", "correction": "Use Redis"}
        return {"status": "ok", "evidence": "", "correction": ""}

    async def send(message):
        sent.append(message)

    events = Events(tmp_path / "events.jsonl")
    monitor = LiveReviewer(review=review, send=send, events=events)
    monitor.start()
    try:
        monitor.publish({"revision": "old"})
        await entered.wait()
        monitor.publish({"revision": "intermediate"})
        monitor.publish({"revision": "fixed"})
        release.set()
        await settle(monitor)
        assert checked == ["old", "fixed"]
        assert not sent
        assert any(e["event"] == "live_review_stale" for e in events.records)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_evidence_is_required_and_duplicate_corrections_are_suppressed(tmp_path):
    result = {"status": "problem", "evidence": "", "correction": "Use Redis"}
    sent = []

    async def review(evidence):
        return result.copy()

    async def send(message):
        sent.append(message)

    monitor = LiveReviewer(review=review, send=send, events=Events(tmp_path / "events.jsonl"))
    monitor.start()
    try:
        monitor.publish({"revision": "sha"})
        await settle(monitor)
        assert not sent
        result["evidence"] = "queue.json contains Kafka"
        for _ in range(2):
            monitor.publish({"revision": "sha"})
            await settle(monitor)
            result["correction"] = "Use the required Redis backend instead"
        assert len(sent) == 1
        assert "queue.json contains Kafka" in sent[0] and "sha" in sent[0]
        assert "INTERRUPT" not in sent[0]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_review_timeout_is_logged_not_reported_as_approval(tmp_path):
    async def review(evidence):
        await asyncio.Event().wait()

    async def send(message):
        pytest.fail("A timeout must not send approval or a correction")

    events = Events(tmp_path / "events.jsonl")
    monitor = LiveReviewer(review=review, send=send, events=events, timeout=0.01)
    monitor.start()
    try:
        monitor.publish({"revision": "sha"})
        await settle(monitor)
        assert any(e["event"] == "live_review_error" for e in events.records)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_skipped_evidence_invalidates_queued_review(tmp_path):
    checked = []
    sent = []

    async def review(evidence):
        checked.append(evidence["revision"])
        return {"status": "problem", "evidence": "observed", "correction": "fix"}

    async def send(message):
        sent.append(message)

    monitor = LiveReviewer(review=review, send=send, events=Events(tmp_path / "events.jsonl"))
    monitor.start()
    try:
        monitor.publish({"revision": "old"})
        monitor.publish({"revision": "same", "review_reason": "skip", "skip_reason": "unchanged"})
        await settle(monitor)
        assert checked == [] and sent == []
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_unchanged_read_does_not_discard_review_of_current_patch(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def review(evidence):
        entered.set()
        await release.wait()
        return {"status": "problem", "evidence": "invalid patch", "correction": "fix patch"}

    async def send(message):
        sent.append(message)

    monitor = LiveReviewer(review=review, send=send, events=Events(tmp_path / "events.jsonl"))
    monitor.start()
    try:
        monitor.publish({"revision": "same"})
        await entered.wait()
        monitor.publish({"revision": "same", "review_reason": "skip", "skip_reason": "unchanged"})
        release.set()
        await settle(monitor)
        assert len(sent) == 1
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_artifact_changed_before_new_evidence_was_published(tmp_path):
    async def review(evidence):
        return {"status": "problem", "evidence": "old patch", "correction": "fix patch"}

    async def send(message):
        pytest.fail("The files changed before the completed-action event was published")

    async def is_current(evidence):
        return False

    events = Events(tmp_path / "events.jsonl")
    monitor = LiveReviewer(review=review, send=send, events=events, is_current=is_current)
    monitor.start()
    try:
        monitor.publish({"revision": "old"})
        await settle(monitor)
        assert any(e["event"] == "live_review_stale" for e in events.records)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_known_tool_error_does_not_cancel_executor_recovery(tmp_path):
    async def review(evidence):
        pytest.fail("Do not call a model to repeat the executor's own traceback")

    async def send(message):
        pytest.fail("Do not restart recovery with the same error")

    events = Events(tmp_path / "events.jsonl")
    monitor = LiveReviewer(review=review, send=send, events=events)
    monitor.start()
    try:
        monitor.publish({"revision": "original", "review_reason": "tool_failure",
                         "tool_output": "exit_code=1\nTypeError: incompatible date types"})
        await settle(monitor)
        assert monitor.calls == 0
        assert events.records[-1]["reason"] == "failure already returned to executor"
    finally:
        await monitor.close()


def test_findings_require_observed_quote_not_restatement_of_task():
    observations = {"task": "Sort data", "tool_output": "exit_code=0\nData rows: 0",
                    "analysis_batch": [{"command": "if row[2]: continue", "output": ""}]}
    assert not quoted_evidence(observations, {"evidence": "The task is not finished yet"})
    assert not quoted_evidence(observations, {"evidence": "Sort data"})
    assert quoted_evidence(observations, {"evidence": "Data rows: 0"})
    assert quoted_evidence(observations, {"evidence": "if row[2]: continue"})
    assert observed_quotes(observations, {"evidence": 'It says "Data rows: 0", which is suspicious.',
        "explanation": "Check `if row[2]: continue` and `made_up_function()`"}) == [
            "Data rows: 0", "if row[2]: continue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [False, True])
async def test_proposed_correction_requires_independent_confirmation(monkeypatch, supported):
    from types import SimpleNamespace
    import openjiuwen.agent_teams
    from jiuwenswarm.benchmarks.duplex_live_review import model_review

    responses = [{"status": "problem", "evidence": "Data rows: 0",
                  "explanation": "The filter excluded the supplied nonempty rows.",
                  "correction": "Correct the filter."},
                 {"supported": supported, "reason": "Check actual input and script",
                  "evidence": "Data rows: 0", "correction": "Correct the filter."}]
    if not supported:
        responses[1] = {"supported": False, "reason": "The alleged error is not established."}
    prompts = []

    class Agent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def run(self, prompt):
            prompts.append(prompt)
            return responses.pop(0)

    monkeypatch.setattr(openjiuwen.agent_teams, "create_tiny_agent", lambda **kwargs: Agent())
    model = SimpleNamespace(model_request_config=SimpleNamespace(model_copy=lambda **kwargs: None))
    model.model_copy = lambda **kwargs: model
    result = await model_review(model, {"tool_output": "exit_code=0\nData rows: 0"})
    assert len(prompts) == 2
    assert (result["status"] == "problem") == supported
    assert ("rejected" in result) == (not supported)
    if not supported:
        assert result["status"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote,correction,expected", [
    ("header_row = section_row + 2", "Use section_row + 1 for the header.", "problem"),
    ("header_row = section_row + 99", "Use section_row + 1 for the header.", "unverified"),
    ("header_row = section_row + 2", "", "unverified"),
])
async def test_prose_finding_is_grounded_before_delivery(monkeypatch, tmp_path, quote, correction, expected):
    from types import SimpleNamespace
    import openjiuwen.agent_teams
    from jiuwenswarm.benchmarks.duplex_live_review import model_review

    # The 13-1 run put commentary around its quote and also made an unsupported
    # deletion claim. Formatting must not discard the valid header-offset issue;
    # the outgoing message must contain only the independently verified fix.
    evidence = {"revision": "sha", "command": "header_row = section_row + 2",
                "tool_output": "Headers at row 3: [1, '02/22/2024', 'BSDER400-00', 'REAS', 200, None]"}
    finding = {"status": "problem",
               "evidence": 'The output shows: "' + evidence["tool_output"] + '" - this is a data row.',
               "explanation": "The first data row is used as headers. Old rows are not deleted.",
               "correction": "Fix the header row and delete all old rows."}
    assert not quoted_evidence(evidence, finding)
    responses = [finding, {"supported": True, "reason": "The actual header precedes row 3.",
                           "evidence": quote, "correction": correction}]
    prompts, sent = [], []

    class Agent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def run(self, prompt):
            prompts.append(json.loads(prompt))
            return responses.pop(0)

    monkeypatch.setattr(openjiuwen.agent_teams, "create_tiny_agent", lambda **kwargs: Agent())
    model = SimpleNamespace(model_request_config=SimpleNamespace(model_copy=lambda **kwargs: None))
    model.model_copy = lambda **kwargs: model

    async def send(message):
        sent.append(message)

    events = Events(tmp_path / "events.jsonl")
    monitor = LiveReviewer(review=lambda data: model_review(model, data), send=send, events=events)
    monitor.start()
    try:
        monitor.publish(evidence)
        await settle(monitor)
        assert len(prompts) == 2
        assert prompts[1]["observations"] == evidence
        assert prompts[1]["finding"]["observed_quotes"] == [evidence["tool_output"]]
        assert prompts[1]["finding"]["explanation"] == finding["explanation"]
        result = next(e["result"] for e in events.records if e["event"] == "live_review_result")
        assert result["status"] == expected
        if expected == "problem":
            assert result["evidence"] == quote
            assert len(sent) == 1 and correction in sent[0]
            assert "delete all old rows" not in sent[0]
        else:
            assert not sent
    finally:
        await monitor.close()
