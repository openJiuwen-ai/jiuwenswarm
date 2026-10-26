from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.symphony.skill_retrieval.dispatch_imports import dispatch_import_path


def test_dynamic_grouping_prompts_do_not_use_fixed_group_count() -> None:
    with dispatch_import_path():
        from indexing.tree.grouping import TreeGroupingEngine

    class FakeBuilder:
        def __init__(self) -> None:
            self.config = SimpleNamespace(branching_factor=999)
            self.prompts: list[str] = []

        def _call_llm_json(self, prompt: str) -> dict:
            self.prompts.append(prompt)
            if "Canonicalization pass" in prompt:
                return {"canonical_groups": {"alpha": {"name": "Alpha", "description": "Alpha group"}}}
            return {"groups": {"alpha": {"name": "Alpha", "description": "Alpha group"}}}

    builder = FakeBuilder()
    engine = TreeGroupingEngine(builder)

    groups = engine.discover_groups(
        [{"id": "skill-a", "name": "Skill A", "description": "A skill."}],
        parent_context={"name": "Parent", "description": "Parent scope."},
    )
    merged = engine.merge_group_definitions([groups, groups])

    expected_group = {
        "alpha": {
            "name": "Alpha",
            "description": "Alpha group",
            "select_when": "",
            "dont_select_when": "",
        }
    }
    assert groups == expected_group
    assert merged == expected_group
    assert len(builder.prompts) == 2
    assert "996" not in builder.prompts[0]
    assert "1001" not in builder.prompts[0]
    assert "996" not in builder.prompts[1]
    assert "1001" not in builder.prompts[1]
    assert "fixed configured count" in builder.prompts[0]
    assert "fixed configured count" in builder.prompts[1]
