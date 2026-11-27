# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for metric resource labels injection."""

import pytest
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

from jiuwenclaw.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenclaw.telemetry.metrics import (
    _with_resource_labels,
    set_resource,
)


class TestResourceLabelsInjection:
    """Test _with_resource_labels and set_resource."""

    @staticmethod
    def test_with_resource_labels_basic() -> None:
        """Basic label injection with service_name only."""
        resource = Resource.create({SERVICE_NAME: "test-service"})
        set_resource(resource)
        result = _with_resource_labels({"key": "value"})
        assert result["key"] == "value"
        assert JIUWENCLAW_CLAW_ID not in result

    @staticmethod
    def test_with_resource_labels_with_claw_id() -> None:
        """Label injection with both service_name and claw_id."""
        resource = Resource.create({
            SERVICE_NAME: "test-service",
            JIUWENCLAW_CLAW_ID: "claw-123",
        })
        set_resource(resource)
        result = _with_resource_labels({"key": "value"})
        assert result[JIUWENCLAW_CLAW_ID] == "claw-123"
        assert result["key"] == "value"

    @staticmethod
    def test_with_resource_labels_preserves_existing_attrs() -> None:
        """Existing attributes should be preserved."""
        resource = Resource.create({
            SERVICE_NAME: "test-service",
            JIUWENCLAW_CLAW_ID: "claw-123",
        })
        set_resource(resource)
        result = _with_resource_labels({
            "existing_key": "existing_value",
            "another_key": "another_value",
        })
        assert result["existing_key"] == "existing_value"
        assert result["another_key"] == "another_value"

    @staticmethod
    def test_with_resource_labels_empty_attrs() -> None:
        """Empty attrs dict should only have claw_id if present."""
        resource = Resource.create({
            SERVICE_NAME: "test-service",
            JIUWENCLAW_CLAW_ID: "claw-123",
        })
        set_resource(resource)
        result = _with_resource_labels({})
        assert result[JIUWENCLAW_CLAW_ID] == "claw-123"
        assert len(result) == 1

    @staticmethod
    def test_set_resource_updates_globals() -> None:
        """set_resource should update global state."""
        resource_a = Resource.create({
            SERVICE_NAME: "service-a",
            JIUWENCLAW_CLAW_ID: "claw-a",
        })
        set_resource(resource_a)
        result_a = _with_resource_labels({})
        assert result_a[JIUWENCLAW_CLAW_ID] == "claw-a"

        resource_b = Resource.create({SERVICE_NAME: "service-b"})
        set_resource(resource_b)
        result_b = _with_resource_labels({})
        assert JIUWENCLAW_CLAW_ID not in result_b

    @staticmethod
    def test_with_resource_labels_none_resource() -> None:
        """None resource should not add any resource labels."""
        set_resource(None)
        result = _with_resource_labels({"key": "value"})
        assert JIUWENCLAW_CLAW_ID not in result
        assert result["key"] == "value"