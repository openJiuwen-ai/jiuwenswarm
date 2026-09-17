"""Tests for @file reference resolution (resolve_at_file_references, _resolve_structured_attachments)."""

import os
import tempfile

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class TestResolveAtFileReferences:
    """Test resolve_at_file_references() — @path → <file-content> inlining."""

    @staticmethod
    def test_inlines_file_content():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("print('hello')\nprint('world')")
            fname = f.name
        try:
            basename = os.path.basename(fname)
            cwd = os.path.dirname(fname)
            result = MessageHandler.resolve_at_file_references(f"check @{basename}", cwd=cwd)
            assert "<file-content" in result
            assert "print('hello')" in result
            assert "print('world')" in result
            assert f'path="{basename}"' in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_quoted_path_with_spaces():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="my file", delete=False
        ) as f:
            f.write("hello world")
            fname = f.name
        try:
            basename = os.path.basename(fname)
            cwd = os.path.dirname(fname)
            result = MessageHandler.resolve_at_file_references(
                f'check @"{basename}"', cwd=cwd
            )
            assert "<file-content" in result
            assert "hello world" in result
            assert f'path="{basename}"' in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_absolute_path():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("abs content")
            fname = f.name
        try:
            result = MessageHandler.resolve_at_file_references(f"check @{fname}", cwd="/tmp")
            assert "<file-content" in result
            assert "abs content" in result
            assert f'path="{fname}"' in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_home_directory_expansion():
        home = os.path.expanduser("~")
        rel_path = ".jiuwenswarm_test_at_ref.txt"
        abs_path = os.path.join(home, rel_path)
        try:
            with open(abs_path, "w") as f:
                f.write("home content")
            result = MessageHandler.resolve_at_file_references(
                f"check @~/{rel_path}", cwd="/tmp"
            )
            assert "<file-content" in result
            assert "home content" in result
        finally:
            try:
                os.unlink(abs_path)
            except OSError:
                pass

    @staticmethod
    def test_file_not_found_preserves_original():
        result = MessageHandler.resolve_at_file_references(
            "check @nonexistent_file_12345.txt", cwd="/tmp"
        )
        assert "<file-content" not in result
        assert "@nonexistent_file_12345.txt" in result

    @staticmethod
    def test_max_file_size_truncation():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write("A" * 200)
            fname = f.name
        try:
            basename = os.path.basename(fname)
            cwd = os.path.dirname(fname)
            result = MessageHandler.resolve_at_file_references(
                f"check @{basename}", cwd=cwd, max_file_size=100
            )
            assert "<file-content" in result
            assert "truncated" in result
            assert "original_size=200" in result
            assert len("A" * 100) < len(result) < len("A" * 200) + 200
        finally:
            os.unlink(fname)

    @staticmethod
    def test_no_truncation_when_max_size_none():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write("B" * 300)
            fname = f.name
        try:
            basename = os.path.basename(fname)
            cwd = os.path.dirname(fname)
            result = MessageHandler.resolve_at_file_references(
                f"check @{basename}", cwd=cwd, max_file_size=None
            )
            assert "<file-content" in result
            assert "B" * 300 in result
            assert "truncated" not in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_line_range_syntax_ignored_whole_file_read():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("line1\nline2\nline3\nline4\nline5")
            fname = f.name
        try:
            basename = os.path.basename(fname)
            cwd = os.path.dirname(fname)
            result = MessageHandler.resolve_at_file_references(
                f"check @{basename}#L2-4", cwd=cwd
            )
            assert "<file-content" in result
            assert "line1" in result
            assert "line5" in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_multiple_at_references():
        f1 = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f2 = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f1.write("first")
        f1.close()
        f2.write("second")
        f2.close()
        try:
            b1 = os.path.basename(f1.name)
            b2 = os.path.basename(f2.name)
            cwd = os.path.dirname(f1.name)
            result = MessageHandler.resolve_at_file_references(
                f"compare @{b1} with @{b2}", cwd=cwd
            )
            assert result.count("<file-content") == 2
            assert "first" in result
            assert "second" in result
        finally:
            os.unlink(f1.name)
            os.unlink(f2.name)

    @staticmethod
    def test_empty_content_returns_empty():
        result = MessageHandler.resolve_at_file_references("", cwd="/tmp")
        assert result == ""

    @staticmethod
    def test_no_at_symbol_returns_unchanged():
        result = MessageHandler.resolve_at_file_references("hello world", cwd="/tmp")
        assert result == "hello world"


class TestStripAttachedMentions:
    """Test strip_attached_mentions() — remove @path tokens already in attachments list."""

    @staticmethod
    def test_strips_attached_path():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("code")
            fname = f.name
        try:
            content = f"check @{fname} for bugs"
            attachments = [{"path": fname, "type": "file"}]
            result = MessageHandler.strip_attached_mentions(content, attachments)
            assert f"@{fname}" not in result
            assert os.path.basename(fname) in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_no_attachments_returns_unchanged():
        content = "check @somefile.py for bugs"
        result = MessageHandler.strip_attached_mentions(content, [])
        assert result == content

    @staticmethod
    def test_non_matching_path_unchanged():
        content = "check @other.py for bugs"
        attachments = [{"path": "/tmp/different.py", "type": "file"}]
        result = MessageHandler.strip_attached_mentions(content, attachments)
        assert "@other.py" in result


class TestStructuredAttachmentsIntegration:
    """Test that structured-attachment-style input is correctly resolved via public APIs."""

    @staticmethod
    def test_attachment_content_inlined_by_at_reference():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("structured content")
            fname = f.name
        try:
            content = f'@"{fname}" please review this code'
            result = MessageHandler.resolve_at_file_references(content)
            assert "<file-content" in result
            assert "structured content" in result
            assert "please review this code" in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_no_at_reference_returns_unchanged():
        result = MessageHandler.resolve_at_file_references("hello @test.py")
        assert result == "hello @test.py"

    @staticmethod
    def test_duplicate_at_references_resolve_to_single():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("dedup")
            fname = f.name
        try:
            content = f'@"{fname}" review @"{fname}" this'
            result = MessageHandler.resolve_at_file_references(content)
            assert result.count("<file-content") == 2
            assert "dedup" in result
        finally:
            os.unlink(fname)

    @staticmethod
    def test_mixed_at_file_and_plain_text():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write("plain text content")
            fname = f.name
        try:
            content = f"plain message with @{fname} embedded"
            result = MessageHandler.resolve_at_file_references(content)
            assert "<file-content" in result
            assert "plain text content" in result
            assert "plain message with" in result
        finally:
            os.unlink(fname)
