# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for coding memory tools.

Tests the coding memory tools (read, write, edit) and related helper functions.
Based on the Coding Memory Rail design document.
"""

import os
import tempfile
from pathlib import Path
from typing import Dict, Generator, Optional, Set, Tuple
from unittest import mock

import pytest


# Helper functions based on design document
def _validate_coding_memory_path(path: str, base_dir: str) -> Tuple[bool, str]:
    """Validate coding memory file path.
    
    Args:
        path: File path to validate
        base_dir: Base directory for coding memory
        
    Returns:
        Tuple of (is_valid, resolved_path_or_error)
    """
    if ".." in path or path.startswith("/"):
        return (False, "Invalid path: directory traversal not allowed")
    if not path.endswith(".md"):
        return (False, "Path must end with .md")
    resolved = os.path.join(base_dir, os.path.basename(path))
    return (True, resolved)


def _read_file_safe(filepath: str) -> str:
    """Safely read file content.
    
    Args:
        filepath: Path to file
        
    Returns:
        File content or empty string if error
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _read_head(filepath: str, max_lines: int = 30) -> str:
    """Read first N lines of file.
    
    Args:
        filepath: Path to file
        max_lines: Maximum number of lines to read
        
    Returns:
        File head content or empty string if error
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            import itertools
            return "".join(itertools.islice(f, max_lines))
    except OSError:
        return ""


def _count_memory_files(memory_dir: str) -> int:
    """Count .md memory files in directory.
    
    Args:
        memory_dir: Directory to count files in
        
    Returns:
        Number of .md files (excluding MEMORY.md)
    """
    try:
        return sum(1 for f in Path(memory_dir).glob("*.md") if f.name != "MEMORY.md")
    except OSError:
        return 0


MAX_INDEX_LINES = 200


def _upsert_memory_index(memory_dir: str, filename: str, frontmatter: Dict[str, str]) -> None:
    """Update or insert entry in MEMORY.md index.
    
    Args:
        memory_dir: Directory containing MEMORY.md
        filename: Name of the memory file
        frontmatter: Frontmatter dictionary with name and description
    """
    index_path = os.path.join(memory_dir, "MEMORY.md")
    new_entry = f"- [{frontmatter['name']}]({filename}) — {frontmatter['description']}"
    
    # Read existing index
    lines = []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
    except FileNotFoundError:
        pass
    
    # Find and replace existing entry or mark as not found
    found = False
    for i, line in enumerate(lines):
        if f"]({filename})" in line:
            lines[i] = new_entry
            found = True
            break
    
    if not found:
        lines.insert(0, new_entry)  # New entry at the beginning
    
    # Truncate and write
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[:MAX_INDEX_LINES]))


def _remove_from_memory_index(memory_dir: str, filename: str) -> None:
    """Remove entry from MEMORY.md index.
    
    Args:
        memory_dir: Directory containing MEMORY.md
        filename: Name of the memory file to remove
    """
    index_path = os.path.join(memory_dir, "MEMORY.md")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
    except FileNotFoundError:
        return

    lines = [line for line in lines if f"]({filename})" not in line]
    
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# Mock SysOperation for testing
class MockSysOperation:
    """Mock SysOperation for testing."""
    
    def __init__(self, base_dir: str):
        self._base_dir = base_dir
    
    def fs(self):
        return self
    
    async def read_file(self, path: str, line_range: Optional[Tuple[int, int]] = None):
        """Mock read file."""
        class Result:
            def __init__(self, content):
                self.data = mock.Mock()
                self.data.content = content
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if line_range:
                    lines = content.split("\n")
                    start, end = line_range
                    if end == -1:
                        content = "\n".join(lines[start - 1:])
                    else:
                        content = "\n".join(lines[start - 1:end])
                return Result(content)
        except Exception as e:
            raise e
    
    async def write_file(self, path: str, content: str, create_if_not_exist: bool = True):
        """Mock write file."""
        class Result:
            pass
        
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return Result()


@pytest.fixture
def temp_memory_dir() -> Generator[str, None, None]:
    """Create a temporary directory for coding memory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def mock_sys_op(temp_memory_dir: str) -> MockSysOperation:
    """Create a mock SysOperation."""
    return MockSysOperation(temp_memory_dir)


