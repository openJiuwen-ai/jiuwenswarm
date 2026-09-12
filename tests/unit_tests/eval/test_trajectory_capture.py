# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for eval trajectory capture (no LLM)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT))

from trajectory import (  # noqa: E402
    TrajectoryRecorder,
    contextbench_record,
    extract_step,
    last_patch_context_from_texts,
    normalize_traj_data,
    parse_patch_context,
    resolve_repo_path,
    to_contextbench_traj_data,
    to_traj_data,
)


def test_read_file_uses_offset_and_limit() -> None:
    step = extract_step(
        "read_file",
        {"file_path": "src/auth.py", "offset": 41, "limit": 20},
        {"content": "..."},
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/auth.py"]
    assert step["spans"]["src/auth.py"] == [{"start": 42, "end": 61}]


def test_find_code_symbols_collects_symbol_spans() -> None:
    step = extract_step(
        "find_code_symbols",
        {"query": "create_user"},
        {
            "status": "COMPLETE",
            "matches": [
                {
                    "symbol_id": "src/service/user.py::UserService.create_user",
                    "name": "create_user",
                    "file": "src/service/user.py",
                    "start_line": 42,
                    "end_line": 71,
                }
            ],
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/service/user.py"]
    assert step["spans"]["src/service/user.py"] == [{"start": 42, "end": 71}]
    assert "create_user" in step["symbols"]["src/service/user.py"]


def test_grep_parses_file_line_output() -> None:
    step = extract_step(
        "grep",
        {"pattern": "PermissionDenied"},
        {
            "content": "src/auth.py:12: raise PermissionDenied\nsrc/views.py:88: except PermissionDenied"
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/auth.py", "src/views.py"]
    assert step["spans"]["src/auth.py"] == [{"start": 12, "end": 12}]


def test_submit_code_context_is_utilized() -> None:
    step = extract_step(
        "submit_code_context",
        {"summary": "primary class"},
        {
            "locations": [
                {
                    "file": "astropy/wcs/wcsapi/wrappers/sliced_wcs.py",
                    "start_line": 80,
                    "end_line": 120,
                    "name": "SlicedLowLevelWCS",
                }
            ],
            "patch_context": (
                "<PATCH_CONTEXT>\n"
                "File: astropy/wcs/wcsapi/wrappers/sliced_wcs.py\n"
                "Lines: 80-120\n"
                "</PATCH_CONTEXT>"
            ),
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["astropy/wcs/wcsapi/wrappers/sliced_wcs.py"]
    assert step["spans"]["astropy/wcs/wcsapi/wrappers/sliced_wcs.py"] == [
        {"start": 80, "end": 120}
    ]


def test_read_symbol_is_an_explored_view() -> None:
    step = extract_step(
        "read_symbol",
        {"symbol_id": "src/user.py::UserService"},
        {
            "file": "src/user.py",
            "start_line": 1,
            "end_line": 20,
            "symbol_start_line": 1,
            "symbol_end_line": 16,
            "name": "UserService",
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/user.py"]
    assert step["spans"]["src/user.py"] == [{"start": 1, "end": 20}]


def test_trace_call_paths_flattens_path_nodes() -> None:
    step = extract_step(
        "trace_call_paths",
        {"symbol_id": "src/api.py::handle"},
        {
            "status": "COMPLETE",
            "paths": [
                {
                    "nodes": [
                        {
                            "symbol_id": "src/api.py::handle",
                            "name": "handle",
                            "file": "src/api.py",
                            "start_line": 10,
                            "end_line": 20,
                        },
                        {
                            "symbol_id": "src/service.py::run",
                            "name": "run",
                            "file": "src/service.py",
                            "start_line": 30,
                            "end_line": 55,
                        },
                    ],
                    "edges": [{"line": 15}],
                }
            ],
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/api.py", "src/service.py"]
    assert step["spans"]["src/service.py"] == [{"start": 30, "end": 55}]


def test_find_callers_records_every_affected_group() -> None:
    step = extract_step(
        "find_callers",
        {"symbol_id": "src/base.py::Base.run"},
        {
            "status": "PARTIAL",
            "direct_callers": [
                {"file": "src/api.py", "start_line": 10, "end_line": 20, "name": "handle"}
            ],
            "subclasses": [
                {"file": "src/impl.py", "start_line": 5, "end_line": 40, "name": "Impl"}
            ],
            "tests": [
                {"file": "tests/test_base.py", "start_line": 1, "end_line": 9, "name": "test_run"}
            ],
            "risk": {"level": "medium"},
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/api.py", "src/impl.py", "tests/test_base.py"]


def test_select_code_context_complete_records_span() -> None:
    step = extract_step(
        "select_code_context",
        {"symbol_id": "src/auth.py::check", "reason": "gate"},
        {
            "status": "COMPLETE",
            "file": "src/auth.py",
            "start_line": 10,
            "end_line": 40,
            "name": "check",
        },
        repo_root="/repo",
    )
    assert step is not None
    assert step["spans"]["src/auth.py"] == [{"start": 10, "end": 40}]


def test_select_code_context_error_is_ignored() -> None:
    assert (
        extract_step(
            "select_code_context",
            {"symbol_id": "tests/test_auth.py::test_x", "reason": "no"},
            {"status": "ERROR", "file": "tests/test_auth.py", "message": "test file"},
            repo_root="/repo",
        )
        is None
    )


def test_utilized_select_is_the_declared_context() -> None:
    recorder = TrajectoryRecorder()
    recorder.record(
        "find_code_symbols",
        {"query": "a"},
        {"matches": [{"file": "noise.py", "name": "foo", "start_line": 1, "end_line": 99}]},
    )
    recorder.record(
        "read_file",
        {"file_path": "noise.py", "offset": 0, "limit": 10},
        {"content": "x"},
    )
    recorder.record(
        "select_code_context",
        {"symbol_id": "src/auth.py::check", "reason": "gate"},
        {"status": "COMPLETE", "file": "src/auth.py", "start_line": 10, "end_line": 20, "name": "check"},
    )
    traj = recorder.traj_data()
    assert traj["pred_files"] == ["src/auth.py"]
    assert traj["pred_spans"]["src/auth.py"] == [{"start": 10, "end": 20}]
    assert len(traj["pred_steps"]) == 1


def test_unknown_tool_is_ignored() -> None:
    assert extract_step("bash", {"command": "ls"}, {"stdout": "a"}, repo_root="/repo") is None


def test_recorder_and_rail_share_steps(tmp_path: Path) -> None:
    recorder = TrajectoryRecorder(repo_root=str(tmp_path))
    recorder.record(
        "read_file",
        {"file_path": str(tmp_path / "a.py"), "offset": 0, "limit": 10},
        {"content": "x"},
    )
    recorder.record(
        "find_code_symbols",
        {"query": "a"},
        {"matches": [{"file": "a.py", "name": "foo", "start_line": 1, "end_line": 3}]},
    )
    traj = to_traj_data(recorder.steps)
    assert "a.py" in traj["pred_files"]
    assert traj["pred_spans"]["a.py"]
    rail = recorder.make_rail()
    assert rail.recorder is recorder


def test_tool_output_object_is_unwrapped() -> None:
    class _Out:
        def __init__(self) -> None:
            self.data = {
                "matches": [{"file": "x.py", "start_line": 2, "end_line": 4, "name": "X"}]
            }

        def model_dump(self) -> dict:
            return {"success": True, "data": self.data}

    step = extract_step("find_code_symbols", {"query": "X"}, _Out(), repo_root="/repo")
    assert step is not None
    assert step["spans"]["x.py"] == [{"start": 2, "end": 4}]


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# x\n", encoding="utf-8")


def test_resolve_unique_basename_and_suffix(tmp_path: Path) -> None:
    _touch(tmp_path, "pkg/frames/altaz.py")
    _touch(tmp_path, "pkg/frames/__init__.py")
    _touch(tmp_path, "pkg/frames/utils.py")
    _touch(tmp_path, "pkg/other/utils.py")
    _touch(tmp_path, "pkg/other/__init__.py")
    root = str(tmp_path)
    assert resolve_repo_path("altaz.py", root) == "pkg/frames/altaz.py"
    assert resolve_repo_path("frames/altaz.py", root) == "pkg/frames/altaz.py"
    assert resolve_repo_path("utils.py", root) == ""
    assert resolve_repo_path("altaz.py", root, search_root="pkg/frames") == "pkg/frames/altaz.py"


def test_grep_joins_search_root(tmp_path: Path) -> None:
    _touch(tmp_path, "pkg/frames/altaz.py")
    step = extract_step(
        "grep",
        {"pattern": "AltAz", "path": "pkg/frames"},
        {"content": "altaz.py:46: class AltAz"},
        repo_root=str(tmp_path),
    )
    assert step is not None
    assert step["files"] == ["pkg/frames/altaz.py"]
    assert step["spans"]["pkg/frames/altaz.py"] == [{"start": 46, "end": 46}]


def test_normalize_uses_sibling_dir_hint(tmp_path: Path) -> None:
    _touch(tmp_path, "pkg/frames/altaz.py")
    _touch(tmp_path, "pkg/frames/__init__.py")
    _touch(tmp_path, "pkg/other/__init__.py")
    traj = normalize_traj_data(
        {
            "pred_steps": [
                {
                    "files": ["altaz.py", "__init__.py"],
                    "spans": {
                        "altaz.py": [{"start": 46, "end": 46}],
                        "__init__.py": [{"start": 17, "end": 17}],
                    },
                    "symbols": {},
                }
            ]
        },
        str(tmp_path),
    )
    assert traj["pred_files"] == ["pkg/frames/__init__.py", "pkg/frames/altaz.py"]
    assert traj["pred_spans"]["pkg/frames/__init__.py"] == [{"start": 17, "end": 17}]


def test_normalize_moves_pred_symbols_off_official_field(tmp_path: Path) -> None:
    _touch(tmp_path, "pkg/frames/altaz.py")
    traj = normalize_traj_data(
        {
            "pred_steps": [
                {
                    "files": ["pkg/frames/altaz.py"],
                    "spans": {"pkg/frames/altaz.py": [{"start": 46, "end": 80}]},
                    "symbols": {"pkg/frames/altaz.py": ["AltAz"]},
                }
            ],
            "pred_files": ["pkg/frames/altaz.py"],
            "pred_spans": {"pkg/frames/altaz.py": [{"start": 46, "end": 80}]},
            "pred_symbols": {"pkg/frames/altaz.py": ["AltAz"]},
        },
        str(tmp_path),
    )
    assert traj["pred_symbols"] == {}
    assert traj["tool_symbols"] == {"pkg/frames/altaz.py": ["AltAz"]}
    assert traj["pred_spans"]["pkg/frames/altaz.py"] == [{"start": 46, "end": 80}]


def test_read_code_uses_start_and_end() -> None:
    step = extract_step(
        "read_code",
        {"path": "src/auth.py", "start_line": 10, "end_line": 40},
        {"content": "..."},
        repo_root="/repo",
    )
    assert step is not None
    assert step["spans"]["src/auth.py"] == [{"start": 10, "end": 40}]


def test_parse_patch_context_file_lines_pairs() -> None:
    spans = parse_patch_context(
        "summary\n<PATCH_CONTEXT>\nFile: src/auth.py\nLines: 10-40\nFile: src/b.py\nLines: 2-2\n</PATCH_CONTEXT>"
    )
    assert spans["src/auth.py"] == [{"start": 10, "end": 40}]
    assert spans["src/b.py"] == [{"start": 2, "end": 2}]


def test_parse_patch_context_keeps_last_block_only() -> None:
    spans = parse_patch_context(
        "<PATCH_CONTEXT>\nFile: old.py\nLines: 1-9\n</PATCH_CONTEXT>\n"
        "<PATCH_CONTEXT>\nFile: src/auth.py\nLines: 10-20\n</PATCH_CONTEXT>"
    )
    assert spans == {"src/auth.py": [{"start": 10, "end": 20}]}


def test_last_patch_context_scans_every_message() -> None:
    spans = last_patch_context_from_texts(
        [
            "<PATCH_CONTEXT>\nFile: mid.py\nLines: 1-2\n</PATCH_CONTEXT>",
            "no block here",
            "<PATCH_CONTEXT>\nFile: final.py\nLines: 8-9\n</PATCH_CONTEXT>",
        ]
    )
    assert spans == {"final.py": [{"start": 8, "end": 9}]}


def test_read_file_without_limit_is_file_only() -> None:
    step = extract_step(
        "read_file",
        {"file_path": "src/auth.py"},
        {"content": "..."},
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/auth.py"]
    assert step["spans"] == {}


def test_bash_cat_is_file_only() -> None:
    step = extract_step(
        "bash",
        {"command": "cat src/auth.py"},
        {"stdout": "..."},
        repo_root="/repo",
    )
    assert step is not None
    assert step["files"] == ["src/auth.py"]
    assert step["spans"] == {}


def test_normalize_does_not_refill_empty_final(tmp_path: Path) -> None:
    _touch(tmp_path, "src/auth.py")
    traj = normalize_traj_data(
        {
            "pred_steps": [
                {
                    "files": ["src/auth.py"],
                    "spans": {"src/auth.py": [{"start": 1, "end": 20}]},
                    "symbols": {},
                }
            ],
            "pred_files": [],
            "pred_spans": {},
        },
        str(tmp_path),
    )
    assert traj["pred_files"] == []
    assert traj["pred_spans"] == {}
    assert traj["pred_steps"][0]["files"] == ["src/auth.py"]


def test_contextbench_mode_drops_search_hits_from_pred_files() -> None:
    recorder = TrajectoryRecorder()
    recorder.record(
        "find_code_symbols",
        {"query": "create_user"},
        {
            "matches": [
                {
                    "file": "src/service/user.py",
                    "name": "create_user",
                    "start_line": 42,
                    "end_line": 71,
                }
            ]
        },
    )
    recorder.record(
        "read_file",
        {"file_path": "src/service/user.py", "offset": 41, "limit": 30},
        {"content": "..."},
    )
    traj = recorder.traj_data()
    assert [step["files"] for step in traj["pred_steps"]] == [["src/service/user.py"]]
    assert traj["pred_files"] == []
    assert traj["pred_spans"] == {}
    assert traj["utilized_source"] == "empty"
    assert traj["retrieved_hits"]


def test_contextbench_mode_uses_patch_context_as_utilized() -> None:
    recorder = TrajectoryRecorder()
    recorder.record(
        "read_file",
        {"file_path": "src/noise.py", "offset": 0, "limit": 10},
        {"content": "x"},
    )
    recorder.apply_output_text(
        "<PATCH_CONTEXT>\nFile: src/auth.py\nLines: 10-20\n</PATCH_CONTEXT>"
    )
    traj = recorder.traj_data()
    assert traj["pred_files"] == ["src/auth.py"]
    assert traj["pred_spans"]["src/auth.py"] == [{"start": 10, "end": 20}]
    assert traj["utilized_source"] == "declared"
    assert traj["pred_steps"][0]["files"] == ["src/noise.py"]


def test_to_contextbench_traj_data_declared_commit() -> None:
    traj = to_contextbench_traj_data(
        [
            {
                "tool": "find_code_symbols",
                "files": ["noise.py"],
                "spans": {"noise.py": [{"start": 1, "end": 99}]},
                "symbols": {},
            },
            {
                "tool": "read_file",
                "files": ["src/auth.py"],
                "spans": {"src/auth.py": [{"start": 1, "end": 20}]},
                "symbols": {},
            },
            {
                "tool": "submit_code_context",
                "files": ["src/auth.py"],
                "spans": {"src/auth.py": [{"start": 10, "end": 18}]},
                "symbols": {"src/auth.py": ["AuthBackend"]},
            },
        ]
    )
    assert traj["pred_files"] == ["src/auth.py"]
    assert traj["pred_spans"]["src/auth.py"] == [{"start": 10, "end": 18}]
    assert traj["utilized_source"] == "declared"
    assert traj["pred_symbols"] == {}
    assert traj["tool_symbols"] == {"src/auth.py": ["AuthBackend"]}


def test_submit_names_do_not_enter_official_pred_symbols() -> None:
    recorder = TrajectoryRecorder(repo_root="/repo")
    recorder.record(
        "submit_code_context",
        {"summary": "primary class"},
        {
            "locations": [
                {
                    "file": "src/auth.py",
                    "start_line": 10,
                    "end_line": 18,
                    "name": "AuthBackend",
                    "symbol_id": "src/auth.py::AuthBackend",
                }
            ]
        },
    )
    traj = recorder.traj_data()
    assert traj["pred_files"] == ["src/auth.py"]
    assert traj["pred_spans"]["src/auth.py"] == [{"start": 10, "end": 18}]
    assert traj["pred_symbols"] == {}
    assert "AuthBackend" in (traj.get("tool_symbols") or {}).get("src/auth.py", [])


def test_as_payload_map_accepts_list_evidence() -> None:
    from coding_agent import _as_payload_map

    mapped = _as_payload_map([{"symbol_id": "a.py::f", "file": "a.py"}])
    assert mapped["a.py::f"]["file"] == "a.py"
    assert _as_payload_map({"k": {"symbol_id": "x"}})["k"]["symbol_id"] == "x"
    assert _as_payload_map(None) == {}


def test_system_patch_context_ignores_last_read_without_submit() -> None:
    from coding_agent import system_patch_context

    class State:
        selected = []
        read_evidence = {
            "x": {
                "symbol_id": "schema.py::f",
                "file": "schema.py",
                "start_line": 1122,
                "end_line": 1149,
            }
        }
        candidates = {}

    class Agent:
        _code_graph_run_state = State()

    assert system_patch_context(Agent()) == ""


def test_contextbench_record_strips_output_and_keeps_schema() -> None:
    record = contextbench_record(
        {
            "instance_id": "org__repo-1",
            "traj_data": {
                "pred_steps": [{"files": ["a.py"], "spans": {"a.py": [{"start": 1, "end": 2}]}, "symbols": {}}],
                "pred_files": ["a.py"],
                "pred_spans": {"a.py": [{"start": 1, "end": 2}]},
                "pred_symbols": {},
            },
            "model_patch": "",
            "output": "do not send this to evaluate.py",
            "extra": 1,
        }
    )
    assert set(record) == {"instance_id", "traj_data", "model_patch"}
    assert record["instance_id"] == "org__repo-1"
    assert record["traj_data"]["pred_files"] == ["a.py"]
    assert "output" not in record


def test_contextbench_record_drops_tool_names_from_official_symbols() -> None:
    record = contextbench_record(
        {
            "instance_id": "org__repo-1",
            "traj_data": {
                "pred_steps": [
                    {
                        "files": ["a.py"],
                        "spans": {"a.py": [{"start": 1, "end": 2}]},
                        "symbols": {"a.py": ["Foo"]},
                    }
                ],
                "pred_files": ["a.py"],
                "pred_spans": {"a.py": [{"start": 1, "end": 2}]},
                "pred_symbols": {"a.py": ["Foo"]},
            },
            "model_patch": "",
        }
    )
    assert record["traj_data"]["pred_symbols"] == {}
    assert record["traj_data"]["pred_spans"]["a.py"] == [{"start": 1, "end": 2}]
    assert record["traj_data"]["pred_steps"][0]["symbols"] == {}

