"""Tests for @agent-mention parsing and @file exclusion."""

import os
import tempfile

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class TestAtFileExcludesAgentPrefix:
    """@agent-xxx mentions should NOT be resolved as file paths."""

    @staticmethod
    def test_skips_agent_hyphen_prefix():
        content = "@agent-reviewer some text"
        result = MessageHandler.resolve_at_file_references(content, cwd="/tmp")
        assert "<file-content" not in result
        assert "@agent-reviewer" in result

    @staticmethod
    def test_skips_agent_colon_prefix():
        content = "@agent:plugin-name some text"
        result = MessageHandler.resolve_at_file_references(content, cwd="/tmp")
        assert "<file-content" not in result
        assert "@agent:plugin-name" in result

    @staticmethod
    def test_agent_and_file_in_same_message():
        """@agent-xxx and @file.py should coexist — only file resolved."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir="/tmp"
        ) as f:
            f.write("print('hello')")
            fname = os.path.basename(f.name)
        try:
            content = f"@agent-reviewer @{fname} some text"
            result = MessageHandler.resolve_at_file_references(content, cwd="/tmp")
            assert "@agent-reviewer" in result
            assert "<file-content" in result
        finally:
            os.unlink(f.name)

    @staticmethod
    def test_strip_attached_mentions_skips_agent_prefix():
        """strip_attached_mentions should not strip @agent-xxx."""
        content = "@agent-reviewer @/tmp/test.py hello"
        attachments = [{"path": "/tmp/test.py", "type": "file"}]
        result = MessageHandler.strip_attached_mentions(content, attachments, cwd="/tmp")
        assert "@agent-reviewer" in result


class TestExtractAgentMentions:
    """Test extract_agent_mentions() parsing logic."""

    @staticmethod
    def test_basic():
        result = MessageHandler.extract_agent_mentions("@agent-reviewer please review")
        assert result == ["reviewer"]

    @staticmethod
    def test_multiple():
        result = MessageHandler.extract_agent_mentions("@agent-reviewer @agent-tester check")
        assert result == ["reviewer", "tester"]

    @staticmethod
    def test_quoted_format():
        result = MessageHandler.extract_agent_mentions('@"code-reviewer (agent)" check')
        assert result == ["code-reviewer"]

    @staticmethod
    def test_plugin_scoped():
        result = MessageHandler.extract_agent_mentions("@agent-asana:project-status")
        assert result == ["asana:project-status"]

    @staticmethod
    def test_dedup():
        result = MessageHandler.extract_agent_mentions("@agent-reviewer @agent-reviewer again")
        assert result == ["reviewer"]

    @staticmethod
    def test_empty():
        result = MessageHandler.extract_agent_mentions("no mentions here")
        assert result == []

    @staticmethod
    def test_mixed_with_file():
        result = MessageHandler.extract_agent_mentions("@agent-reviewer @src/main.py review this")
        assert result == ["reviewer"]

    @staticmethod
    def test_at_agent_without_hyphen_ignored():
        """@agent (without suffix) should NOT be treated as agent mention."""
        result = MessageHandler.extract_agent_mentions("@agent is cool")
        assert result == []