from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from jiuwenswarm.symphony.agent import AgenticRetrievalToolKit, AgenticToolResult
from jiuwenswarm.agents.harness.common.tools import skill_retrieval_toolkits as _toolkit_mod


def _node(
    node_id: str,
    description: str = "",
    *,
    children: list[SimpleNamespace] | None = None,
    items: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node_id,
        description=description,
        children=children or [],
        items=items or [],
    )


def _toolkit(visible_skill_names: set[str] | None = None) -> AgenticRetrievalToolKit:
    pdf_item = SimpleNamespace(
        payload="pdf-reader",
        item_id="pdf-reader",
        label="pdf-reader",
        description=(
            "Read, summarize, and extract content from PDF files.\n"
            "Select when: 用户需要读取、总结或抽取 PDF 内容。\n"
            "Don't select when: 用户要生成 PPT、Excel 或图片。"
        ),
    )
    pdf_branch = _node(
        "OfficeDocs.Documents.Pdf",
        "PDF 阅读、抽取、转换、整理。\nSelect when: 用户处理 PDF。",
        items=[pdf_item],
    )
    docs_branch = _node(
        "OfficeDocs.Documents",
        "Word、PDF、TXT、Markdown、格式转换。",
        children=[pdf_branch],
    )
    office_branch = _node(
        "OfficeDocs",
        (
            "办公文件的生成、编辑、转换、提取、排版和结构化处理。\n"
            "Select when: 用户要处理 Word、PDF、PPT、Excel、Markdown。\n"
            "Don't select when: 用户只是联网查资料。"
        ),
        children=[docs_branch],
    )
    research_branch = _node(
        "SearchResearch",
        "网页搜索、内容抓取、新闻、论文、深度调研和知识库检索。",
    )
    root = _node("ROOT", "Root description must not be rendered.", children=[office_branch, research_branch])
    record = SimpleNamespace(
        payload="pdf-reader",
        worker_id="pdf-reader",
        name="pdf-reader",
        description=pdf_item.description,
        metadata={"skill_path": "/home/doujzc/.openjiuwen/skills/pdf-reader/SKILL.md"},
    )
    loaded_index = SimpleNamespace(tree_root=root, catalog_records=[record])
    return AgenticRetrievalToolKit(
        loaded_index=loaded_index,
        progressive_config=object(),
        top_k=5,
        visible_skill_names=visible_skill_names,
    )


def _skill_tree_from_detailed_output(result: AgenticToolResult) -> dict:
    assert "skill_tree" not in result
    assert "skill_tree" not in str(result)
    detailed_output = result.detailed_output
    assert isinstance(detailed_output, dict)
    skill_tree = detailed_output.get("skill_tree")
    assert isinstance(skill_tree, dict)
    return skill_tree


def test_skill_branch_peek_returns_branch_summary_only() -> None:
    result = _toolkit().skill_branch_peek(["ROOT"])

    assert result["success"] is True
    markdown = result["result"]
    assert markdown.startswith("# Skill Branch Peek")
    assert "## input `ROOT`" in markdown
    assert "Root description must not be rendered" not in markdown
    assert "1. `OfficeDocs`" in markdown
    assert "SearchResearch" not in markdown
    assert "covers: 3 branches, 1 skill" in markdown
    assert "SKILL.md" not in markdown
    assert "pdf-reader" not in markdown
    assert isinstance(result, AgenticToolResult)
    tree = _skill_tree_from_detailed_output(result)
    assert tree["query"] == "skill_branch_peek: ROOT"
    assert tree["candidate_count"] == 0
    assert tree["steps"][0]["event_type"] == "fragment_built"
    assert tree["steps"][0]["node_id"] == "ROOT"
    assert tree["steps"][0]["branches"][0]["id"] == "OfficeDocs"
    assert len(tree["steps"][0]["branches"]) == 1


def test_skill_branch_peek_leaf_and_unknown_errors() -> None:
    toolkit = _toolkit()

    empty_explore = toolkit.skill_branch_explore([])
    assert empty_explore["success"] is False
    assert "`node_ids` is required" in empty_explore["result"]

    root_explore = toolkit.skill_branch_explore(["ROOT"])
    assert root_explore["success"] is False
    assert "`ROOT` is already summarized" in root_explore["result"]

    leaf_result = toolkit.skill_branch_peek(["pdf-reader"])
    assert leaf_result["success"] is False
    assert leaf_result["result"].startswith("# Skill Tree Node Error")
    assert "`pdf-reader` is a skill id, not a branch id" in leaf_result["result"]

    missing_result = toolkit.skill_branch_explore(["OfficeDocs.Unknown"])
    assert missing_result["success"] is False
    assert missing_result["result"].startswith("# Skill Tree Node Not Found")
    assert "`OfficeDocs.Unknown`" in missing_result["result"]


