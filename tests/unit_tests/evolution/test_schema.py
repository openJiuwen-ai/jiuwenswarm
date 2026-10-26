# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for evolution schema models (using openjiuwen core types)."""

import pytest

from openjiuwen.agent_evolving.checkpointing.types import (
    EvolutionLog,
    EvolutionRecord,
    EvolutionPatch,
    VALID_SECTIONS,
)
from openjiuwen.agent_evolving import EvolutionSignal, EvolutionTarget


class TestEvolutionTarget:
    """Test EvolutionTarget enum."""

    @staticmethod
    def test_evolution_target_values():
        """Test that EvolutionTarget has expected values."""
        assert EvolutionTarget.DESCRIPTION.value == "description"
        assert EvolutionTarget.BODY.value == "body"

    @staticmethod
    def test_evolution_target_comparison():
        """Test EvolutionTarget comparison."""
        assert EvolutionTarget.DESCRIPTION == EvolutionTarget.DESCRIPTION
        assert EvolutionTarget.DESCRIPTION != EvolutionTarget.BODY


class TestEvolutionPatch:
    """Test EvolutionPatch dataclass."""

    @staticmethod
    def test_create_evolution_patch():
        """Test creating an EvolutionPatch."""
        patch = EvolutionPatch(
            section="Instructions",
            action="append",
            content="Test content",
        )
        assert patch.section == "Instructions"
        assert patch.action == "append"
        assert patch.content == "Test content"
        assert patch.merge_target is None

    @staticmethod
    def test_evolution_patch_to_dict():
        """Test converting EvolutionPatch to dict."""
        patch = EvolutionPatch(
            section="Troubleshooting",
            action="append",
            content="Fix: Check configuration",
            target=EvolutionTarget.BODY,
            skip_reason="duplicate",
            merge_target="entry_123",
        )
        result = patch.to_dict()
        # Check core fields (openjiuwen may have additional script fields)
        assert result["section"] == "Troubleshooting"
        assert result["action"] == "append"
        assert result["content"] == "Fix: Check configuration"
        assert result["target"] == "body"
        assert result["skip_reason"] == "duplicate"
        assert result["merge_target"] == "entry_123"

    @staticmethod
    def test_evolution_patch_from_dict():
        """Test creating EvolutionPatch from dict."""
        data = {
            "section": "Examples",
            "action": "append",
            "content": "Example content",
            "merge_target": "entry_456",
        }
        patch = EvolutionPatch.from_dict(data)
        assert patch.section == "Examples"
        assert patch.action == "append"
        assert patch.content == "Example content"
        assert patch.merge_target == "entry_456"

    @staticmethod
    def test_valid_sections_constant():
        """Test that VALID_SECTIONS contains expected sections."""
        assert "Instructions" in VALID_SECTIONS
        assert "Examples" in VALID_SECTIONS
        assert "Troubleshooting" in VALID_SECTIONS


class TestEvolutionRecord:
    """Test EvolutionRecord dataclass."""

    @staticmethod
    def test_create_evolution_record():
        """Test creating an EvolutionRecord."""
        patch = EvolutionPatch(
            section="Instructions",
            action="append",
            content="Test instruction",
        )
        record = EvolutionRecord(
            id="ev_test123",
            source="execution_failure",
            timestamp="2025-03-20T10:00:00Z",
            context="Test context",
            change=patch,
        )
        assert record.id == "ev_test123"
        assert record.source == "execution_failure"
        assert record.context == "Test context"
        assert record.applied is False

    @staticmethod
    def test_evolution_record_make():
        """Test EvolutionRecord.make factory method."""
        patch = EvolutionPatch(
            section="Examples",
            action="append",
            content="Test example",
        )
        record = EvolutionRecord.make(
            source="user_correction",
            context="User corrected the behavior",
            change=patch,
        )
        assert record.id.startswith("ev_")
        assert len(record.id) == 11  # "ev_" + 8 hex chars
        assert record.source == "user_correction"
        assert record.context == "User corrected the behavior"
        assert record.applied is False

    @staticmethod
    def test_evolution_record_to_dict():
        """Test converting EvolutionRecord to dict."""
        patch = EvolutionPatch(
            section="Troubleshooting",
            action="append",
            content="Fix the issue",
        )
        record = EvolutionRecord(
            id="ev_abc123",
            source="execution_failure",
            timestamp="2025-03-20T10:00:00Z",
            context="Error occurred",
            change=patch,
            applied=False,
        )
        result = record.to_dict()
        assert result["id"] == "ev_abc123"
        assert result["source"] == "execution_failure"
        assert result["timestamp"] == "2025-03-20T10:00:00Z"
        assert result["context"] == "Error occurred"
        assert result["applied"] is False
        assert "change" in result

    @staticmethod
    def test_evolution_record_from_dict():
        """Test creating EvolutionRecord from dict."""
        data = {
            "id": "ev_xyz789",
            "source": "user_correction",
            "timestamp": "2025-03-20T11:00:00Z",
            "context": "User feedback",
            "change": {
                "section": "Instructions",
                "action": "append",
                "content": "New instruction",
            },
            "applied": True,
        }
        record = EvolutionRecord.from_dict(data)
        assert record.id == "ev_xyz789"
        assert record.source == "user_correction"
        assert record.context == "User feedback"
        assert record.applied is True
        assert record.change.section == "Instructions"

    @staticmethod
    def test_evolution_record_is_pending():
        """Test is_pending property."""
        patch = EvolutionPatch(
            section="Examples",
            action="append",
            content="Test",
        )
        record_pending = EvolutionRecord(
            id="ev_001",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=patch,
            applied=False,
        )
        record_applied = EvolutionRecord(
            id="ev_002",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=patch,
            applied=True,
        )
        assert record_pending.is_pending is True
        assert record_applied.is_pending is False


