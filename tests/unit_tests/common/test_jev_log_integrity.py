"""Regression for the September 23 probability-margin redaction failure."""

import json
import logging

import pytest

from jiuwenswarm.common import utils


@pytest.mark.parametrize("marker", ["BROWSER_POLICY_RESPONSE", "BROWSER_POLICY", "model_end"])
def test_real_decimal_shape_and_pii_survive_both_redaction_layers(marker):
    payload = {
        "confidence": 0.99, "probability_margin": 0.020000000000000018,
        "nested": {"value": 0.123456789012345678, "email": "private@example.test",
                   "id_number": 110101199001010010, "phone": "13812345678"},
        "input_tokens": 1342, "output_tokens": 19, "api_key": 'secret"with\\escape',
        "authorization_outcome": "allow", "args": ["--token", "private-token-value"],
    }
    message = f"[{marker}] " + json.dumps(payload)
    first = utils.mask_sensitive(message)
    record = logging.LogRecord("test", logging.INFO, "", 1, first, (), None)
    utils.SensitiveDataFilter().filter(record)
    parsed = json.loads(record.getMessage().split("] ", 1)[1])
    assert parsed["probability_margin"] == payload["probability_margin"]
    assert parsed["input_tokens"] == 1342 and parsed["output_tokens"] == 19
    assert parsed["nested"]["value"] == payload["nested"]["value"]
    assert parsed["nested"]["email"] == parsed["nested"]["phone"] == parsed["nested"]["id_number"] == "******"
    assert parsed["authorization_outcome"] == "allow"
    assert "private-token-value" not in record.getMessage()
    assert "secret" not in parsed["api_key"]
    assert first == record.getMessage()
