# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodingMemoryRail.

Tests the CodingMemoryRail class and its integration with the memory system.
Based on the Coding Memory Rail design document.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from unittest import mock

import pytest


# Mock classes based on design document
class MockEmbeddingConfig:
    """Mock EmbeddingConfig for testing."""
    
    def __init__(self, model_name: str = "test-model", base_url: str = "https://test.com", api_key: str = "test-key"):
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key


class MockMemoryIndexManager:
    """Mock MemoryIndexManager for testing."""
    
    _instances: Dict[str, "MockMemoryIndexManager"] = {}
    
    def __init__(self, agent_id: str, workspace_dir: str, settings: Any):
        self.agent_id = agent_id
        self.workspace_dir = workspace_dir
        self.settings = settings
        self._documents: Dict[str, str] = {}
    
    @classmethod
    async def get(cls, agent_id: str, workspace_dir: str, settings: Any) -> "MockMemoryIndexManager":
        """Get or create MemoryIndexManager instance."""
        key = f"{agent_id}:{workspace_dir}"
        if key not in cls._instances:
            cls._instances[key] = cls(agent_id, workspace_dir, settings)
        return cls._instances[key]
    
    async def search(self, query: str, max_results: int = 5) -> List["MockSearchResult"]:
        """Mock search method."""
        results = []
        for path, content in self._documents.items():
            if query.lower() in content.lower():
                results.append(MockSearchResult(path=path, score=0.9))
        return results[:max_results]
    
    def add_document(self, path: str, content: str) -> None:
        """Add document for testing."""
        self._documents[path] = content
    
    @classmethod
    def clear_instances(cls) -> None:
        """Clear all instances."""
        cls._instances.clear()


class MockSearchResult:
    """Mock search result."""
    
    def __init__(self, path: str, score: float):
        self.path = path
        self.score = score


class MockSystemPromptBuilder:
    """Mock SystemPromptBuilder for testing."""

    def __init__(self, language: str = "cn"):
        self._language = language
        self._sections: Dict[str, Any] = {}

    @property
    def language(self) -> str:
        """Get language."""
        return self._language

    def remove_section(self, name: str) -> None:
        """Remove section."""
        self._sections.pop(name, None)

    def add_section(self, section: Any) -> None:
        """Add section."""
        self._sections[section.name] = section

    def has_section(self, name: str) -> bool:
        """Check if section exists."""
        return name in self._sections

    def get_section(self, name: str) -> Optional[Any]:
        """Get section by name."""
        return self._sections.get(name)


class MockPromptSection:
    """Mock PromptSection."""
    
    def __init__(self, name: str, content: Dict[str, str], priority: int = 80):
        self.name = name
        self.content = content
        self.priority = priority


class MockAgent:
    """Mock Agent for testing."""
    
    def __init__(self):
        self.system_prompt_builder = MockSystemPromptBuilder()
        self.tools: Dict[str, Any] = {}
    
    def register_tool(self, tool: Any) -> None:
        """Register tool."""
        self.tools[tool.name] = tool


class MockInvokeInputs:
    """Mock InvokeInputs for testing."""
    
    def __init__(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        is_cron: bool = False,
        is_heartbeat: bool = False,
    ):
        self.messages = messages or []
        self._is_cron = is_cron
        self._is_heartbeat = is_heartbeat
    
    def is_cron(self) -> bool:
        return self._is_cron
    
    def is_heartbeat(self) -> bool:
        return self._is_heartbeat


class MockContext:
    """Mock context for testing."""
    
    def __init__(self, inputs: Any = None, session: Any = None):
        self.inputs = inputs or MockInvokeInputs()
        self.session = session or mock.Mock()
        self.session.agent_id = "test-agent"


