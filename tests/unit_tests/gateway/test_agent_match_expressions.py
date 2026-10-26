# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import pytest
from jiuwenswarm.gateway.config.enterprise.expressions import matches, validate_match_expr

IDENTITY = {"user_id": "u2", "group_id": "g1", "bot_id": "A"}

@pytest.mark.parametrize("expr, expected", [
    (None, True), ("", True), ([], True), ("[]", True),
    ("user_id == 'u1'", False), ("user_id != 'u1'", True),
    ("(user_id == 'u1' or user_id == 'u2') and group_id == 'g1'", True),
    ("user_id == 'u2' and group_id == 'g2'", False),
    (["user_id == 'u1'", "group_id == 'g1'"], True),
    ('["user_id == \'u1\'", "bot_id == \'B\'"]', False),
    ("user_id in ('u1', 'u2')", True),
    ("user_id in ['u1', 'u3']", False),
    ("group_id not in ['g2', 'g3']", True),
    ("user_id in ('u2')", True),
    ("user_id in ('u22')", False),
    ("user_id in 'u2xx'", False),
    ("user_id not in 'u2xx'", True),
    ("False", False), ("True", True),
    ("user_id == group_id == 'g1'", False),
])
def test_match_contract(expr, expected, resource_schema):
    resource_schema.model_validate({
        "resource_id": "A", "resource_name": "Agent A", "ref_template_id": "shared",
        "match_expr": expr,
    })
    assert matches(expr, IDENTITY) is expected

@pytest.mark.parametrize("expr", [
    "garbage", "user_id === 'u1'", "user_id > 'u1'",
    "service_id == 'x'", "user_id.lower() == 'u2'", "__import__('os')",
    {"user_id": "u1"}, 42, "[broken", ["True", "unknown"],
    ["user_id == 'nobody'", ""], [" "], [None], [[]], ["[]"],
])
def test_invalid_rules_are_rejected_on_write_and_read(expr):
    with pytest.raises(ValueError, match="invalid match_expr"):
        validate_match_expr(expr)
    with pytest.raises(ValueError, match="invalid match_expr"):
        matches(expr, IDENTITY)


@pytest.fixture(scope="module")
def resource_schema():
    import importlib
    import sys
    import types
    from pathlib import Path
    name = "_agent_authorization_receiver"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[3] /
        "packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver")]
    sys.modules[name] = package
    module = importlib.import_module(name + ".schemas.instance_resource_schemas")
    yield module.InstanceAgentResourceUpsertRequest
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]


@pytest.mark.parametrize("expr", [None, [], "user_id in ('alice')", ["group_id == 'g1'", "user_id != 'bob'"]])
def test_upsert_schema_keeps_designed_payload(resource_schema, expr):
    payload = {"resource_id": "A", "resource_name": "Agent A", "ref_template_id": "shared", "match_expr": expr}
    parsed = resource_schema.model_validate(payload)
    assert parsed.model_dump()["match_expr"] == expr


def test_upsert_rejects_invalid_expression(resource_schema):
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="invalid match_expr"):
        resource_schema.model_validate({
            "resource_id": "A", "resource_name": "Agent A", "ref_template_id": "shared",
            "match_expr": "user_id === 'alice'",
        })


@pytest.mark.parametrize("expr, message", [
    ("user_id === 'alice'", "=== / !== are not allowed; use == / !="),
    ("unknown == 'alice'", "unknown name 'unknown'; only user_id / group_id / bot_id are allowed"),
    ("user_id.lower() == 'alice'", "function calls are not supported"),
    (["user_id == 'alice'", "user_id === 'bob'"], "item[1]: === / !== are not allowed; use == / !="),
    ("user_id in [other]", "membership values must be literals"),
])
def test_upsert_preserves_validation_errors(resource_schema, expr, message):
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        resource_schema.model_validate({
            "resource_id": "A", "resource_name": "Agent A", "ref_template_id": "shared",
            "match_expr": expr,
        })
    assert message in str(exc.value)


@pytest.mark.parametrize("expr", ["unknown", "__import__('os')", ["True", "unknown"]])
def test_upsert_rejects_unknown_rules(resource_schema, expr):
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="invalid match_expr"):
        resource_schema.model_validate({
            "resource_id": "A", "resource_name": "Agent A", "ref_template_id": "shared",
            "match_expr": expr,
        })
