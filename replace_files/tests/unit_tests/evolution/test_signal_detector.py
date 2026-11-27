# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for SignalDetector."""

import pytest

from jiuwenclaw.evolution.signal_detector import SignalDetector
from jiuwenclaw.evolution.schema import EvolutionType, EvolutionSignal


class TestSignalDetector:
    """Test SignalDetector class."""

    @staticmethod
    def test_detect_no_signals():
        """Test detecting signals from messages without any signals."""
        detector = SignalDetector()
        messages = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm fine, thank you!"},
        ]
        signals = detector.detect(messages)
        assert len(signals) == 0

    @staticmethod
    def test_detect_execution_failure():
        """Test detecting execution failure signals."""
        detector = SignalDetector()
        messages = [
            {"role": "user", "content": "Run the command"},
            {"role": "assistant", "content": "Running command", "tool_calls": []},
            {
                "role": "tool",
                "name": "bash.execute",
                "content": "Error: Command failed with exit code 1",
            },
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        assert signals[0].type == "execution_failure"
        assert signals[0].section == "Troubleshooting"
        assert "Command failed" in signals[0].excerpt

    @staticmethod
    def test_detect_user_correction_chinese():
        """Test detecting user correction signals in Chinese."""
        detector = SignalDetector()
        messages = [
            {"role": "assistant", "content": "Here's the result"},
            {"role": "user", "content": "不对，应该这样做"},
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        assert signals[0].type == "user_correction"
        assert signals[0].section == "Examples"

    @staticmethod
    def test_detect_user_correction_english():
        """Test detecting user correction signals in English."""
        detector = SignalDetector()
        messages = [
            {"role": "assistant", "content": "Here's the result"},
            {"role": "user", "content": "That's wrong, you should use method X"},
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        assert signals[0].type == "user_correction"

    @staticmethod
    def test_detect_multiple_signals():
        """Test detecting multiple signals from messages."""
        detector = SignalDetector()
        messages = [
            {"role": "user", "content": "Help me"},
            {
                "role": "tool",
                "content": "Error: Connection timeout",
                "name": "http.request",
            },
            {"role": "user", "content": "不对，重新来"},
            {
                "role": "tool",
                "content": "TypeError: NoneType has no attribute",
                "name": "data.process",
            },
        ]
        signals = detector.detect(messages)
        assert len(signals) >= 2

    @staticmethod
    def test_deduplicate_signals():
        """Test signal deduplication."""
        detector = SignalDetector()
        messages = [
            {"role": "tool", "content": "Error: File not found", "name": "file.read"},
            {"role": "tool", "content": "Error: File not found", "name": "file.read"},
        ]
        signals = detector.detect(messages)
        # Should deduplicate identical signals
        assert len(signals) == 1

    @staticmethod
    def test_detect_with_skill_from_tool_calls():
        """Test detecting skill name from tool calls."""
        detector = SignalDetector(skill_dir_map={"test-skill": "/path/to/test-skill"})
        messages = [
            {
                "role": "assistant",
                "content": "Reading skill file",
                "tool_calls": [
                    {
                        "name": "file.read",
                        "arguments": '{"file_path": "/path/to/test-skill/SKILL.md"}',
                    }
                ],
            },
            {
                "role": "tool",
                "content": "Error: Permission denied",
                "name": "file.read",
            },
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        assert signals[0].skill_name == "test-skill"

    @staticmethod
    def test_ignore_tool_schema_in_content():
        """Test that tool schema content is ignored even with error keywords."""
        detector = SignalDetector()
        messages = [
            {
                "role": "tool",
                "content": "---\nname: test_tool\ndescription: This is a tool schema\n---",
                "name": "tools.list",
            },
        ]
        signals = detector.detect(messages)
        assert len(signals) == 0

    @staticmethod
    def test_extract_around_match():
        """Test excerpt extraction around matched text."""
        detector = SignalDetector()
        long_content = "Starting process... " * 10 + "ERROR: File not found" + " End of process... " * 10
        messages = [
            {"role": "tool", "content": long_content, "name": "file.read"},
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        # Excerpt should be truncated but contain the error
        assert "ERROR" in signals[0].excerpt or "error" in signals[0].excerpt.lower()

    @staticmethod
    def test_various_error_keywords():
        """Test detection of various error keywords."""
        error_keywords = [
            "exception",
            "traceback",
            "failed",
            "timeout",
            "错误",
            "异常",
            "失败",
        ]
        for keyword in error_keywords:
            detector = SignalDetector()
            messages = [
                {
                    "role": "tool",
                    "content": f"Operation failed with {keyword}",
                    "name": "test.tool",
                },
            ]
            signals = detector.detect(messages)
            assert len(signals) == 1, f"Failed to detect keyword: {keyword}"

    def test_message_with_object_attributes(self):
        """Test detecting signals from message objects with attributes."""
        detector = SignalDetector()

        class MockMessage:
            def __init__(self, role, content, **kwargs):
                self.role = role
                self.content = content
                for k, v in kwargs.items():
                    setattr(self, k, v)

            def get(self, key, default=None):
                return getattr(self, key, default)

        messages = [
            MockMessage(role="user", content="Hello"),
            MockMessage(role="tool", content="Error: Test error", name="test.tool"),
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1

    @staticmethod
    def test_signal_to_dict_structure():
        """Test that detected signals can be converted to dict properly."""
        detector = SignalDetector()
        messages = [
            {"role": "tool", "content": "Error: Connection failed", "name": "http.connect"},
        ]
        signals = detector.detect(messages)
        assert len(signals) == 1
        signal_dict = signals[0].to_dict()
        assert "type" in signal_dict
        assert "evolution_type" in signal_dict
        assert "section" in signal_dict
        assert "excerpt" in signal_dict
