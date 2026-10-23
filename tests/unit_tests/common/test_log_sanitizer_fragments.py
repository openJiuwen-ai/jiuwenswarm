"""Regressions for wide logs containing many independent JSON fragments."""

import json
import logging
import re
import subprocess
import sys

import pytest

from jiuwenswarm.common import utils


@pytest.mark.parametrize("prefix", ["{invalid", "{'python': 1}", "[TopLogprob", "[{'python': 1}", "[[TopLogprob"])
def test_invalid_json_prefix_skips_decoder(prefix: str, monkeypatch):
    def unexpected_decode(*args):
        raise AssertionError("Invalid prefixes must not reach the JSON decoder")

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", unexpected_decode)
    assert utils._sanitize_log_text(prefix) == prefix


@pytest.mark.parametrize("depth", [1, 63, 64, 65])
def test_nested_json_prefix_preserves_values_and_secrets(depth: int):
    value = {"api_key": "nested-local-secret", "metric": 0.12345678901234568}
    text = "[ \n" * depth + json.dumps(value) + "]" * depth

    result = json.loads(utils._sanitize_log_text(text))

    for _ in range(depth):
        result = result[0]
    assert result == {"api_key": utils._masked_with_fp("nested-local-secret"), "metric": value["metric"]}


def test_prefix_lookahead_stops_after_64_containers():
    assert utils._has_possible_json_container_prefix("[" * 64 + "TopLogprob", 0) is False
    assert utils._has_possible_json_container_prefix("[" * 65 + "TopLogprob", 0) is True


@pytest.mark.parametrize("text", ["[]", "{}", "[true, false, null]", "[NaN, Infinity, -Infinity]", '[{"value": []}]'])
def test_prefix_check_preserves_decoder_supported_values(text: str):
    assert utils._sanitize_log_text(text) == json.dumps(json.loads(text), separators=(",", ":"))


@pytest.mark.parametrize("prefix", ['[true broken ', '[null broken ', '[1 broken ', '["broken" nope ', '{"broken": nope '])
def test_legal_first_token_still_uses_decoder_and_recovers(prefix: str):
    secret = "recovered-local-secret"
    sanitized = utils._sanitize_log_text(prefix + json.dumps({"api_key": secret}) + " 完整末尾")
    assert secret not in sanitized
    assert sanitized.endswith(" 完整末尾")
    assert utils._masked_with_fp(secret) in sanitized


def test_large_sdk_representation_finishes_within_deadline():
    # Run separately so the deadline also catches a blocked sanitizer worker.
    script = r'''
from jiuwenswarm.common import utils
fragment = "TopLogprob(token='piece', bytes=[65, 66], logprob=-0.12345678901234568, top_logprobs=[TopLogprob(bytes=[67, 68])]), metadata=[{'value': 1}], "
text = "content=[" + fragment * 18000 + "] complete-tail"
assert 2_400_000 < len(text) < 2_700_000
result = utils._sanitize_log_text(text)
assert result.count("bytes=[65,66]") == 18000
assert result.count("bytes=[67,68]") == 18000
assert result.count("logprob=-0.12345678901234568") == 18000
assert result.endswith("] complete-tail")
'''
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10, capture_output=True, text=True)


@pytest.mark.parametrize("fragment_count", [1100, 10000])
def test_shallow_json_fragments_preserve_tail_without_recursion(fragment_count: int):
    recursion_limit = sys.getrecursionlimit()
    text = "[1, 2, 3] " * fragment_count + "完整末尾: finished"

    sanitized = utils._sanitize_log_text(text)

    assert sanitized == "[1,2,3] " * fragment_count + "完整末尾: finished"
    assert sys.getrecursionlimit() == recursion_limit


@pytest.mark.parametrize("failed_candidates", [31, 32])
def test_candidate_limit_includes_the_32nd_start(failed_candidates: int):
    prefix = "[invalid " * failed_candidates
    fragment = '{"value": 42}'

    sanitized = utils._sanitize_log_text(prefix + fragment)

    expected = '{"value":42}' if failed_candidates == 31 else fragment
    assert sanitized == prefix + expected


def test_success_resets_candidate_limit():
    segment = "[invalid " * 31 + '{"value": 42} '

    sanitized = utils._sanitize_log_text(segment * 3 + "完整末尾")

    assert sanitized == ("[invalid " * 31 + '{"value":42} ') * 3 + "完整末尾"


def test_failed_candidates_preserve_prefix_and_absolute_unicode_offsets():
    prefix = "中文前缀 {invalid [broken 继续 "
    first = {"message": "括号 [ ] { }", "nested": [{"value": 17}]}
    second = ["中文", {"value": 23}]
    text = prefix + json.dumps(first, ensure_ascii=False) + " 中间 " + json.dumps(second) + " 完整末尾"

    sanitized = utils._sanitize_log_text(text)

    expected_first = {"message": "括号 [] {}", "nested": [{"value": 17}]}
    assert sanitized == (
        prefix + json.dumps(expected_first, ensure_ascii=False, separators=(",", ":"))
        + " 中间 " + json.dumps(second, ensure_ascii=False, separators=(",", ":")) + " 完整末尾"
    )


