# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for frontmatter parsing and validation.

Tests the frontmatter parsing and validation functions for coding memory.
Based on the Coding Memory Rail design document.
"""

from typing import Dict, Optional, Tuple


# Import the functions to test (these will be implemented in the actual module)
# For now, we define them here based on the design document
VALID_TYPES = ("user", "feedback", "project", "reference")


def parse_frontmatter(content: str) -> Optional[Dict[str, str]]:
    """Parse frontmatter from markdown content.
    
    Args:
        content: Markdown content with frontmatter
        
    Returns:
        Dictionary of frontmatter fields or None if invalid
    """
    content = content.strip()
    if not content.startswith("---"):
        return None
    end = content.find("---", 3)
    if end == -1:
        return None
    result = {}
    for line in content[3:end].strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result if result else None


def validate_frontmatter(fm: Dict[str, str]) -> Tuple[bool, str]:
    """Validate frontmatter fields.
    
    Args:
        fm: Frontmatter dictionary
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    for field in ("name", "description", "type"):
        if not fm.get(field):
            return (False, f"Missing required field: {field}")
    if fm["type"] not in VALID_TYPES:
        return (False, f"type must be one of {VALID_TYPES}")
    return (True, "")


class TestParseFrontmatter:
    """Tests for parse_frontmatter function."""
    
    @staticmethod
    def test_valid_frontmatter() -> None:
        """Test parsing valid frontmatter."""
        content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer, first time working with React."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Developer Role"
        assert result["description"] == "Senior Python dev, new to frontend"
        assert result["type"] == "user"
    
    @staticmethod
    def test_frontmatter_without_content() -> None:
        """Test parsing frontmatter without body content."""
        content = """---
name: Test Memory
description: Test description
type: reference
---"""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test Memory"
        assert result["description"] == "Test description"
        assert result["type"] == "reference"
    
    @staticmethod
    def test_frontmatter_with_extra_whitespace() -> None:
        """Test parsing frontmatter with extra whitespace."""
        content = """---
name:   Test Memory  
description:   Test description  
type:   user  
---

Content here."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test Memory"
        assert result["description"] == "Test description"
        assert result["type"] == "user"
    
    @staticmethod
    def test_frontmatter_with_colon_in_value() -> None:
        """Test parsing frontmatter with colon in value."""
        content = """---
name: Test: Memory
description: This is a: test description
type: user
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test: Memory"
        assert result["description"] == "This is a: test description"
    
    @staticmethod
    def test_missing_start_delimiter() -> None:
        """Test parsing content without start delimiter."""
        content = """name: Test Memory
description: Test description
type: user
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is None
    
    @staticmethod
    def test_missing_end_delimiter() -> None:
        """Test parsing content without end delimiter."""
        content = """---
name: Test Memory
description: Test description
type: user

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is None
    
    @staticmethod
    def test_empty_frontmatter() -> None:
        """Test parsing empty frontmatter."""
        content = """---
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is None
    
    @staticmethod
    def test_frontmatter_with_empty_lines() -> None:
        """Test parsing frontmatter with empty lines."""
        content = """---
name: Test Memory

description: Test description

type: user
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test Memory"
        assert result["description"] == "Test description"
        assert result["type"] == "user"
    
    @staticmethod
    def test_frontmatter_with_extra_fields() -> None:
        """Test parsing frontmatter with extra fields."""
        content = """---
name: Test Memory
description: Test description
type: user
created: 2026-04-11
tags: python, coding
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test Memory"
        assert result["description"] == "Test description"
        assert result["type"] == "user"
        assert result["created"] == "2026-04-11"
        assert result["tags"] == "python, coding"
    
    @staticmethod
    def test_frontmatter_with_leading_whitespace() -> None:
        """Test parsing frontmatter with leading whitespace."""
        content = """
---
name: Test Memory
description: Test description
type: user
---

Content."""
        
        result = parse_frontmatter(content)
        
        assert result is not None
        assert result["name"] == "Test Memory"
    
    @staticmethod
    def test_frontmatter_with_only_whitespace_content() -> None:
        """Test parsing frontmatter with leading whitespace before ---
        
        Note: The current implementation strips whitespace before parsing,
        so this should successfully parse the frontmatter.
        """
        content = """   ---
name: Test Memory
description: Test description
type: user
---

