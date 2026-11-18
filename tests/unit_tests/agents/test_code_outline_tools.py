# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the code_outline AST and regex symbol outline tool."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from openjiuwen.core.sys_operation.cwd import (
    get_cwd,
    get_project_root,
    get_workspace,
    init_cwd,
)

from jiuwenswarm.agents.harness.common.tools import code_outline as exported_code_outline
from jiuwenswarm.agents.harness.common.tools import code_outline_tools
from jiuwenswarm.agents.harness.common.tools.code_outline_tools import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILES,
    _normalize_request,
    code_outline,
)


@pytest.fixture(name="workspace_cwd")
def _workspace_cwd_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bind a clean temporary workspace as the active runtime cwd/project/workspace."""
    prev_cwd = get_cwd()
    prev_project = get_project_root()
    prev_workspace = get_workspace()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_ws = tmp_path / "agent_ws"
    agent_ws.mkdir()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.command_runtime.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    init_cwd(str(workspace), project_root=str(workspace), workspace=str(workspace))
    try:
        yield workspace
    finally:
        init_cwd(prev_cwd, project_root=prev_project, workspace=prev_workspace)


def test_code_outline_card_schema_and_lazy_export() -> None:
    properties = code_outline.card.input_params["properties"]
    assert exported_code_outline is code_outline
    assert code_outline.card.name == "code_outline"
    assert set(properties.keys()) == {
        "path",
        "max_depth",
        "max_files",
        "max_chars",
        "include_private",
    }
    assert "inputs" not in properties
    for key in properties:
        assert properties[key].get("description")


def test_normalize_request_defaults_and_clamping() -> None:
    req = _normalize_request(
        path=None,
        max_depth=None,
        max_files=None,
        max_chars=None,
        include_private=None,
    )
    assert req.path == "."
    assert req.max_depth == DEFAULT_MAX_DEPTH
    assert req.max_files == DEFAULT_MAX_FILES
    assert req.max_chars == DEFAULT_MAX_CHARS
    assert req.include_private is False

    clamped_low = _normalize_request(
        path="  ",
        max_depth=-5,
        max_files=0,
        max_chars=10,
        include_private=True,
    )
    assert clamped_low.path == "."
    assert clamped_low.max_depth == 0
    assert clamped_low.max_files == 1
    assert clamped_low.max_chars == 1_000
    assert clamped_low.include_private is True

    clamped_high = _normalize_request(
        path="src",
        max_depth=999,
        max_files=999,
        max_chars=999_999,
        include_private=False,
    )
    assert clamped_high.max_depth == 10
    assert clamped_high.max_files == 200
    assert clamped_high.max_chars == 100_000


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"path": 123}, "path must be a string"),
        ({"path": "a\x00b"}, "NUL"),
        ({"max_depth": True}, "max_depth must be an integer"),
        ({"max_files": 2.5}, "max_files must be an integer"),
        ({"max_chars": "abc"}, "max_chars must be an integer"),
        ({"include_private": "yes"}, "include_private must be a boolean"),
    ],
)
def test_normalize_request_rejects_invalid_inputs(
    kwargs: dict[str, object],
    match: str,
) -> None:
    defaults: dict[str, object] = {
        "path": ".",
        "max_depth": DEFAULT_MAX_DEPTH,
        "max_files": DEFAULT_MAX_FILES,
        "max_chars": DEFAULT_MAX_CHARS,
        "include_private": False,
    }
    defaults.update(kwargs)
    with pytest.raises(ValueError, match=match):
        _normalize_request(**defaults)


@pytest.mark.asyncio
async def test_python_ast_extracts_classes_functions_signatures_and_docstrings(
    workspace_cwd: Path,
) -> None:
    sample = workspace_cwd / "service.py"
    sample.write_text(
        '"""Service module summary line.\n\nSecond paragraph ignored."""\n\n'
        "class BaseClient:\n"
        "    pass\n\n"
        "@dataclass\n"
        "class UserService(BaseClient):\n"
        '    """Manage user records."""\n\n'
        "    def __init__(self, name: str, /, count: int = 1, *tags: str, "
        "active: bool = True, **extra: object) -> None:\n"
        '        """Initialize service."""\n'
        "        self.name = name\n\n"
        "    def _internal(self) -> None:\n"
        "        pass\n\n"
        "    async def fetch_user(self, user_id: int) -> dict[str, str]:\n"
        '        """Fetch one user by ID."""\n'
        "        return {}\n\n"
        "def _private_helper(x: int) -> int:\n"
        "    return x + 1\n\n"
        "def public_entry(flag: bool = False) -> str:\n"
        '    """Run public entrypoint."""\n'
        "    return 'ok'\n",
        encoding="utf-8",
    )

    public_out = await code_outline.invoke({"path": "service.py"})
    assert "Code Outline: service.py | files: 1 | mode: file" in public_out
    assert "# module: Service module summary line." in public_out
    assert "Second paragraph ignored" not in public_out
    assert "@dataclass" in public_out
    assert "class UserService(BaseClient) [L9-L21]" in public_out
    assert "# Manage user records." in public_out
    assert (
        "def __init__(self, name: str, /, count: int = 1, *tags: str, "
        "active: bool = True, **extra: object) -> None"
    ) in public_out
    assert "async def fetch_user(self, user_id: int) -> dict[str, str]" in public_out
    assert "def public_entry(flag: bool = False) -> str" in public_out
    assert "_internal" not in public_out
    assert "_private_helper" not in public_out

    private_out = await code_outline.invoke(
        {"path": "service.py", "include_private": True},
    )
    assert "def _internal(self) -> None" in private_out
    assert "def _private_helper(x: int) -> int" in private_out


@pytest.mark.asyncio
async def test_python_syntax_error_is_reported_gracefully_in_directory(
    workspace_cwd: Path,
) -> None:
    (workspace_cwd / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (workspace_cwd / "valid.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")

    out = await code_outline.invoke({"path": "."})
    assert "## broken.py" in out
    assert "[syntax error at line 1]:" in out
    assert "## valid.py" in out
    assert "def ok() -> int [L1-L2]" in out


_SAMPLE_CLIENT_TSX = (
    "// function fakeCommented() {}\n"
    "/*\n"
    " * class FakeBlockComment {}\n"
    " * export function fakeInsideBlock() {}\n"
    " */\n"
    'const strA = "function fakeDoubleQuoted() {}";\n'
    "const strB = 'class FakeSingleQuoted {}';\n"
    "const tpl = `export const fakeTemplate = () => {\n"
    "  function nestedFake() {}\n"
    "};`;\n"
    "export interface UserRecord extends BaseRecord {\n"
    "  id: string;\n"
    "}\n"
    "export type UserRole = 'admin' | 'member';\n"
    "export enum StatusCode {\n"
    "  Ok = 200,\n"
    "}\n"
    "export class ApiClient extends BaseClient {\n"
    "  private secretMethod(): void {\n"
    "    return;\n"
    "  }\n"
    "  async loadItem(id: string): Promise<UserRecord> {\n"
    "    if (id) { return {} as UserRecord; }\n"
    "    return {} as UserRecord;\n"
    "  }\n"
    "}\n"
    "export async function runSync(limit: number): Promise<void> {\n"
    "  const inner = () => 1;\n"
    "}\n"
    "export const formatLabel = (name: string): string => {\n"
    "  return name;\n"
    "};\n"
    "function _hiddenHelper() {\n"
    "  return null;\n"
    "}\n"
)


@pytest.mark.asyncio
async def test_jsts_extracts_symbols_and_ignores_comments_and_strings(
    workspace_cwd: Path,
) -> None:
    ts_file = workspace_cwd / "client.tsx"
    ts_file.write_text(_SAMPLE_CLIENT_TSX, encoding="utf-8")

    out = await code_outline.invoke({"path": "client.tsx"})
    for ignored in (
        "fakeCommented",
        "FakeBlockComment",
        "fakeInsideBlock",
        "fakeDoubleQuoted",
        "FakeSingleQuoted",
        "fakeTemplate",
        "nestedFake",
        "inner",
        "secretMethod",
        "_hiddenHelper",
    ):
        assert ignored not in out

    assert "export interface UserRecord extends BaseRecord [L11]" in out
    assert "export type UserRole [L14]" in out
    assert "export enum StatusCode [L15]" in out
    assert "export class ApiClient extends BaseClient [L18]" in out
    assert "  async loadItem(id: string): Promise<UserRecord> [L22]" in out
    assert "export async function runSync(limit: number): Promise<void> [L27]" in out
    assert "export const formatLabel = (name: string): string => [L30]" in out

    private_out = await code_outline.invoke(
        {"path": "client.tsx", "include_private": True},
    )
    assert "  private secretMethod(): void [L19]" in private_out
    assert "function _hiddenHelper() [L33]" in private_out


@pytest.mark.asyncio
async def test_directory_pruning_depth_file_and_char_limits(
    workspace_cwd: Path,
) -> None:
    (workspace_cwd / "node_modules" / "pkg").mkdir(parents=True)
    (workspace_cwd / "node_modules" / "pkg" / "dep.js").write_text(
        "export function shouldBePruned() {}\n",
        encoding="utf-8",
    )
    (workspace_cwd / ".git").mkdir()
    (workspace_cwd / ".git" / "hook.py").write_text("def git_hook(): pass\n", encoding="utf-8")
    (workspace_cwd / "notes.md").write_text("# ignored\n", encoding="utf-8")
    (workspace_cwd / "root.py").write_text("def root_fn(): pass\n", encoding="utf-8")
    (workspace_cwd / "sub" / "deep").mkdir(parents=True)
    (workspace_cwd / "sub" / "mid.py").write_text("def mid_fn(): pass\n", encoding="utf-8")
    (workspace_cwd / "sub" / "deep" / "leaf.mjs").write_text(
        "export function leafFn() {}\n",
        encoding="utf-8",
    )

    depth0 = await code_outline.invoke({"path": ".", "max_depth": 0})
    assert "def root_fn()" in depth0
    assert "mid_fn" not in depth0
    assert "shouldBePruned" not in depth0
    assert "git_hook" not in depth0
    assert "notes.md" not in depth0
    assert "Depth limit reached (max_depth=0)" in depth0

    max1 = await code_outline.invoke({"path": ".", "max_files": 1})
    assert "File limit reached (max_files=1)" in max1

    large_py = workspace_cwd / "many.py"
    gen_blocks: list[str] = []
    for idx in range(40):
        gen_blocks.append(
            f"def generated_function_{idx}(arg_one: int, arg_two: str) -> bool:\n"
            f'    """Documentation summary for generated function {idx}."""\n'
            "    return True\n"
        )
    large_py.write_text("\n".join(gen_blocks), encoding="utf-8")
    capped = await code_outline.invoke({"path": ".", "max_chars": 1_000})
    assert len(capped) <= 1_000
    assert "Truncated at max_chars=1000" in capped


@pytest.mark.asyncio
async def test_binary_non_utf8_oversized_and_unsupported_files(
    workspace_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace_cwd / "bin_mod.py").write_bytes(b"def ok():\n    pass\n\x00\x01\x02")
    (workspace_cwd / "bad_enc.py").write_bytes(b"\xff\xfe\xfa\xfb")
    (workspace_cwd / "plain.txt").write_text("hello", encoding="utf-8")

    dir_out = await code_outline.invoke({"path": "."})
    assert "[skipped: binary file (NUL byte detected)]" in dir_out
    assert "[skipped: non-UTF-8 file]" in dir_out

    err_txt = await code_outline.invoke({"path": "plain.txt"})
    assert err_txt.startswith("[ERROR]: code_outline failed:")
    assert "unsupported file type" in err_txt

    big_py = workspace_cwd / "big.py"
    big_py.write_text("def big_fn(): pass\n" * 10, encoding="utf-8")
    monkeypatch.setattr(code_outline_tools, "_MAX_FILE_BYTES", 32)
    big_out = await code_outline.invoke({"path": "big.py"})
    assert "[skipped: file exceeds 32 bytes]" in big_out


@pytest.mark.asyncio
async def test_workspace_boundary_rejects_escape_and_external_symlinks(
    workspace_cwd: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def secret(): pass\n", encoding="utf-8")

    rel_escape = await code_outline.invoke({"path": "../outside.py"})
    assert rel_escape.startswith("[ERROR]: code_outline failed:")
    assert "outside workspace" in rel_escape

    abs_escape = await code_outline.invoke({"path": str(outside)})
    assert abs_escape.startswith("[ERROR]: code_outline failed:")
    assert "outside workspace" in abs_escape

    missing = await code_outline.invoke({"path": "missing.py"})
    assert missing.startswith("[ERROR]: code_outline failed:")
    assert "does not exist" in missing

    link_path = workspace_cwd / "escape_link.py"
    try:
        os.symlink(outside, link_path)
    except OSError:
        pytest.skip("Symlink creation requires elevated privileges on this host")

    single_link = await code_outline.invoke({"path": "escape_link.py"})
    assert single_link.startswith("[ERROR]: code_outline failed:")
    assert "outside workspace" in single_link

    (workspace_cwd / "safe.py").write_text("def safe(): pass\n", encoding="utf-8")
    dir_link_out = await code_outline.invoke({"path": "."})
    assert "def safe()" in dir_link_out
    assert "secret" not in dir_link_out
    assert "Skipped 1 symlink(s) pointing outside workspace" in dir_link_out


@pytest.mark.asyncio
async def test_empty_call_defaults_to_current_workspace(workspace_cwd: Path) -> None:
    (workspace_cwd / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    out = await code_outline.invoke({})
    assert "Code Outline: . | files: 1 | mode: directory" in out
    assert "def main() -> None [L1-L2]" in out
