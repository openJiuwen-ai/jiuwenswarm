# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for evolution schema models."""

import pytest

from jiuwenclaw.evolution.schema import (
    EvolutionType,
    EvolutionChange,
    EvolutionEntry,
    EvolutionFile,
    EvolutionSignal,
    VALID_SECTIONS,
    ExperienceTarget,
)


class TestEvolutionType:
    """Test EvolutionType enum."""

    @staticmethod
    def test_evolution_type_values():
        """Test that EvolutionType has expected values."""
        assert EvolutionType.SKILL_EXPERIENCE.value == "skill_experience"
        assert EvolutionType.NEW_SKILL.value == "new_skill"

    @staticmethod
    def test_evolution_type_comparison():
        """Test EvolutionType comparison."""
        assert EvolutionType.SKILL_EXPERIENCE == EvolutionType.SKILL_EXPERIENCE
        assert EvolutionType.SKILL_EXPERIENCE != EvolutionType.NEW_SKILL


class TestEvolutionChange:
    """Test EvolutionChange dataclass."""

    @staticmethod
    def test_create_evolution_change():
        """Test creating an EvolutionChange."""
        change = EvolutionChange(
            section="Instructions",
            action="append",
            content="Test content",
        )
        assert change.section == "Instructions"
        assert change.action == "append"
        assert change.content == "Test content"
        assert change.merge_target is None

    @staticmethod
    def test_evolution_change_to_dict():
        """Test converting EvolutionChange to dict."""
        change = EvolutionChange(
            section="Troubleshooting",
            action="append",
            content="Fix: Check configuration",
            target=ExperienceTarget.BODY,
            skip_reason="duplicate",
            merge_target="entry_123",
        )
        result = change.to_dict()
        assert result == {
            "section": "Troubleshooting",
            "action": "append",
            "content": "Fix: Check configuration",
            "target": "body",
            "skip_reason": "duplicate",
            "merge_target": "entry_123",
        }

    @staticmethod
    def test_evolution_change_from_dict():
        """Test creating EvolutionChange from dict."""
        data = {
            "section": "Examples",
            "action": "append",
            "content": "Example content",
            "merge_target": "entry_456",
        }
        change = EvolutionChange.from_dict(data)
        assert change.section == "Examples"
        assert change.action == "append"
        assert change.content == "Example content"
        assert change.merge_target == "entry_456"

    @staticmethod
    def test_evolution_change_from_dict_with_defaults():
        """Test creating EvolutionChange from dict with default values."""
        data = {"content": "Test content"}
        change = EvolutionChange.from_dict(data)
        assert change.section == "Troubleshooting"
        assert change.action == "append"
        assert change.content == "Test content"
        assert change.merge_target is None

    @staticmethod
    def test_valid_sections_constant():
        """Test that VALID_SECTIONS contains expected sections."""
        assert "Instructions" in VALID_SECTIONS
        assert "Examples" in VALID_SECTIONS
        assert "Troubleshooting" in VALID_SECTIONS


class TestEvolutionEntry:
    """Test EvolutionEntry dataclass."""

    @staticmethod
    def test_create_evolution_entry():
        """Test creating an EvolutionEntry."""
        change = EvolutionChange(
            section="Instructions",
            action="append",
            content="Test instruction",
        )
        entry = EvolutionEntry(
            id="ev_test123",
            source="execution_failure",
            timestamp="2025-03-20T10:00:00Z",
            context="Test context",
            change=change,
        )
        assert entry.id == "ev_test123"
        assert entry.source == "execution_failure"
        assert entry.context == "Test context"
        assert entry.applied is False

    @staticmethod
    def test_evolution_entry_make():
        """Test EvolutionEntry.make factory method."""
        change = EvolutionChange(
            section="Examples",
            action="append",
            content="Test example",
        )
        entry = EvolutionEntry.make(
            source="user_correction",
            context="User corrected the behavior",
            change=change,
        )
        assert entry.id.startswith("ev_")
        assert len(entry.id) == 11  # "ev_" + 8 hex chars
        assert entry.source == "user_correction"
        assert entry.context == "User corrected the behavior"
        assert entry.applied is False

    @staticmethod
    def test_evolution_entry_to_dict():
        """Test converting EvolutionEntry to dict."""
        change = EvolutionChange(
            section="Troubleshooting",
            action="append",
            content="Fix the issue",
        )
        entry = EvolutionEntry(
            id="ev_abc123",
            source="execution_failure",
            timestamp="2025-03-20T10:00:00Z",
            context="Error occurred",
            change=change,
            applied=False,
        )
        result = entry.to_dict()
        assert result["id"] == "ev_abc123"
        assert result["source"] == "execution_failure"
        assert result["timestamp"] == "2025-03-20T10:00:00Z"
        assert result["context"] == "Error occurred"
        assert result["applied"] is False
        assert "change" in result

    @staticmethod
    def test_evolution_entry_from_dict():
        """Test creating EvolutionEntry from dict."""
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
        entry = EvolutionEntry.from_dict(data)
        assert entry.id == "ev_xyz789"
        assert entry.source == "user_correction"
        assert entry.context == "User feedback"
        assert entry.applied is True
        assert entry.change.section == "Instructions"

    @staticmethod
    def test_evolution_entry_is_pending():
        """Test is_pending property."""
        change = EvolutionChange(
            section="Examples",
            action="append",
            content="Test",
        )
        entry_pending = EvolutionEntry(
            id="ev_001",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=change,
            applied=False,
        )
        entry_applied = EvolutionEntry(
            id="ev_002",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=change,
            applied=True,
        )
        assert entry_pending.is_pending is True
        assert entry_applied.is_pending is False