Content."""
        
        result = parse_frontmatter(content)
        
        # Implementation strips whitespace, so it should parse successfully
        assert result is not None
        assert result["name"] == "Test Memory"


class TestValidateFrontmatter:
    """Tests for validate_frontmatter function."""
    
    @staticmethod
    def test_valid_frontmatter_all_fields() -> None:
        """Test validating frontmatter with all required fields."""
        fm = {
            "name": "Test Memory",
            "description": "Test description",
            "type": "user",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is True
        assert error == ""
    
    @staticmethod
    def test_valid_frontmatter_feedback_type() -> None:
        """Test validating frontmatter with feedback type."""
        fm = {
            "name": "Code Style Feedback",
            "description": "Prefer integration tests over mocks",
            "type": "feedback",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is True
        assert error == ""
    
    @staticmethod
    def test_valid_frontmatter_project_type() -> None:
        """Test validating frontmatter with project type."""
        fm = {
            "name": "Project Deadline",
            "description": "Release freeze starts on 2026-04-10",
            "type": "project",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is True
        assert error == ""
    
    @staticmethod
    def test_valid_frontmatter_reference_type() -> None:
        """Test validating frontmatter with reference type."""
        fm = {
            "name": "Jira Board",
            "description": "Link to project Jira board",
            "type": "reference",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is True
        assert error == ""
    
    @staticmethod
    def test_missing_name_field() -> None:
        """Test validating frontmatter without name field."""
        fm = {
            "description": "Test description",
            "type": "user",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "name" in error.lower()
    
    @staticmethod
    def test_missing_description_field() -> None:
        """Test validating frontmatter without description field."""
        fm = {
            "name": "Test Memory",
            "type": "user",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "description" in error.lower()
    
    @staticmethod
    def test_missing_type_field() -> None:
        """Test validating frontmatter without type field."""
        fm = {
            "name": "Test Memory",
            "description": "Test description",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "type" in error.lower()
    
    @staticmethod
    def test_empty_name_field() -> None:
        """Test validating frontmatter with empty name field."""
        fm = {
            "name": "",
            "description": "Test description",
            "type": "user",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "name" in error.lower()
    
    @staticmethod
    def test_empty_description_field() -> None:
        """Test validating frontmatter with empty description field."""
        fm = {
            "name": "Test Memory",
            "description": "",
            "type": "user",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "description" in error.lower()
    
    @staticmethod
    def test_empty_type_field() -> None:
        """Test validating frontmatter with empty type field."""
        fm = {
            "name": "Test Memory",
            "description": "Test description",
            "type": "",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "type" in error.lower()
    
    @staticmethod
    def test_invalid_type() -> None:
        """Test validating frontmatter with invalid type."""
        fm = {
            "name": "Test Memory",
            "description": "Test description",
            "type": "invalid_type",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "type" in error.lower()
        assert "user" in error or "feedback" in error or "project" in error or "reference" in error
    
    @staticmethod
    def test_type_case_sensitive() -> None:
        """Test that type validation is case sensitive."""
        fm = {
            "name": "Test Memory",
            "description": "Test description",
            "type": "USER",
        }
        
        is_valid, error = validate_frontmatter(fm)
        
        assert is_valid is False
        assert "type" in error.lower()
    
    @staticmethod
    def test_all_valid_types() -> None:
        """Test all valid memory types."""
        for mem_type in VALID_TYPES:
            fm = {
                "name": "Test Memory",
                "description": "Test description",
                "type": mem_type,
            }
            
            is_valid, error = validate_frontmatter(fm)
            
            assert is_valid is True, f"Type '{mem_type}' should be valid"
            assert error == "", f"Type '{mem_type}' should have no error"


class TestFrontmatterIntegration:
    """Integration tests for frontmatter parsing and validation."""
    
    @staticmethod
    def test_parse_and_validate_valid_frontmatter() -> None:
        """Test parsing and validating a valid frontmatter."""
        content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer."""
        
        fm = parse_frontmatter(content)
        assert fm is not None
        
        is_valid, error = validate_frontmatter(fm)
        assert is_valid is True
        assert error == ""
    
    @staticmethod
    def test_parse_and_validate_invalid_type() -> None:
        """Test parsing and validating frontmatter with invalid type."""
        content = """---
name: Developer Role
description: Senior Python dev, new to frontend
type: invalid
---

User is a senior Python developer."""
        
        fm = parse_frontmatter(content)
        assert fm is not None
        
        is_valid, error = validate_frontmatter(fm)
        assert is_valid is False
        assert "type" in error.lower()
    
    @staticmethod
    def test_parse_and_validate_missing_field() -> None:
        """Test parsing and validating frontmatter with missing field."""
        content = """---
name: Developer Role
type: user
---

User is a senior Python developer."""
        
        fm = parse_frontmatter(content)
        assert fm is not None
        
        is_valid, error = validate_frontmatter(fm)
        assert is_valid is False
        assert "description" in error.lower()
    
    @staticmethod
    def test_real_world_examples() -> None:
        """Test with real-world example frontmatters."""
        examples = [
            # User role example
            {
                "content": """---
name: Developer Role
description: Senior Python dev, new to frontend
type: user
---

User is a senior Python developer, first time working with React.""",
                "expected_valid": True,
            },
            # Feedback example
            {
                "content": """---
name: Code Style Feedback
description: Prefer integration tests over mocks
type: feedback
---

Integration tests must hit real DB.
**Why:** mock/prod divergence masked broken migration.
**How to apply:** Always use real database in tests.""",
                "expected_valid": True,
            },
            # Project example
            {
                "content": """---
name: Merge Freeze
description: Freeze merges after Thursday for mobile release
type: project
---

Merge freeze from 2026-04-10.
**Why:** Mobile release branch cut.
**How to apply:** No non-critical merges after Thursday.""",
                "expected_valid": True,
            },
            # Reference example
            {
                "content": """---
name: Jira Board
description: Link to project Jira board
type: reference
---

https://jira.company.com/projects/PROJ""",
                "expected_valid": True,
            },
        ]
        
        for example in examples:
            fm = parse_frontmatter(example["content"])
            assert fm is not None, "Frontmatter should be parsed"
            
            is_valid, error = validate_frontmatter(fm)
            assert is_valid == example["expected_valid"], f"Validation failed: {error}"