def test_json_inside_string_keeps_nested_brackets_and_escaping():
    inner = {"message": '中文 [括号] {以及} "引号"', "nested": [{"value": 42}]}
    outer = {"message": "前缀 " + json.dumps(inner, ensure_ascii=False) + " 后缀"}

    sanitized = json.loads(utils._sanitize_log_text(json.dumps(outer, ensure_ascii=False)))

    assert sanitized == {
        "message": "前缀 " + json.dumps(inner, ensure_ascii=False, separators=(",", ":")) + " 后缀"
    }


def _sdk_completion():
    """Build the SDK response locally; no client or network is involved."""
    from openai.types.chat import ChatCompletion

    return ChatCompletion(
        id="completion-fragment-regression",
        created=1,
        model="local-model",
        object="chat.completion",
        choices=[{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "完整回复末尾"},
            "logprobs": {"content": [
                {
                    "token": f"piece-{index:03d}",
                    "bytes": [65, index % 256],
                    "logprob": -0.1,
                    "top_logprobs": [
                        {
                            "token": f"alternative-{index:03d}-{alternative:02d}",
                            "bytes": [66, index % 256, alternative],
                            "logprob": -0.2,
                        }
                        for alternative in range(20)
                    ],
                }
                for index in range(230)
            ]},
        }],
    )


def _assert_sdk_representation_complete(sanitized: str, original: str) -> None:
    """Token strings follow existing masking rules; bytes arrays stay complete."""
    arrays = re.findall(r"bytes=(\[[0-9, ]+\])", sanitized)
    expected_arrays = re.findall(r"bytes=(\[[0-9, ]+\])", original)
    assert len(arrays) == 230 * 21
    assert [json.loads(array) for array in arrays] == [json.loads(array) for array in expected_arrays]
    assert sanitized.count("ChatCompletionTokenLogprob(") == 230
    assert sanitized.count("TopLogprob(") == 230 * 20
    assert sanitized.count("logprob=-0.1") == original.count("logprob=-0.1") == 230
    assert sanitized.count("logprob=-0.2") == original.count("logprob=-0.2") == 230 * 20
    assert sanitized.endswith(original[original.rindex("]") + 1:])
    expected_tokens = []
    for index in range(230):
        expected_tokens.append(utils._masked_with_fp(f"piece-{index:03d}"))
        for alternative in range(20):
            token = f"alternative-{index:03d}-{alternative:02d}"
            expected_tokens.append(utils._masked_with_fp(token))
    assert re.findall(r"token=(\*{6}\(fp:[0-9a-f]{8}\))", sanitized) == expected_tokens


@pytest.mark.parametrize("outer_event", [False, True])
def test_real_sdk_logprobs_representation_preserves_all_fragments(outer_event: bool):
    representation = str(_sdk_completion())
    secret = "local-only-credential"
    message = representation + '\ncredentials=' + json.dumps({"api_key": secret})
    if outer_event:
        message = json.dumps({"event": "model_end", "output": message, "tail": "外层完整末尾"})

    sanitized = utils._sanitize_log_text(message)

    if outer_event:
        event = json.loads(sanitized)
        assert event["event"] == "model_end"
        assert event["tail"] == "外层完整末尾"
        sanitized = event["output"]
    sdk_output, sensitive_json = sanitized.rsplit('\ncredentials=', 1)
    _assert_sdk_representation_complete(sdk_output, representation)
    assert json.loads(sensitive_json) == {"api_key": utils._masked_with_fp(secret)}
    assert secret not in sanitized


def test_source_record_factory_handles_real_sdk_fragments_and_restores_state():
    representation = str(_sdk_completion())
    secret = "source-local-only-credential"
    message = json.dumps({
        "event": "model_end",
        "output": representation + '\ncredentials=' + json.dumps({"api_key": secret}),
        "tail": "外层完整末尾",
    })
    factory = logging.getLogRecordFactory()
    installed = utils._source_record_masking_installed
    failures = utils._source_masking_failures
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        utils._source_record_masking_installed = False
        utils.install_source_record_masking()

        record = logging.getLogRecordFactory()(
            "openai._base_client", logging.DEBUG, __file__, 1, "%s", (message,), None,
        )

        assert record.jiuwen_sensitive_sanitized is True
        assert record.args == ()
        assert utils._source_masking_failures == failures
        event = json.loads(record.getMessage())
        assert event["tail"] == "外层完整末尾"
        sdk_output, sensitive_json = event["output"].rsplit('\ncredentials=', 1)
        _assert_sdk_representation_complete(sdk_output, representation)
        assert json.loads(sensitive_json) == {"api_key": utils._masked_with_fp(secret)}
        assert secret not in record.getMessage()
    finally:
        logging.setLogRecordFactory(factory)
        utils._source_record_masking_installed = installed
        utils._source_masking_failures = failures
