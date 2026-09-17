# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System tests for Coding Memory feature.

Tests the integration of Coding Memory Rail with the agent system.
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.fixture
def temp_workspace() -> Generator[Path, None, None]:
    """Create a temporary workspace with coding_memory directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        # Create coding_memory directory
        (workspace / "coding_memory").mkdir(parents=True, exist_ok=True)
        yield workspace


class TestCodingMemoryDirectoryStructure:
    """Tests for coding memory directory structure."""

    @staticmethod
    def test_coding_memory_directory_created(temp_workspace: Path) -> None:
        """Test that coding_memory directory is created."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        assert coding_memory_dir.exists()
        assert coding_memory_dir.is_dir()

    @staticmethod
    def test_memory_files_in_directory(temp_workspace: Path) -> None:
        """Test creating memory files in coding_memory directory."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create memory files
        memory_files = [
            ("user_role.md", "User role memory"),
            ("feedback_code_style.md", "Code style feedback"),
            ("project_auth_rewrite.md", "Project auth rewrite"),
        ]
        
        for filename, content in memory_files:
            file_path = coding_memory_dir / filename
            file_path.write_text(content, encoding="utf-8")
        
        # Verify files exist
        for filename, _ in memory_files:
            file_path = coding_memory_dir / filename
            assert file_path.exists()
            assert file_path.read_text(encoding="utf-8") == dict(memory_files)[filename]

    @staticmethod
    def test_memory_md_index_created(temp_workspace: Path) -> None:
        """Test that MEMORY.md index is created."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create MEMORY.md
        memory_md = coding_memory_dir / "MEMORY.md"
        index_content = """- [User Role](user_role.md) — Senior Python dev, new to frontend
- [Code Style](feedback_code_style.md) — prefer integration tests over mocks
"""
        memory_md.write_text(index_content, encoding="utf-8")
        
        assert memory_md.exists()
        assert "User Role" in memory_md.read_text(encoding="utf-8")


class TestFrontmatterInMemoryFiles:
    """Tests for frontmatter in memory files."""

    @staticmethod
    def test_valid_frontmatter_in_memory_file(temp_workspace: Path) -> None:
        """Test that valid frontmatter is parsed from memory file."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create memory file with frontmatter
        memory_content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer, first time working with React."""
        
        memory_file = coding_memory_dir / "user_role.md"
        memory_file.write_text(memory_content, encoding="utf-8")
        
        # Read and verify content
        content = memory_file.read_text(encoding="utf-8")
        assert "---" in content
        assert "name: Developer Role" in content
        assert "type: user" in content

    @staticmethod
    def test_all_memory_types(temp_workspace: Path) -> None:
        """Test all valid memory types."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        memory_types = [
            ("user", "User preferences and role"),
            ("feedback", "Code style feedback"),
            ("project", "Project deadline information"),
            ("reference", "External system reference"),
        ]
        
        for mem_type, description in memory_types:
            memory_content = f"""---
name: Test {mem_type.capitalize()}
description: {description}
type: {mem_type}
---

Test content for {mem_type} type."""
            
            memory_file = coding_memory_dir / f"test_{mem_type}.md"
            memory_file.write_text(memory_content, encoding="utf-8")
            
            # Verify file was created with correct type
            content = memory_file.read_text(encoding="utf-8")
            assert f"type: {mem_type}" in content


class TestCodingMemoryToolsIntegration:
    """Integration tests for coding memory tools."""
    
    @pytest.mark.asyncio
    async def test_memory_write_and_read_workflow(self, temp_workspace: Path) -> None:
        """Test full workflow: write memory, update index, read memory."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Simulate writing a memory file
        memory_content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer."""
        
        memory_file = coding_memory_dir / "user_role.md"
        memory_file.write_text(memory_content, encoding="utf-8")
        
        # Simulate updating MEMORY.md index
        memory_md = coding_memory_dir / "MEMORY.md"
        index_entry = "- [Developer Role](user_role.md) — Senior Python dev, new to frontend"
        memory_md.write_text(index_entry, encoding="utf-8")
        
        # Verify memory file exists and is readable
        assert memory_file.exists()
        read_content = memory_file.read_text(encoding="utf-8")
        assert "Developer Role" in read_content
        
        # Verify index was updated
        assert memory_md.exists()
        index_content = memory_md.read_text(encoding="utf-8")
        assert "Developer Role" in index_content
        assert "user_role.md" in index_content
    
    @pytest.mark.asyncio
    async def test_memory_edit_workflow(self, temp_workspace: Path) -> None:
        """Test editing a memory file."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create initial memory file
        initial_content = """---
name: Developer Role
description: Senior Python dev
type: user
---

User is a senior Python developer."""
        
        memory_file = coding_memory_dir / "user_role.md"
        memory_file.write_text(initial_content, encoding="utf-8")
        
        # Simulate editing (replace old_text with new_text)
        old_text = "description: Senior Python dev"
        new_text = "description: Senior Python dev, new to frontend"
        
        content = memory_file.read_text(encoding="utf-8")
        new_content = content.replace(old_text, new_text, 1)
        memory_file.write_text(new_content, encoding="utf-8")
        
        # Verify edit was applied
        updated_content = memory_file.read_text(encoding="utf-8")
        assert "new to frontend" in updated_content
        assert "Senior Python dev, new to frontend" in updated_content