class TestEvolutionLog:
    """Test EvolutionLog dataclass."""

    @staticmethod
    def test_create_evolution_log():
        """Test creating an EvolutionLog."""
        log = EvolutionLog(skill_id="test-skill")
        assert log.skill_id == "test-skill"
        assert log.version == "1.0.0"
        assert len(log.entries) == 0

    @staticmethod
    def test_evolution_log_pending_entries():
        """Test pending_entries property."""
        patch1 = EvolutionPatch(
            section="Instructions",
            action="append",
            content="Pending instruction",
        )
        patch2 = EvolutionPatch(
            section="Examples",
            action="append",
            content="Applied example",
        )
        record1 = EvolutionRecord(
            id="ev_001",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test pending",
            change=patch1,
            applied=False,
        )
        record2 = EvolutionRecord(
            id="ev_002",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test applied",
            change=patch2,
            applied=True,
        )
        log = EvolutionLog(
            skill_id="test-skill",
            entries=[record1, record2],
        )
        pending = log.pending_entries
        assert len(pending) == 1
        assert pending[0].id == "ev_001"

    @staticmethod
    def test_evolution_log_to_dict():
        """Test converting EvolutionLog to dict."""
        patch = EvolutionPatch(
            section="Troubleshooting",
            action="append",
            content="Fix",
        )
        record = EvolutionRecord(
            id="ev_123",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=patch,
        )
        log = EvolutionLog(
            skill_id="test-skill",
            version="2.0.0",
            entries=[record],
        )
        result = log.to_dict()
        assert result["skill_id"] == "test-skill"
        assert result["version"] == "2.0.0"
        assert len(result["entries"]) == 1
        assert result["entries"][0]["id"] == "ev_123"

    @staticmethod
    def test_evolution_log_from_dict():
        """Test creating EvolutionLog from dict."""
        data = {
            "skill_id": "another-skill",
            "version": "1.5.0",
            "updated_at": "2025-03-20T12:00:00Z",
            "entries": [
                {
                    "id": "ev_456",
                    "source": "test",
                    "timestamp": "2025-03-20T10:00:00Z",
                    "context": "Test context",
                    "change": {
                        "section": "Instructions",
                        "action": "append",
                        "content": "Test content",
                    },
                    "applied": False,
                }
            ],
        }
        log = EvolutionLog.from_dict(data)
        assert log.skill_id == "another-skill"
        assert log.version == "1.5.0"
        assert len(log.entries) == 1
        assert log.entries[0].id == "ev_456"

    @staticmethod
    def test_evolution_log_empty():
        """Test creating empty EvolutionLog."""
        log = EvolutionLog.empty("empty-skill")
        assert log.skill_id == "empty-skill"
        assert len(log.entries) == 0


class TestEvolutionSignal:
    """Test EvolutionSignal dataclass (from openjiuwen)."""

    @staticmethod
    def test_create_evolution_signal():
        """Test creating an EvolutionSignal."""
        signal = EvolutionSignal(
            signal_type="execution_failure",
            section="Troubleshooting",
            excerpt="Error: File not found",
            skill_name="test-skill",
            context={"tool_name": "file.read"},
        )
        assert signal.signal_type == "execution_failure"
        assert signal.section == "Troubleshooting"
        assert signal.excerpt == "Error: File not found"
        assert signal.skill_name == "test-skill"
        assert signal.context == {"tool_name": "file.read"}

    @staticmethod
    def test_evolution_signal_to_dict():
        """Test converting EvolutionSignal to dict."""
        signal = EvolutionSignal(
            signal_type="user_correction",
            section="Examples",
            excerpt="You should do it this way",
            skill_name="my-skill",
            context={"source": "passive_conversation"},
        )
        result = signal.to_dict()
        # Note: to_dict returns 'type' (not 'signal_type') for compatibility
        assert result["type"] == "user_correction"
        assert result["section"] == "Examples"
        assert result["excerpt"] == "You should do it this way"
        assert result["skill_name"] == "my-skill"
        assert result["context"] == {"source": "passive_conversation"}

    @staticmethod
    def test_evolution_signal_with_optional_fields():
        """Test EvolutionSignal with None optional fields."""
        signal = EvolutionSignal(
            signal_type="execution_failure",
            section="Instructions",
            excerpt="Some error",
        )
        result = signal.to_dict()
        assert result["skill_name"] is None
        assert "context" not in result