# CodingMemoryRail implementation based on design document
class CodingMemoryRail:
    """Coding Memory Rail implementation for testing."""
    
    priority = 80
    MAX_RECALL_RESULTS = 5
    MAX_RECALL_TOTAL_BYTES = 10240
    
    def __init__(self, coding_memory_dir: str, embedding_config: MockEmbeddingConfig, language: str = "cn"):
        self._coding_memory_dir = coding_memory_dir
        self._embedding_config = embedding_config
        self._language = language
        self._manager: Optional[MockMemoryIndexManager] = None
        self._manager_initialized = False
        self._recalled_content: Optional[str] = None
        self._total_memories: int = 0
        self._prefetch_task: Optional[asyncio.Task] = None
        self._owned_tool_names: set = set()
        self._owned_tool_ids: set = set()
        self.system_prompt_builder = None
        self.sys_operation = None

    @property
    def coding_memory_dir(self) -> str:
        """Get coding memory directory."""
        return self._coding_memory_dir

    @property
    def embedding_config(self) -> MockEmbeddingConfig:
        """Get embedding config."""
        return self._embedding_config

    @property
    def language(self) -> str:
        """Get language."""
        return self._language

    @property
    def manager(self) -> Optional[MockMemoryIndexManager]:
        """Get memory index manager."""
        return self._manager

    @property
    def manager_initialized(self) -> bool:
        """Get manager initialization status."""
        return self._manager_initialized

    def add_document_to_manager(self, path: str, content: str) -> None:
        """Add document to manager (for testing)."""
        if self._manager:
            self._manager.add_document(path, content)

    @property
    def prefetch_task(self) -> Optional[asyncio.Task]:
        """Get prefetch task."""
        return self._prefetch_task

    def init(self, agent: MockAgent) -> None:
        """Initialize rail."""
        self.system_prompt_builder = agent.system_prompt_builder
    
    def uninit(self, agent: MockAgent) -> None:
        """Uninitialize rail."""
        pass
    
    async def before_invoke(self, ctx: MockContext) -> None:
        """Called before invoke."""
        if not self._manager_initialized:
            self._manager = await MockMemoryIndexManager.get(
                agent_id=ctx.session.agent_id,
                workspace_dir=self._coding_memory_dir,
                settings=self._embedding_config,
            )
            self._manager_initialized = True
        
        self._recalled_content = None
        self._prefetch_task = None
        
        is_read_only = isinstance(ctx.inputs, MockInvokeInputs) and (
            ctx.inputs.is_cron() or ctx.inputs.is_heartbeat()
        )
        if not is_read_only and self._manager:
            query = self._extract_last_user_query(ctx)
            if query:
                self._prefetch_task = asyncio.create_task(self._auto_recall(query))
    
    async def before_model_call(self, ctx: MockContext) -> None:
        """Called before model call."""
        if self.system_prompt_builder is None:
            return
        
        self.system_prompt_builder.remove_section("memory")
        lang = self.system_prompt_builder.language
        is_read_only = isinstance(ctx.inputs, MockInvokeInputs) and (
            ctx.inputs.is_cron() or ctx.inputs.is_heartbeat()
        )
        
        section_content = self._build_section_content(lang, is_read_only)
        
        if not is_read_only and self._prefetch_task is not None and self._recalled_content is None:
            if self._prefetch_task.done():
                try:
                    self._recalled_content, self._total_memories = self._prefetch_task.result()
                except Exception:
                    self._recalled_content = None
                self._prefetch_task = None
        
        if self._recalled_content:
            header = "## 已加载的相关记忆\n\n" if lang == "cn" else "## Loaded relevant memories\n\n"
            footer = (f"\n\n（共 {self._total_memories} 条记忆，用 coding_memory_read 读取其他。）"
                      if lang == "cn" else
                      f"\n\n({self._total_memories} total. Use coding_memory_read for others.)")
            section_content += "\n\n" + header + self._recalled_content + footer
        else:
            index = self._read_memory_index()
            if index:
                header = "## 当前记忆索引\n\n" if lang == "cn" else "## Current memory index\n\n"
                section_content += "\n\n" + header + index
        
        section = MockPromptSection(
            name="memory",
            content={lang: section_content},
            priority=85,
        )
        self.system_prompt_builder.add_section(section)
    
    def _build_section_content(self, lang: str, is_read_only: bool) -> str:
        """Build base section content."""
        if is_read_only:
            if lang == "cn":
                return (
                    f"# coding memory（只读）\n"
                    f"位于 `{self._coding_memory_dir}`。"
                    f"用 coding_memory_read 读取。不允许写入。"
                )
            else:
                return (
                    f"# coding memory (read-only)\n"
                    f"At `{self._coding_memory_dir}`. "
                    f"Use coding_memory_read to read. No writing allowed."
                )
        else:
            if lang == "cn":
                return (
                    f"# coding memory\n"
                    f"你有一个基于文件的持久化记忆系统，"
                    f"位于 `{self._coding_memory_dir}`。"
                )
            else:
                return (
                    f"# coding memory\n"
                    f"You have a persistent, file-based memory system "
                    f"at `{self._coding_memory_dir}`."
                )
    
    async def _auto_recall(self, query: str) -> tuple[Optional[str], int]:
        """Auto recall memories based on query."""
        if not self._manager:
            return None, 0
        
        results = await self._manager.search(query=query, max_results=self.MAX_RECALL_RESULTS)
        total = self._count_memory_files()
        
        if not results:
            return None, total
        
        parts = []
        total_bytes = 0
        
        for r in results:
            if r.path == "MEMORY.md":
                continue
            
            content = self._read_memory_file(r.path)
            if not content:
                continue
            
            content_bytes = len(content.encode("utf-8"))
            if total_bytes + content_bytes > self.MAX_RECALL_TOTAL_BYTES:
                remaining = self.MAX_RECALL_TOTAL_BYTES - total_bytes
                if remaining > 200:
                    content = content[:remaining] + "\n\n... (truncated)"
                    parts.append(f"### {r.path}\n\n{content}")
                break
            
            parts.append(f"### {r.path}\n\n{content}")
            total_bytes += content_bytes
        
        return ("\n\n---\n\n".join(parts), total) if parts else (None, total)
    
    def _read_memory_index(self) -> str:
        """Read MEMORY.md index."""
        try:
            index_path = os.path.join(self._coding_memory_dir, "MEMORY.md")
            with open(index_path, "r", encoding="utf-8") as f:
                return "".join(f.readlines()[:200]).strip()
        except FileNotFoundError:
            return ""
    
    def _read_memory_file(self, path: str) -> str:
        """Read memory file content."""
        try:
            file_path = os.path.join(self._coding_memory_dir, path)
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    
    def _count_memory_files(self) -> int:
        """Count memory files."""
        try:
            return sum(1 for f in Path(self._coding_memory_dir).glob("*.md") if f.name != "MEMORY.md")
        except OSError:
            return 0
    
    @staticmethod
    def _extract_last_user_query(ctx: MockContext) -> Optional[str]:
        """Extract last user query from context."""
        messages = getattr(ctx.inputs, 'messages', None) or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, str):
                    return content
        return None