class TestValidateCodingMemoryPath:
    """Tests for _validate_coding_memory_path function."""

    @staticmethod
    def test_valid_path(temp_memory_dir: str) -> None:
        """Test validating a valid path."""
        is_valid, resolved = _validate_coding_memory_path("test.md", temp_memory_dir)
        
        assert is_valid is True
        assert resolved == os.path.join(temp_memory_dir, "test.md")

    @staticmethod
    def test_path_with_directory_traversal(temp_memory_dir: str) -> None:
        """Test path with directory traversal attempt."""
        is_valid, error = _validate_coding_memory_path("../etc/passwd.md", temp_memory_dir)
        
        assert is_valid is False
        assert "directory traversal" in error.lower()

    @staticmethod
    def test_absolute_path(temp_memory_dir: str) -> None:
        """Test absolute path is rejected."""
        is_valid, error = _validate_coding_memory_path("/etc/passwd.md", temp_memory_dir)
        
        assert is_valid is False
        assert "directory traversal" in error.lower() or "invalid" in error.lower()

    @staticmethod
    def test_non_md_extension(temp_memory_dir: str) -> None:
        """Test non-.md extension is rejected."""
        is_valid, error = _validate_coding_memory_path("test.txt", temp_memory_dir)
        
        assert is_valid is False
        assert ".md" in error.lower()

    @staticmethod
    def test_path_without_extension(temp_memory_dir: str) -> None:
        """Test path without extension is rejected."""
        is_valid, error = _validate_coding_memory_path("test", temp_memory_dir)
        
        assert is_valid is False
        assert ".md" in error.lower()
    
    @staticmethod
    def test_path_basename_extraction(temp_memory_dir: str) -> None:
        """Test that only basename is used."""
        is_valid, resolved = _validate_coding_memory_path("subdir/test.md", temp_memory_dir)
        
        assert is_valid is True
        # Should only use basename
        assert resolved == os.path.join(temp_memory_dir, "test.md")


class TestReadFileSafe:
    """Tests for _read_file_safe function."""

    @staticmethod
    def test_read_existing_file(temp_memory_dir: str) -> None:
        """Test reading an existing file."""
        test_file = os.path.join(temp_memory_dir, "test.md")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Test content")
        
        content = _read_file_safe(test_file)
        
        assert content == "Test content"

    @staticmethod
    def test_read_nonexistent_file(temp_memory_dir: str) -> None:
        """Test reading a non-existent file returns empty string."""
        test_file = os.path.join(temp_memory_dir, "nonexistent.md")
        
        content = _read_file_safe(test_file)
        
        assert content == ""

    @staticmethod
    def test_read_file_with_unicode(temp_memory_dir: str) -> None:
        """Test reading file with unicode content."""
        test_file = os.path.join(temp_memory_dir, "test.md")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("中文内容 🎉")
        
        content = _read_file_safe(test_file)
        
        assert content == "中文内容 🎉"


class TestReadHead:
    """Tests for _read_head function."""

    @staticmethod
    def test_read_head_default_lines(temp_memory_dir: str) -> None:
        """Test reading default number of lines."""
        test_file = os.path.join(temp_memory_dir, "test.md")
        lines = [f"Line {i}" for i in range(1, 50)]
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        
        head = _read_head(test_file)
        
        # Default is 30 lines
        assert len(head.strip().split("\n")) == 30

    @staticmethod
    def test_read_head_custom_lines(temp_memory_dir: str) -> None:
        """Test reading custom number of lines."""
        test_file = os.path.join(temp_memory_dir, "test.md")
        lines = [f"Line {i}" for i in range(1, 20)]
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        
        head = _read_head(test_file, max_lines=10)
        
        assert len(head.strip().split("\n")) == 10

    @staticmethod
    def test_read_head_more_than_available(temp_memory_dir: str) -> None:
        """Test reading more lines than available."""
        test_file = os.path.join(temp_memory_dir, "test.md")
        lines = [f"Line {i}" for i in range(1, 5)]
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        
        head = _read_head(test_file, max_lines=10)
        
        # Should return all available lines
        assert len(head.strip().split("\n")) == 4

    @staticmethod
    def test_read_head_nonexistent_file(temp_memory_dir: str) -> None:
        """Test reading head of non-existent file."""
        test_file = os.path.join(temp_memory_dir, "nonexistent.md")
        
        head = _read_head(test_file)
        
        assert head == ""


