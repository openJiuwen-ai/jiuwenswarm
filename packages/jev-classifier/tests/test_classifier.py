import asyncio
import json

import pytest

from jev_classifier import (
    ClassifierConfig,
    InputTooLargeError,
    InsufficientContextError,
    InvalidInputError,
    InvalidResponseError,
    JevClassifier,
    JevTimeoutError,
    ProviderError,
)


class StubProvider:
    def __init__(self, result="APPEND"):
        self.result = result
        self.calls = []

    async def decide(self, *, context, messages):
        self.calls.append((context, messages))
        return self.result


@pytest.mark.parametrize("label", ["APPEND", "INTERRUPT"])
def test_classifies_whole_batch_once_without_runtime_metadata(label):
    provider = StubProvider(label)
    classifier = JevClassifier(provider)
    messages = ["Use revised data.", "Keep the agreed report format."]
    assert asyncio.run(classifier.classify(context="Computing the report.", messages=messages)) == label
    assert provider.calls == [("Computing the report.", tuple(messages))]


@pytest.mark.parametrize("context,messages,error", [
    (None, ["update"], InvalidInputError),
    ("  ", ["update"], InsufficientContextError),
    ("working", "update", InvalidInputError),
    ("working", [], InvalidInputError),
    ("working", [""], InvalidInputError),
    ("working", [None], InvalidInputError),
    ("working", {"message": "update"}, InvalidInputError),
    ("\ud800", ["update"], InvalidInputError),
])
def test_invalid_input_never_calls_provider(context, messages, error):
    provider = StubProvider()
    with pytest.raises(error):
        asyncio.run(JevClassifier(provider).classify(context=context, messages=messages))
    assert provider.calls == []


def test_input_limit_counts_utf8_and_json_overhead_without_truncating():
    context, messages = "当前工作", ["修改输入"]
    size = len(json.dumps(
        {"context": context, "messages": messages}, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    provider = StubProvider()
    exact = JevClassifier(provider, ClassifierConfig(max_input_bytes=size))
    assert asyncio.run(exact.classify(context=context, messages=messages)) == "APPEND"
    too_small = JevClassifier(provider, ClassifierConfig(max_input_bytes=size - 1))
    with pytest.raises(InputTooLargeError):
        asyncio.run(too_small.classify(context=context, messages=messages))
    assert len(provider.calls) == 1


def test_message_count_limit_prevents_provider_call():
    provider = StubProvider()
    classifier = JevClassifier(provider, ClassifierConfig(max_messages=1))
    with pytest.raises(InputTooLargeError):
        asyncio.run(classifier.classify(context="working", messages=["one", "two"]))
    assert provider.calls == []


@pytest.mark.parametrize("value", [None, {}, {"action": "APPEND"}, "append", "APPEND\n", "ABORT"])
def test_custom_provider_output_must_be_exact_label(value):
    with pytest.raises(InvalidResponseError):
        asyncio.run(JevClassifier(StubProvider(value)).classify(context="working", messages=["update"]))


@pytest.mark.parametrize("timeout", [True, 0, -1, "2", float("nan"), float("inf"), 10**1000])
def test_invalid_timeout_config(timeout):
    with pytest.raises(ValueError):
        ClassifierConfig(timeout_seconds=timeout)


@pytest.mark.parametrize("name", ["max_input_bytes", "max_messages"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_invalid_input_limits(name, value):
    with pytest.raises(ValueError):
        ClassifierConfig(**{name: value})


def test_deadline_cancels_provider_and_does_not_return_append():
    async def run():
        cancelled = asyncio.Event()

        class WaitingProvider:
            async def decide(self, **kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        classifier = JevClassifier(WaitingProvider(), ClassifierConfig(timeout_seconds=0.02))
        with pytest.raises(JevTimeoutError):
            await classifier.classify(context="working", messages=["update"])
        assert cancelled.is_set()

    asyncio.run(run())


def test_caller_cancellation_propagates_and_batch_is_copied():
    async def run():
        entered = asyncio.Event()
        seen = []

        class WaitingProvider:
            async def decide(self, *, context, messages):
                seen.append(messages)
                entered.set()
                await asyncio.Event().wait()

        messages = ["original"]
        task = asyncio.create_task(JevClassifier(WaitingProvider()).classify(
            context="working", messages=messages,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        messages[0] = "changed later"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == [("original",)]

    asyncio.run(run())


def test_unexpected_provider_failure_is_sanitized():
    class BrokenProvider:
        async def decide(self, **kwargs):
            raise RuntimeError("secret-token and private-message-content")

    with pytest.raises(ProviderError) as error:
        asyncio.run(JevClassifier(BrokenProvider()).classify(context="working", messages=["update"]))
    assert str(error.value) == "decision provider failed"
    assert error.value.__suppress_context__


def test_concurrent_requests_keep_their_own_context():
    async def run():
        class Provider:
            async def decide(self, *, context, messages):
                await asyncio.sleep(0)
                return "INTERRUPT" if context == "affected work" else "APPEND"

        classifier = JevClassifier(Provider())
        results = await asyncio.gather(
            classifier.classify(context="affected work", messages=["correction"]),
            classifier.classify(context="unrelated work", messages=["correction"]),
        )
        assert results == ["INTERRUPT", "APPEND"]

    asyncio.run(run())