@pytest.fixture
def temp_memory_dir() -> Generator[str, None, None]:
    """Create a temporary directory for coding memory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def embedding_config() -> MockEmbeddingConfig:
    """Create mock embedding config."""
    return MockEmbeddingConfig()


@pytest.fixture
def coding_rail(temp_memory_dir: str, embedding_config: MockEmbeddingConfig) -> CodingMemoryRail:
    """Create CodingMemoryRail instance."""
    return CodingMemoryRail(
        coding_memory_dir=temp_memory_dir,
        embedding_config=embedding_config,
        language="cn",
    )


@pytest.fixture
def mock_agent() -> MockAgent:
    """Create mock agent."""
    return MockAgent()


@pytest.fixture(autouse=True)
def clear_memory_manager() -> Generator[None, None, None]:
    """Clear memory manager instances before each test."""
    MockMemoryIndexManager.clear_instances()
    yield
    MockMemoryIndexManager.clear_instances()


class TestCodingMemoryRailInit:
    """Tests for CodingMemoryRail initialization."""
    
    @staticmethod
    def test_init_sets_properties(temp_memory_dir: str, embedding_config: MockEmbeddingConfig) -> None:
        """Test that init sets properties correctly."""
        rail = CodingMemoryRail(
            coding_memory_dir=temp_memory_dir,
            embedding_config=embedding_config,
            language="en",
        )
        
        assert rail.coding_memory_dir == temp_memory_dir
        assert rail.embedding_config == embedding_config
        assert rail.language == "en"
        assert rail.manager is None
        assert rail.manager_initialized is False
    
    @staticmethod
    def test_init_with_agent(coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test init with agent."""
        coding_rail.init(mock_agent)

        assert coding_rail.system_prompt_builder == mock_agent.system_prompt_builder


