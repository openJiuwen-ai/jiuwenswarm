# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for protected_paths defaults and merge helper."""

from jiuwenswarm.agents.harness.common.rails.permissions.protected_paths import (
    JIUWENCLAW_PROTECTED_WRITE_PATHS,
        merge_protected_write_paths,
        )


        def test_default_paths_contain_jiuwenbox() -> None:
                assert "jiuwenbox" in JIUWENCLAW_PROTECTED_WRITE_PATHS


                def test_default_paths_contain_permissions_module() -> None:
                    assert (
                            "jiuwenswarm/agents/harness/common/rails/permissions"
                                    in JIUWENCLAW_PROTECTED_WRITE_PATHS
                                        )


                                        def test_default_paths_contain_permission_unit_tests() -> None:
                                            assert "tests/unit_tests/agentserver/permissions" in JIUWENCLAW_PROTECTED_WRITE_PATHS


                                            def test_default_paths_contain_system_matrix_tests() -> None:
                                                assert "tests/system_tests/auto_permission_matrix" in JIUWENCLAW_PROTECTED_WRITE_PATHS


                                                def test_default_paths_have_no_duplicates_or_blanks() -> None:
                                                    assert len(set(JIUWENCLAW_PROTECTED_WRITE_PATHS)) == len(JIUWENCLAW_PROTECTED_WRITE_PATHS)
                                                        assert all(entry.strip() for entry in JIUWENCLAW_PROTECTED_WRITE_PATHS)


                                                        def test_merge_single_group_returns_tuple() -> None:
                                                            assert merge_protected_write_paths(("a", "b")) == ("a", "b")


                                                            def test_merge_deduplicates_across_groups() -> None:
                                                                merged = merge_protected_write_paths(("a", "b"), ("b", "c"), ("a",))
                                                                    assert merged == ("a", "b", "c")


                                                                    def test_merge_preserves_first_occurrence_order() -> None:
                                                                        assert merge_protected_write_paths(("z", "y"), ("y", "x")) == ("z", "y", "x")


                                                                        def test_merge_drops_blank_and_whitespace_only_entries() -> None:
                                                                            assert merge_protected_write_paths(("", "  ", "\t", "keep")) == ("keep",)


                                                                            def test_merge_strips_entries() -> None:
                                                                                assert merge_protected_write_paths(("  spaced  ",)) == ("spaced",)


                                                                                def test_merge_without_input_returns_empty_tuple() -> None:
                                                                                    assert merge_protected_write_paths() == ()


                                                                                    def test_merge_returns_tuple_type() -> None:
                                                                                        assert isinstance(merge_protected_write_paths(("a",)), tuple)
                                                                                        