class TestEvolutionFile:
    """Test EvolutionFile dataclass."""

    @staticmethod
    def test_create_evolution_file():
        """Test creating an EvolutionFile."""
        efile = EvolutionFile(skill_id="test-skill")
        assert efile.skill_id == "test-skill"
        assert efile.version == "1.0.0"
        assert len(efile.entries) == 0

    @staticmethod
    def test_evolution_file_pending_entries():
        """Test pending_entries property."""
        change1 = EvolutionChange(
            section="Instructions",
            action="append",
            content="Pending instruction",
        )
        change2 = EvolutionChange(
            section="Examples",
            action="append",
            content="Applied example",
        )
        entry1 = EvolutionEntry(
            id="ev_001",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test pending",
            change=change1,
            applied=False,
        )
        entry2 = EvolutionEntry(
            id="ev_002",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test applied",
            change=change2,
            applied=True,
        )
        efile = EvolutionFile(
            skill_id="test-skill",
            entries=[entry1, entry2],
        )
        pending = efile.pending_entries
        assert len(pending) == 1
        assert pending[0].id == "ev_001"

    @staticmethod
    def test_evolution_file_to_dict():
        """Test converting EvolutionFile to dict."""
        change = EvolutionChange(
            section="Troubleshooting",
            action="append",
            content="Fix",
        )
        entry = EvolutionEntry(
            id="ev_123",
            source="test",
            timestamp="2025-03-20T10:00:00Z",
            context="Test",
            change=change,
        )
        efile = EvolutionFile(
            skill_id="test-skill",
            version="2.0.0",
            entries=[entry],
        )
        result = efile.to_dict()
        assert result["skill_id"] == "test-skill"
        assert result["version"] == "2.0.0"
        assert len(result["entries"]) == 1
        assert result["entries"][0]["id"] == "ev_123"

    @staticmethod
    def test_evolution_file_from_dict():
        """Test creating EvolutionFile from dict."""
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
        efile = EvolutionFile.from_dict(data)
        assert efile.skill_id == "another-skill"
        assert efile.version == "1.5.0"
        assert len(efile.entries) == 1
        assert efile.entries[0].id == "ev_456"

    @staticmethod
    def test_evolution_file_empty():
        """Test creating empty EvolutionFile."""
        efile = EvolutionFile.empty("empty-skill")
        assert efile.skill_id == "empty-skill"
        assert len(efile.entries) == 0


class TestEvolutionSignal:
    """Test EvolutionSignal dataclass."""

    @staticmethod
    def test_create_evolution_signal():
        """Test creating an EvolutionSignal."""
        signal = EvolutionSignal(
            type="execution_failure",
            evolution_type=EvolutionType.SKILL_EXPERIENCE,
            section="Troubleshooting",
            excerpt="Error: File not found",
            tool_name="file.read",
            skill_name="test-skill",
        )
        assert signal.type == "execution_failure"
        assert signal.evolution_type == EvolutionType.SKILL_EXPERIENCE
        assert signal.section == "Troubleshooting"
        assert signal.excerpt == "Error: File not found"
        assert signal.tool_name == "file.read"
        assert signal.skill_name == "test-skill"

    @staticmethod
    def test_evolution_signal_to_dict():
        """Test converting EvolutionSignal to dict."""
        signal = EvolutionSignal(
            type="user_correction",
            evolution_type=EvolutionType.SKILL_EXPERIENCE,
            section="Examples",
            excerpt="You should do it this way",
            skill_name="my-skill",
        )
        result = signal.to_dict()
        assert result["type"] == "user_correction"
        assert result["evolution_type"] == "skill_experience"
        assert result["section"] == "Examples"
        assert result["excerpt"] == "You should do it this way"
        assert result["skill_name"] == "my-skill"
        assert result["tool_name"] is None

    @staticmethod
    def test_evolution_signal_with_optional_fields():
        """Test EvolutionSignal with None optional fields."""
        signal = EvolutionSignal(
            type="execution_failure",
            evolution_type=EvolutionType.NEW_SKILL,
            section="Instructions",
            excerpt="Some error",
        )
        result = signal.to_dict()
        assert result["tool_name"] is None
        assert result["skill_name"] is None