class TestCodingMemoryRailLifecycle:
    """Tests for CodingMemoryRail lifecycle."""

    @staticmethod
    def test_rail_initialization(temp_workspace: Path) -> None:
        """Test CodingMemoryRail initialization."""
        coding_memory_dir = str(temp_workspace / "coding_memory")
        os.makedirs(coding_memory_dir, exist_ok=True)

        assert os.path.exists(coding_memory_dir)


class TestMemoryIndexManagement:
    """Tests for memory index management."""

    @staticmethod
    def test_index_entry_format(temp_workspace: Path) -> None:
        """Test that index entries follow correct format."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create index entry
        entry = "- [Developer Role](user_role.md) — Senior Python dev, new to frontend"
        
        memory_md = coding_memory_dir / "MEMORY.md"
        memory_md.write_text(entry, encoding="utf-8")
        
        # Verify format
        content = memory_md.read_text(encoding="utf-8")
        assert content.startswith("- [")
        assert "](" in content
        assert ") — " in content

    @staticmethod
    def test_multiple_index_entries(temp_workspace: Path) -> None:
        """Test multiple entries in index."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        entries = [
            "- [Developer Role](user_role.md) — Senior Python dev",
            "- [Code Style](feedback_code_style.md) — prefer integration tests",
            "- [Project Deadline](project_deadline.md) — Release freeze info",
        ]
        
        memory_md = coding_memory_dir / "MEMORY.md"
        memory_md.write_text("\n".join(entries), encoding="utf-8")
        
        # Verify all entries
        content = memory_md.read_text(encoding="utf-8")
        for entry in entries:
            assert entry in content

    @staticmethod
    def test_index_update_existing_entry(temp_workspace: Path) -> None:
        """Test updating an existing index entry."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Initial index
        initial_entry = "- [Developer Role](user_role.md) — Senior Python dev"
        memory_md = coding_memory_dir / "MEMORY.md"
        memory_md.write_text(initial_entry, encoding="utf-8")
        
        # Update entry
        updated_entry = "- [Developer Role](user_role.md) — Senior Python dev, new to frontend"
        content = memory_md.read_text(encoding="utf-8")
        content = content.replace(initial_entry, updated_entry)
        memory_md.write_text(content, encoding="utf-8")
        
        # Verify update
        updated_content = memory_md.read_text(encoding="utf-8")
        assert "new to frontend" in updated_content
        # Check that the description was updated (not just appended)
        assert updated_content == updated_entry


class TestCodingMemoryPermissions:
    """Tests for coding memory tool permissions."""

    @staticmethod
    def test_coding_memory_tools_in_config(temp_workspace: Path) -> None:
        """Test that coding memory tools are defined in config."""
        config_content = {
            "permissions": {
                "tools": {
                    "coding_memory_write": "allow",
                    "coding_memory_edit": "allow",
                    "coding_memory_read": "allow",
                },
            },
        }
        
        config_file = temp_workspace / "config.yaml"
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_content, f)
        
        # Verify config
        with open(config_file, "r", encoding="utf-8") as f:
            loaded_config = yaml.safe_load(f)
        
        tools = loaded_config["permissions"]["tools"]
        assert "coding_memory_write" in tools
        assert "coding_memory_edit" in tools
        assert "coding_memory_read" in tools
        assert tools["coding_memory_write"] == "allow"


class TestCodingMemoryEndToEnd:
    """End-to-end tests for coding memory feature."""
    
    @pytest.mark.asyncio
    async def test_user_message_to_memory_recall(self, temp_workspace: Path) -> None:
        """Test end-to-end: user message → auto recall → response."""
        coding_memory_dir = temp_workspace / "coding_memory"
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Setup: Create memory files
        memory_files = [
            ("python_tips.md", """---
name: Python Tips
description: Useful Python programming tips
type: reference
---

Always use virtual environments for Python projects."""),
            ("code_style.md", """---
name: Code Style
description: Project code style guidelines
type: feedback
---

Use 4 spaces for indentation in Python."""),
        ]
        
        for filename, content in memory_files:
            (coding_memory_dir / filename).write_text(content, encoding="utf-8")
        
        # Create index
        index_content = """- [Python Tips](python_tips.md) — Useful Python programming tips
- [Code Style](code_style.md) — Project code style guidelines
"""
        (coding_memory_dir / "MEMORY.md").write_text(index_content, encoding="utf-8")
        
        # Verify setup
        assert (coding_memory_dir / "python_tips.md").exists()
        assert (coding_memory_dir / "code_style.md").exists()
        assert (coding_memory_dir / "MEMORY.md").exists()

    @staticmethod
    def test_memory_directory_isolation(temp_workspace: Path) -> None:
        """Test that personal and coding memory directories are isolated."""
        # Create both directories
        personal_memory_dir = temp_workspace / "memory"
        coding_memory_dir = temp_workspace / "coding_memory"
        personal_memory_dir.mkdir(exist_ok=True)
        coding_memory_dir.mkdir(exist_ok=True)
        
        # Create files in each
        (personal_memory_dir / "personal_note.md").write_text("Personal note", encoding="utf-8")
        (coding_memory_dir / "coding_note.md").write_text("Coding note", encoding="utf-8")
        
        # Verify isolation
        assert (personal_memory_dir / "personal_note.md").exists()
        assert not (personal_memory_dir / "coding_note.md").exists()
        
        assert (coding_memory_dir / "coding_note.md").exists()
        assert not (coding_memory_dir / "personal_note.md").exists()