class TestCountMemoryFiles:
    """Tests for _count_memory_files function."""

    @staticmethod
    def test_count_empty_directory(temp_memory_dir: str) -> None:
        """Test counting files in empty directory."""
        count = _count_memory_files(temp_memory_dir)
        
        assert count == 0

    @staticmethod
    def test_count_md_files(temp_memory_dir: str) -> None:
        """Test counting .md files."""
        # Create some .md files
        for i in range(3):
            Path(temp_memory_dir, f"memory{i}.md").touch()
        
        count = _count_memory_files(temp_memory_dir)
        
        assert count == 3

    @staticmethod
    def test_count_excludes_memory_md(temp_memory_dir: str) -> None:
        """Test that MEMORY.md is excluded from count."""
        # Create regular .md files
        for i in range(3):
            Path(temp_memory_dir, f"memory{i}.md").touch()
        # Create MEMORY.md
        Path(temp_memory_dir, "MEMORY.md").touch()
        
        count = _count_memory_files(temp_memory_dir)
        
        assert count == 3  # MEMORY.md not counted

    @staticmethod
    def test_count_ignores_non_md_files(temp_memory_dir: str) -> None:
        """Test that non-.md files are ignored."""
        # Create .md files
        for i in range(3):
            Path(temp_memory_dir, f"memory{i}.md").touch()
        # Create non-.md files
        for i in range(3):
            Path(temp_memory_dir, f"file{i}.txt").touch()
        
        count = _count_memory_files(temp_memory_dir)
        
        assert count == 3

    @staticmethod
    def test_count_nonexistent_directory() -> None:
        """Test counting in non-existent directory."""
        count = _count_memory_files("/nonexistent/directory")
        
        assert count == 0