def test_skill_branch_explore_renders_next_boundary(monkeypatch) -> None:
    terminal = SimpleNamespace(
        canonical_id="pdf-reader",
        selectable_canonical_id="pdf-reader",
        children=[],
    )
    pdf_branch = SimpleNamespace(
        canonical_id="node::OfficeDocs.Documents.Pdf",
        selectable_canonical_id="",
        description=(
            "PDF 阅读、抽取、转换、整理。\n"
            "Select when: 用户提供 PDF。\n"
            "Don't select when: 用户主要处理 Excel。"
        ),
        children=[terminal],
    )
    resolution = SimpleNamespace(
        canonical_id="pdf-reader",
        is_terminal=True,
        item=SimpleNamespace(
            payload="pdf-reader",
            item_id="pdf-reader",
            label="pdf-reader",
            description=(
                "Read, summarize, and extract content from PDF files.\n"
                "Select when: 用户需要读取、总结或抽取 PDF 内容。\n"
                "Don't select when: 用户要生成 PPT、Excel 或图片。"
            ),
        ),
        label="pdf-reader",
        description="",
    )
    fragment = SimpleNamespace(
        root=SimpleNamespace(children=[pdf_branch]),
        code_to_resolution={"A": resolution},
    )

    class FakeCurrentSubtreeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def get_current_subtree(cursor):
            assert cursor.node.node_id == "OfficeDocs.Documents"
            assert cursor.branch_path == ("ROOT", "OfficeDocs", "OfficeDocs.Documents")
            return SimpleNamespace(fragment=fragment)

    class FakeSearchCursor:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    retrieval_module = types.ModuleType("retrieval")
    tree_module = types.ModuleType("retrieval.tree")
    subtree_module = types.ModuleType("retrieval.tree.subtree")
    subtree_module.DefaultCurrentSubtreeProvider = FakeCurrentSubtreeProvider
    types_module = types.ModuleType("retrieval.tree.types")
    types_module.SearchCursor = FakeSearchCursor

    monkeypatch.setitem(sys.modules, "retrieval", retrieval_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree", tree_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree.subtree", subtree_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree.types", types_module)

    result = _toolkit().skill_branch_explore(["OfficeDocs.Documents"])

    assert result["success"] is True
    markdown = result["result"]
    assert markdown.startswith("# Skill Branch Explore")
    assert "## input `OfficeDocs.Documents`" in markdown
    assert "### branch `OfficeDocs.Documents.Pdf`" in markdown
    assert "pdf-reader" not in markdown
    assert "Worker id" not in markdown
    assert "SKILL.md" not in markdown
    assert "Terminal Skill Candidates" not in markdown
    assert "Covers:" not in markdown
    assert isinstance(result, AgenticToolResult)
    tree = _skill_tree_from_detailed_output(result)
    assert tree["query"] == "skill_branch_explore: OfficeDocs.Documents"
    assert tree["candidate_count"] == 0
    assert [step["event_type"] for step in tree["steps"]] == ["fragment_built", "fragment_continue"]
    assert tree["steps"][1]["selected"][0]["id"] == "OfficeDocs.Documents.Pdf"


def test_skill_branch_explore_renders_terminal_skills_with_read_info(monkeypatch) -> None:
    terminal = SimpleNamespace(
        canonical_id="pdf-reader",
        selectable_canonical_id="pdf-reader",
        children=[],
    )
    resolution = SimpleNamespace(
        canonical_id="pdf-reader",
        is_terminal=True,
        item=SimpleNamespace(
            payload="pdf-reader",
            item_id="pdf-reader",
            label="pdf-reader",
            description=(
                "Read, summarize, and extract content from PDF files.\n"
                "Select when: 用户需要读取、总结或抽取 PDF 内容。\n"
                "Don't select when: 用户要生成 PPT、Excel 或图片。"
            ),
        ),
        label="pdf-reader",
        description="",
    )
    fragment = SimpleNamespace(
        root=SimpleNamespace(children=[terminal]),
        code_to_resolution={"A": resolution},
    )

    class FakeCurrentSubtreeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def get_current_subtree(cursor):
            assert cursor.node.node_id == "OfficeDocs.Documents.Pdf"
            return SimpleNamespace(fragment=fragment)

    class FakeSearchCursor:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    retrieval_module = types.ModuleType("retrieval")
    tree_module = types.ModuleType("retrieval.tree")
    subtree_module = types.ModuleType("retrieval.tree.subtree")
    subtree_module.DefaultCurrentSubtreeProvider = FakeCurrentSubtreeProvider
    types_module = types.ModuleType("retrieval.tree.types")
    types_module.SearchCursor = FakeSearchCursor

    monkeypatch.setitem(sys.modules, "retrieval", retrieval_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree", tree_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree.subtree", subtree_module)
    monkeypatch.setitem(sys.modules, "retrieval.tree.types", types_module)

    result = _toolkit().skill_branch_explore(["OfficeDocs.Documents.Pdf"])

    assert result["success"] is True
    markdown = result["result"]
    assert "### skills" in markdown
    assert "Candidate installed skills, not branch ids." in markdown
    assert "Shortlist by Name/Description" in markdown
    assert "read SKILL.md only after choosing a skill as likely useful" in markdown
    assert "1. `pdf-reader`" in markdown
    assert "- Description: Read, summarize, and extract content from PDF files." in markdown
    assert "- SKILL.md: `/home/doujzc/.openjiuwen/skills/pdf-reader/SKILL.md`" in markdown
    assert "Worker id" not in markdown
    assert "- Select when:" not in markdown
    assert "- Don't select when:" not in markdown
    assert "- Read:" not in markdown
    assert isinstance(result, AgenticToolResult)
    tree = _skill_tree_from_detailed_output(result)
    assert tree["query"] == "skill_branch_explore: OfficeDocs.Documents.Pdf"
    assert tree["candidate_count"] == 1
    assert [step["event_type"] for step in tree["steps"]] == [
        "fragment_built",
        "fragment_continue",
        "search_complete",
    ]
    assert tree["steps"][1]["leaves"][0]["id"] == "pdf-reader"
    assert tree["candidates"][0]["label"] == "pdf-reader"
    assert tree["candidates"][0]["worker_id"] == "pdf-reader"


def test_root_prompt_contains_first_level_categories_only() -> None:
    markdown = _toolkit().root_prompt_markdown(language="cn")

    assert markdown.startswith("# Agentic 技能检索")
    assert "技能目录查阅工具" in markdown
    assert "请先根据目录结果筛选候选技能" in markdown
    assert "只有当某个技能看起来确实需要使用时，才读取其 SKILL.md" in markdown
    assert "不要在未使用至少一个目录工具前直接返回" in markdown
    assert "使用技能目录时，先根据任务识别" in markdown
    assert "主能力分支" in markdown
    assert "输入格式分支" in markdown
    assert "输出格式分支" in markdown
    assert "验证/评估/测试分支" in markdown
    assert "必要的执行环境分支" in markdown
    assert "优先探索主能力分支" in markdown
    assert "任务约束会改变技能选择、执行方案或验收标准" in markdown
    assert "输入/输出形式、验证要求、执行约束、依赖条件" in markdown
    assert "只提供背景且不影响技能选择" in markdown
    assert "任务明确点名框架、平台、库、运行时或云服务" not in markdown
    assert "SaaS API" not in markdown
    assert "优先选择精确技能" in markdown
    assert "宽泛概览技能只在需要补充必要背景时使用" in markdown
    assert "## 第一层分类" in markdown
    assert "不要把展示序号当作 `node_ids`" in markdown
    assert "不要把 `ROOT` 作为首轮 `skill_branch_explore` 的输入" in markdown
    assert "不要继续展开技能名" in markdown
    assert "只有当某个技能看起来需要使用时，才读取其 SKILL.md" in markdown
    assert "- `OfficeDocs`" in markdown
    assert "1. `OfficeDocs`" not in markdown
    assert "`OfficeDocs`" in markdown
    assert "`OfficeDocs.Documents`" not in markdown
    assert "pdf-reader" not in markdown
    assert "`SearchResearch`" not in markdown


def test_visible_skill_filter_hides_unavailable_branches() -> None:
    toolkit = _toolkit(visible_skill_names=set())

    peek = toolkit.skill_branch_peek(["ROOT"])
    assert peek["success"] is True
    assert "No child branches." in peek["result"]
    assert "OfficeDocs" not in peek["result"]
    assert "pdf-reader" not in peek["result"]

    explore = toolkit.skill_branch_explore(["OfficeDocs"])
    assert explore["success"] is False
    assert "Unknown branch node id" in explore["result"]


@pytest.mark.asyncio
async def test_runtime_toolkit_methods_return_build_and_missing_index_results(monkeypatch) -> None:
    monkeypatch.setattr(
        _toolkit_mod,
        "build_skill_index",
        lambda manager: {
            "success": True,
            "result": "# Skill Retrieval Index\n\nThe index is ready.",
        },
    )
    monkeypatch.setattr(
        _toolkit_mod,
        "skill_branch_explore",
        lambda node_ids, manager: {
            "success": False,
            "result": "# Skill Tree Retrieval Unavailable\n\nCall `skill_index_build`.",
        },
    )
    toolkit = _toolkit_mod.SkillRetrievalToolkit(manager=SimpleNamespace())

    build_result = await toolkit.skill_index_build()
    explore_result = await toolkit.skill_branch_explore(["OfficeDocs"])

    assert build_result["success"] is True
    assert "index is ready" in build_result["result"]
    assert explore_result["success"] is False
    assert "skill_index_build" in explore_result["result"]