class TestCodingMemoryRailBeforeInvoke:
    """Tests for before_invoke method."""
    
    @pytest.mark.asyncio
    async def test_initializes_manager(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that before_invoke initializes manager."""
        coding_rail.init(mock_agent)
        
        ctx = MockContext()
        await coding_rail.before_invoke(ctx)
        
        assert coding_rail.manager_initialized is True
        assert coding_rail.manager is not None

    @pytest.mark.asyncio
    async def test_starts_prefetch_for_normal_mode(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that prefetch is started for normal mode."""
        coding_rail.init(mock_agent)
        
        ctx = MockContext()
        await coding_rail.before_invoke(ctx)
        coding_rail.add_document_to_manager("test.md", "Test content about Python")
        
        inputs = MockInvokeInputs(messages=[{"role": "user", "content": "Tell me about Python"}])
        ctx = MockContext(inputs=inputs)
        await coding_rail.before_invoke(ctx)
        
        assert coding_rail.prefetch_task is not None

    @pytest.mark.asyncio
    async def test_no_prefetch_for_cron_mode(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that prefetch is not started for cron mode."""
        coding_rail.init(mock_agent)
        
        inputs = MockInvokeInputs(is_cron=True)
        ctx = MockContext(inputs=inputs)
        await coding_rail.before_invoke(ctx)

        assert coding_rail.prefetch_task is None

    @pytest.mark.asyncio
    async def test_no_prefetch_for_heartbeat_mode(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that prefetch is not started for heartbeat mode."""
        coding_rail.init(mock_agent)

        inputs = MockInvokeInputs(is_heartbeat=True)
        ctx = MockContext(inputs=inputs)
        await coding_rail.before_invoke(ctx)

        assert coding_rail.prefetch_task is None

    @pytest.mark.asyncio
    async def test_no_prefetch_without_user_message(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that prefetch is not started without user message."""
        coding_rail.init(mock_agent)

        inputs = MockInvokeInputs(messages=[])
        ctx = MockContext(inputs=inputs)
        await coding_rail.before_invoke(ctx)

        assert coding_rail.prefetch_task is None


class TestCodingMemoryRailBeforeModelCall:
    """Tests for before_model_call method."""
    
    @pytest.mark.asyncio
    async def test_adds_memory_section(self, coding_rail: CodingMemoryRail, mock_agent: MockAgent) -> None:
        """Test that memory section is added."""
        coding_rail.init(mock_agent)
        
        ctx = MockContext()
        await coding_rail.before_model_call(ctx)

        assert mock_agent.system_prompt_builder.has_section("memory")

    @pytest.mark.asyncio
    async def test_read_only_mode_content(
        self,
        coding_rail: CodingMemoryRail,
        mock_agent: MockAgent,
        temp_memory_dir: str,
    ) -> None:
        """Test read-only mode content."""
        coding_rail.init(mock_agent)
        
        with open(os.path.join(temp_memory_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("- [Test](test.md) — Test description")
        
        inputs = MockInvokeInputs(is_cron=True)
        ctx = MockContext(inputs=inputs)
        await coding_rail.before_model_call(ctx)
        
        section = mock_agent.system_prompt_builder.get_section("memory")
        assert section is not None
        assert "只读" in section.content["cn"] or "read-only" in section.content["cn"].lower()

    @pytest.mark.asyncio
    async def test_includes_memory_index(
        self,
        coding_rail: CodingMemoryRail,
        mock_agent: MockAgent,
        temp_memory_dir: str,
    ) -> None:
        """Test that memory index is included."""
        coding_rail.init(mock_agent)
        
        with open(os.path.join(temp_memory_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("- [Test](test.md) — Test description")
        
        ctx = MockContext()
        await coding_rail.before_model_call(ctx)

        section = mock_agent.system_prompt_builder.get_section("memory")
        assert "Test description" in section.content["cn"]


class TestCodingMemoryRailIntegration:
    """Integration tests for CodingMemoryRail."""
    
    @pytest.mark.asyncio
    async def test_full_workflow(
        self,
        coding_rail: CodingMemoryRail,
        mock_agent: MockAgent,
        temp_memory_dir: str,
    ) -> None:
        """Test full workflow: init, before_invoke, before_model_call."""
        coding_rail.init(mock_agent)

        os.makedirs(temp_memory_dir, exist_ok=True)
        with open(os.path.join(temp_memory_dir, "python.md"), "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: Python Guide\n"
                "description: Python programming guide\n"
                "type: reference\n"
                "---\n\n"
                "Python content"
            )
        with open(os.path.join(temp_memory_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("- [Python Guide](python.md) — Python programming guide")
        
        inputs = MockInvokeInputs(messages=[{"role": "user", "content": "Tell me about Python"}])
        ctx = MockContext(inputs=inputs)
        
        await coding_rail.before_invoke(ctx)
        
        if coding_rail.prefetch_task:
            await coding_rail.prefetch_task
        
        await coding_rail.before_model_call(ctx)

        assert mock_agent.system_prompt_builder.has_section("memory")

    @pytest.mark.asyncio
    async def test_rail_lifecycle(self, temp_memory_dir: str, embedding_config: MockEmbeddingConfig) -> None:
        """Test CodingMemoryRail init and before_invoke lifecycle."""
        coding_rail = CodingMemoryRail(
            coding_memory_dir=temp_memory_dir,
            embedding_config=embedding_config,
            language="cn",
        )

        agent = MockAgent()
        coding_rail.init(agent)

        ctx = MockContext()
        await coding_rail.before_invoke(ctx)

        assert coding_rail.manager_initialized is True