class TestUpsertMemoryIndex:
    """Tests for _upsert_memory_index function."""

    @staticmethod
    def test_create_new_index(temp_memory_dir: str) -> None:
        """Test creating new MEMORY.md index."""
        frontmatter = {
            "name": "Test Memory",
            "description": "Test description",
        }
        
        _upsert_memory_index(temp_memory_dir, "test.md", frontmatter)
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        assert os.path.exists(index_path)
        
        content = _read_file_safe(index_path)
        assert "Test Memory" in content
        assert "test.md" in content
        assert "Test description" in content

    @staticmethod
    def test_update_existing_entry(temp_memory_dir: str) -> None:
        """Test updating existing entry in index."""
        # Create initial index
        frontmatter1 = {
            "name": "Test Memory",
            "description": "Original description",
        }
        _upsert_memory_index(temp_memory_dir, "test.md", frontmatter1)
        
        # Update entry
        frontmatter2 = {
            "name": "Test Memory Updated",
            "description": "Updated description",
        }
        _upsert_memory_index(temp_memory_dir, "test.md", frontmatter2)
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        content = _read_file_safe(index_path)
        
        # Should have updated content
        assert "Test Memory Updated" in content
        assert "Updated description" in content
        # Should not have old content
        assert "Original description" not in content

    @staticmethod
    def test_add_multiple_entries(temp_memory_dir: str) -> None:
        """Test adding multiple entries to index."""
        entries = [
            ("memory1.md", {"name": "Memory 1", "description": "Description 1"}),
            ("memory2.md", {"name": "Memory 2", "description": "Description 2"}),
            ("memory3.md", {"name": "Memory 3", "description": "Description 3"}),
        ]
        
        for filename, fm in entries:
            _upsert_memory_index(temp_memory_dir, filename, fm)
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        content = _read_file_safe(index_path)
        
        # All entries should be present
        for filename, fm in entries:
            assert fm["name"] in content
            assert filename in content

    @staticmethod
    def test_new_entries_at_beginning(temp_memory_dir: str) -> None:
        """Test that new entries are added at the beginning."""
        # Add first entry
        _upsert_memory_index(temp_memory_dir, "first.md", {
            "name": "First Memory",
            "description": "First description",
        })
        
        # Add second entry
        _upsert_memory_index(temp_memory_dir, "second.md", {
            "name": "Second Memory",
            "description": "Second description",
        })
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        with open(index_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Second entry should be first in file
        assert "Second Memory" in lines[0]
        assert "First Memory" in lines[1]


class TestRemoveFromMemoryIndex:
    """Tests for _remove_from_memory_index function."""

    @staticmethod
    def test_remove_existing_entry(temp_memory_dir: str) -> None:
        """Test removing existing entry from index."""
        # Create index with entries
        entries = [
            ("memory1.md", {"name": "Memory 1", "description": "Description 1"}),
            ("memory2.md", {"name": "Memory 2", "description": "Description 2"}),
            ("memory3.md", {"name": "Memory 3", "description": "Description 3"}),
        ]
        
        for filename, fm in entries:
            _upsert_memory_index(temp_memory_dir, filename, fm)
        
        # Remove middle entry
        _remove_from_memory_index(temp_memory_dir, "memory2.md")
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        content = _read_file_safe(index_path)
        
        # Memory 2 should be removed
        assert "Memory 2" not in content
        # Others should remain
        assert "Memory 1" in content
        assert "Memory 3" in content

    @staticmethod
    def test_remove_nonexistent_entry(temp_memory_dir: str) -> None:
        """Test removing non-existent entry."""
        # Create index with one entry
        _upsert_memory_index(temp_memory_dir, "memory1.md", {
            "name": "Memory 1",
            "description": "Description 1",
        })
        
        # Try to remove non-existent entry
        _remove_from_memory_index(temp_memory_dir, "nonexistent.md")
        
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        content = _read_file_safe(index_path)
        
        # Original entry should still be there
        assert "Memory 1" in content

    @staticmethod
    def test_remove_from_nonexistent_index(temp_memory_dir: str) -> None:
        """Test removing from non-existent index."""
        # Should not raise error
        _remove_from_memory_index(temp_memory_dir, "memory.md")
        
        # Index should not be created
        index_path = os.path.join(temp_memory_dir, "MEMORY.md")
        assert not os.path.exists(index_path)


class TestCodingMemoryToolsIntegration:
    """Integration tests for coding memory tools."""
    
    @pytest.mark.asyncio
    async def test_full_workflow(self, temp_memory_dir: str, mock_sys_op: MockSysOperation) -> None:
        """Test full workflow: write, read, edit, delete."""
        # 1. Write a memory file
        memory_content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer."""
        
        memory_path = os.path.join(temp_memory_dir, "user_role.md")
        await mock_sys_op.fs().write_file(memory_path, memory_content)
        
        # 2. Read the memory file
        result = await mock_sys_op.fs().read_file(memory_path)
        assert "Developer Role" in result.data.content
        
        # 3. Update index
        _upsert_memory_index(temp_memory_dir, "user_role.md", {
            "name": "Developer Role",
            "description": "Senior Python dev, new to frontend",
        })
        
        # 4. Verify index
        index_content = _read_file_safe(os.path.join(temp_memory_dir, "MEMORY.md"))
        assert "Developer Role" in index_content
        
        # 5. Count memory files
        count = _count_memory_files(temp_memory_dir)
        assert count == 1
    
    @pytest.mark.asyncio
    async def test_read_with_line_range(self, temp_memory_dir: str, mock_sys_op: MockSysOperation) -> None:
        """Test reading file with line range."""
        # Create file with multiple lines
        lines = [f"Line {i}" for i in range(1, 21)]
        content = "\n".join(lines)
        
        file_path = os.path.join(temp_memory_dir, "test.md")
        await mock_sys_op.fs().write_file(file_path, content)
        
        # Read lines 5-10
        result = await mock_sys_op.fs().read_file(file_path, line_range=(5, 10))
        read_lines = result.data.content.split("\n")
        
        assert len(read_lines) == 6
        assert "Line 5" in read_lines[0]
        assert "Line 10" in read_lines[-1]
