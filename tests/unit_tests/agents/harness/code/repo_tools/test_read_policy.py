# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read policy for the repo tools and the repo map.

Regression for the integration audit: with a read deny on ``secret/**``,
``read_file`` and ``grep`` were refused but ``code_search`` returned the
contents of ``secret/keys.py``. Tool calls here go through agent-core's real
ability manager with a JSON-string ``ToolCall.arguments``; permissions are the
shipped template section plus one deny rule, evaluated by the real engine.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.factory import create_deep_agent

from jiuwenswarm.agents.harness.code.repo_tools import (
    CodeSearchEngine,
    CodeSearchTool,
    FindSymbolTool,
    LspBridge,
    SymbolOutlineTool,
    build_read_policy,
    build_repo_map,
    cached_repo_map,
    clear_repo_map_cache,
    permission_read_policy_factory,
)
from jiuwenswarm.agents.harness.code.repo_tools.read_policy import (
    READ_POLICY_NOTICE,
    ReadPolicy,
    filter_lsp_text,
    resolve_read_policy,
)

_TEMPLATE = (
    Path(__file__).resolve().parents[6] / "jiuwenswarm" / "resources" / "config.yaml"
)
_PROJECT_POLICY_MODULE = (
    "jiuwenswarm.agents.harness.common.rails.permissions.project_policy"
)
SECRET = "TOPSECRET-123"


def _template_permissions(
    *, enabled: bool = True, deny: str | None = "**/secret/**"
) -> dict[str, Any]:
    perms = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))["permissions"]
    perms = dict(perms)
    perms["enabled"] = enabled
    if deny:
        fg = dict(perms["file_guard"])
        fg["paths"] = list(fg.get("paths") or []) + [
            {
                "path": deny,
                "match": "glob",
                "read": "deny",
                "write": "deny",
                "exec": "deny",
            }
        ]
        perms["file_guard"] = fg
    return perms


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / "secret").mkdir(parents=True)
    (root / "secret" / "keys.py").write_text(
        f"API_KEY = '{SECRET}'\ndef load_keys():\n    pass\n", encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        f"# uses {SECRET} marker\ndef load_keys():\n    return 1\n", encoding="utf-8"
    )
    return root


def _engine_factory(perms: dict[str, Any]):
    return lambda root: build_read_policy(
        root, engine_permissions=perms, project_permissions=perms
    )


async def _run_tool_call(tool: Any, name: str, arguments: str, workspace: Path) -> str:
    """Execute one tool call exactly as agent-core does (JSON-string arguments)."""
    model = Model(
        ModelClientConfig(
            client_provider="OpenAI", api_key="x", api_base="http://127.0.0.1:9"
        ),
        ModelRequestConfig(model="x"),
    )
    deep = create_deep_agent(
        model, system_prompt="base", workspace=str(workspace), tools=[tool]
    )
    await deep.ensure_initialized()
    inner = deep._react_agent
    session = create_agent_session(session_id="read-policy", card=inner.card)
    ctx = AgentCallbackContext(agent=inner, inputs=None)
    call = ToolCall(id="c1", type="function", name=name, arguments=arguments)
    [(_result, message)] = await inner.ability_manager.execute(ctx, call, session)
    return str(message.content)


# ─── the audit repro, through the real ability manager ──────────


def test_code_search_hides_denied_file_contents(repo: Path, tmp_path: Path):
    tool = CodeSearchTool(
        lambda: str(repo), read_policy_factory=_engine_factory(_template_permissions())
    )
    text = asyncio.run(
        _run_tool_call(
            tool, "code_search", json.dumps({"pattern": SECRET}), tmp_path / "ws"
        )
    )
    assert "API_KEY" not in text
    assert "secret/keys.py" not in text
    assert "src/app.py" in text
    assert READ_POLICY_NOTICE in text


def test_code_search_without_policy_still_finds_everything(repo: Path, tmp_path: Path):
    tool = CodeSearchTool(lambda: str(repo))
    text = asyncio.run(
        _run_tool_call(
            tool, "code_search", json.dumps({"pattern": SECRET}), tmp_path / "ws"
        )
    )
    assert "secret/keys.py" in text


def test_code_search_scoped_to_denied_paths(repo: Path, tmp_path: Path):
    tool = CodeSearchTool(
        lambda: str(repo), read_policy_factory=_engine_factory(_template_permissions())
    )
    # A denied file as the search path is refused outright.
    text = asyncio.run(
        _run_tool_call(
            tool,
            "code_search",
            json.dumps({"pattern": "API", "path": "secret/keys.py"}),
            tmp_path / "ws",
        )
    )
    assert SECRET not in text
    assert "not readable under the read policy" in text
    # ``**/secret/**`` matches only what is below ``secret``; a directory whose
    # whole content is denied is refused too.
    text = asyncio.run(
        _run_tool_call(
            tool,
            "code_search",
            json.dumps({"pattern": "API", "path": "secret"}),
            tmp_path / "ws",
        )
    )
    assert SECRET not in text
    assert "not readable under the read policy" in text


def test_permissions_disabled_means_no_engine_filtering(repo: Path):
    # read_file is not checked at all when the engine is off; neither is search.
    factory = permission_read_policy_factory(
        lambda: _template_permissions(enabled=False)
    )
    policy = factory(repo)
    assert not policy.active
    assert policy.allows(repo / "secret" / "keys.py")


def test_factory_composes_user_layer(monkeypatch, repo: Path):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    monkeypatch.setattr(permissions_layers, "load_user_permissions", dict)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: {})
    factory = permission_read_policy_factory(
        lambda: _template_permissions(), lambda: "sid-1"
    )
    policy = factory(repo)
    assert policy.hidden_reason(repo / "secret" / "keys.py")
    assert policy.allows(repo / "src" / "app.py")


def test_symlink_to_denied_dir_is_hidden(repo: Path):
    (repo / "src" / "alias").symlink_to(repo / "secret", target_is_directory=True)
    policy = build_read_policy(repo, engine_permissions=_template_permissions())
    assert policy.hidden_reason(repo / "src" / "alias" / "keys.py")


def test_policy_build_failure_refuses_search(repo: Path):
    def boom(_root: Path) -> ReadPolicy:
        raise RuntimeError("config unreadable")

    policy = resolve_read_policy(boom, repo)
    assert policy.refusal
    tool = CodeSearchTool(lambda: str(repo), read_policy_factory=boom)
    out = asyncio.run(tool.invoke({"pattern": SECRET}))
    assert not out.success
    assert "read policy could not be evaluated" in out.error


def test_check_error_hides_that_file_only(repo: Path):
    def flaky(path: Path) -> str | None:
        if path.name == "keys.py":
            raise OSError("boom")
        return None

    policy = ReadPolicy(checks=(("flaky", flaky),))
    assert policy.hidden_reason(repo / "secret" / "keys.py") == "flaky check failed"
    assert policy.allows(repo / "src" / "app.py")


# ─── symbol tools ──────────────────────────────────────────────


def test_find_symbol_regex_results_filtered(repo: Path, tmp_path: Path):
    factory = _engine_factory(_template_permissions())
    engine = CodeSearchEngine(lambda: str(repo), read_policy_factory=factory)
    tool = FindSymbolTool(lambda: str(repo), engine=engine)
    text = asyncio.run(
        _run_tool_call(
            tool,
            "find_symbol",
            json.dumps({"name": "load_keys", "kind": "all"}),
            tmp_path / "ws",
        )
    )
    assert "secret/keys.py" not in text
    assert "src/app.py" in text
    assert READ_POLICY_NOTICE in text


def test_find_symbol_anchor_in_denied_dir_refused(repo: Path):
    factory = _engine_factory(_template_permissions())
    tool = FindSymbolTool(lambda: str(repo), read_policy_factory=factory)
    out = asyncio.run(tool.invoke({"name": "load_keys", "file_path": "secret/keys.py"}))
    assert not out.success
    assert "not readable under the read policy" in out.error


def test_symbol_outline_of_denied_file_refused(repo: Path, tmp_path: Path):
    tool = SymbolOutlineTool(
        lambda: str(repo), read_policy_factory=_engine_factory(_template_permissions())
    )
    text = asyncio.run(
        _run_tool_call(
            tool,
            "symbol_outline",
            json.dumps({"file_path": "secret/keys.py"}),
            tmp_path / "ws",
        )
    )
    assert "load_keys" not in text
    assert "not readable under the read policy" in text


def test_lsp_text_filtering_grouped_and_flat(repo: Path):
    policy = build_read_policy(repo, engine_permissions=_template_permissions())
    grouped = (
        f"{repo}/secret/keys.py:\n  2: Function load_keys\n"
        f"{repo}/src/app.py:\n  2: Function load_keys"
    )
    text = filter_lsp_text(grouped, repo, policy)
    assert "secret" not in text and "src/app.py" in text
    flat = f"{repo}/secret/keys.py:2:1\nDefined in {repo}/secret/keys.py:2:5\nsrc/app.py:2:1"
    text = filter_lsp_text(flat, repo, policy)
    assert text.strip() == "src/app.py:2:1"


# ─── repo map ──────────────────────────────────────────────────


def test_repo_map_omits_denied_paths_and_does_not_read_them(tmp_path: Path):
    root = (tmp_path / "proj").resolve()
    (root / "secret").mkdir(parents=True)
    (root / "secret" / "keys.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hidden-name"\n', encoding="utf-8"
    )
    perms = _template_permissions(deny="**/pyproject.toml")
    fg = perms["file_guard"]
    fg["paths"].append(
        {
            "path": "**/secret/**",
            "match": "glob",
            "read": "deny",
            "write": "deny",
            "exec": "deny",
        }
    )
    policy = build_read_policy(root, engine_permissions=perms)
    text = build_repo_map(root, read_policy=policy).text
    assert "secret" not in text
    assert "hidden-name" not in text
    assert "pyproject.toml" not in text
    assert "src/" in text
    assert "hidden-name" in build_repo_map(root).text


def test_repo_map_cache_keyed_by_policy(tmp_path: Path):
    clear_repo_map_cache()
    root = (tmp_path / "proj").resolve()
    (root / "secret").mkdir(parents=True)
    (root / "secret" / "keys.py").write_text("x = 1\n", encoding="utf-8")
    open_map = cached_repo_map(root).text
    perms = _template_permissions()
    closed_map = cached_repo_map(
        root, read_policy=build_read_policy(root, engine_permissions=perms)
    ).text
    assert "secret" in open_map
    assert "secret" not in closed_map
    clear_repo_map_cache()


# ─── deny-only project policy (optional package) ─────────────────


@dataclass
class _Deny:
    raw: str
    axes: frozenset


class _StubProjectPolicy:
    def __init__(self, root: str, denies: list[str], error: str | None = None) -> None:
        self.root = root
        self.error = error
        self.deny_paths = [_Deny(d, frozenset({"read"})) for d in denies]

    def path_denied(self, path: Any, axis: str) -> _Deny | None:
        text = Path(path).as_posix()
        for d in self.deny_paths:
            prefix = f"{self.root}/{d.raw.rstrip('/')}"
            if axis in d.axes and (text == prefix or text.startswith(prefix + "/")):
                return d
        return None


@pytest.fixture
def stub_project_policy(monkeypatch):
    """Stand in for the shell-policy package's loader when it is not merged.

    The real module is exercised by ``test_real_project_policy_module`` when
    both packages are present.
    """
    state: dict[str, Any] = {"denies": [], "error": None}
    module = types.ModuleType(_PROJECT_POLICY_MODULE)
    module.load_project_policy = lambda root, config=None: _StubProjectPolicy(
        str(Path(root).resolve()), state["denies"], state["error"]
    )
    module.invalid_policy_message = lambda pol: f"project policy invalid: {pol.error}"
    monkeypatch.setitem(sys.modules, _PROJECT_POLICY_MODULE, module)
    return state


def test_project_policy_deny_read_hides_results(repo: Path, stub_project_policy):
    stub_project_policy["denies"] = ["secret"]
    tool = CodeSearchTool(
        lambda: str(repo),
        read_policy_factory=permission_read_policy_factory(lambda: {"enabled": False}),
    )
    out = asyncio.run(tool.invoke({"pattern": SECRET}))
    assert out.success
    assert "secret/keys.py" not in out.data["content"]
    assert READ_POLICY_NOTICE in out.data["content"]


def test_invalid_project_policy_refuses_search(repo: Path, stub_project_policy):
    stub_project_policy["error"] = "not YAML"
    tool = CodeSearchTool(
        lambda: str(repo),
        read_policy_factory=permission_read_policy_factory(dict),
    )
    out = asyncio.run(tool.invoke({"pattern": SECRET}))
    assert not out.success
    assert "project policy invalid" in out.error


def test_real_project_policy_module(repo: Path):
    pytest.importorskip(_PROJECT_POLICY_MODULE)
    (repo / ".jiuwenswarm").mkdir()
    (repo / ".jiuwenswarm" / "permissions.yaml").write_text(
        "deny_read:\n  - secret/**\n", encoding="utf-8"
    )
    policy = build_read_policy(repo, project_permissions={})
    assert policy.hidden_reason(repo / "secret" / "keys.py")
    assert policy.allows(repo / "src" / "app.py")


# ─── no content oracle (desktop test follow-up) ──────────────────
#
# A count of hidden files (or a note that appears only when something was
# dropped) lets a search for a guessed string reveal whether a denied file
# contains it. The answer for a string found only in a denied file must be
# byte-identical to the answer for a string found nowhere.

ONLY_DENIED = "ONLYDENIED-7f3a"  # present only in secret/only.py
NOWHERE = "NOWHERE-0000000"  # present nowhere; same length


@pytest.fixture
def oracle_repo(repo: Path) -> Path:
    (repo / "secret" / "only.py").write_text(
        f"TOKEN = '{ONLY_DENIED}'\ndef only_denied_fn():\n    pass\n", encoding="utf-8"
    )
    return repo


def _same(present: str, absent: str, token_present: str, token_absent: str) -> None:
    assert present.replace(token_present, "<Q>") == absent.replace(token_absent, "<Q>")


@pytest.mark.parametrize("case", ["smart", "insensitive"])
def test_code_search_does_not_reveal_denied_content(
    oracle_repo: Path, tmp_path: Path, case: str
):
    tool = CodeSearchTool(
        lambda: str(oracle_repo),
        read_policy_factory=_engine_factory(_template_permissions()),
    )
    runs = {
        token: asyncio.run(
            _run_tool_call(
                tool,
                "code_search",
                json.dumps({"pattern": token, "case": case}),
                tmp_path / f"ws-{token}",
            )
        )
        for token in (ONLY_DENIED, NOWHERE)
    }
    _same(runs[ONLY_DENIED], runs[NOWHERE], ONLY_DENIED, NOWHERE)
    assert READ_POLICY_NOTICE in runs[NOWHERE]
    assert "only.py" not in runs[ONLY_DENIED]


def test_code_search_notice_is_fixed_even_when_nothing_is_hidden(repo: Path):
    tool = CodeSearchTool(
        lambda: str(repo), read_policy_factory=_engine_factory(_template_permissions())
    )
    out = asyncio.run(tool.invoke({"pattern": "return 1"}))  # only in src/app.py
    assert out.success and "src/app.py" in out.data["content"]
    assert READ_POLICY_NOTICE in out.data["content"]
    assert out.data["warning"] == READ_POLICY_NOTICE


def test_code_search_without_policy_has_no_notice(repo: Path):
    out = asyncio.run(CodeSearchTool(lambda: str(repo)).invoke({"pattern": "return 1"}))
    assert READ_POLICY_NOTICE not in out.data["content"]


def test_project_policy_search_does_not_reveal_denied_content(
    oracle_repo: Path, stub_project_policy
):
    stub_project_policy["denies"] = ["secret"]
    tool = CodeSearchTool(
        lambda: str(oracle_repo),
        read_policy_factory=permission_read_policy_factory(lambda: {"enabled": False}),
    )
    present = asyncio.run(tool.invoke({"pattern": ONLY_DENIED})).data
    absent = asyncio.run(tool.invoke({"pattern": NOWHERE})).data
    assert present["content"] == absent["content"]
    assert {k: v for k, v in present.items() if k != "pattern"} == {
        k: v for k, v in absent.items() if k != "pattern"
    }


def test_find_symbol_regex_does_not_reveal_denied_definitions(
    oracle_repo: Path, tmp_path: Path
):
    factory = _engine_factory(_template_permissions())
    engine = CodeSearchEngine(lambda: str(oracle_repo), read_policy_factory=factory)
    tool = FindSymbolTool(lambda: str(oracle_repo), engine=engine)
    present_name, absent_name = "only_denied_fn", "absent_zzz_fn"
    runs = {
        name: asyncio.run(
            _run_tool_call(
                tool,
                "find_symbol",
                json.dumps({"name": name, "kind": "all"}),
                tmp_path / f"ws-{name}",
            )
        )
        for name in (present_name, absent_name)
    }
    _same(runs[present_name], runs[absent_name], present_name, absent_name)


class _ScriptedLsp(LspBridge):
    """Language server stand-in: answers from a script, empty for unknown names."""

    def __init__(self, repo: Path, denied_only_name: str) -> None:
        super().__init__()
        self._repo = repo
        self._name = denied_only_name

    async def available(self, file_path: Path) -> bool:
        return file_path.suffix == ".py"

    async def call(self, inputs, workspace):
        op = inputs["operation"]
        query = inputs.get("query", "")
        if op == "workspaceSymbol":
            if query == self._name:
                return True, f"{self._repo}/secret/only.py:\n  2: Function {self._name}"
            return True, "No symbols found."
        if op == "findReferences":
            if (
                self._name in str(inputs.get("file_path", ""))
                or inputs.get("line") == 2
            ):
                return True, f"{self._repo}/secret/only.py:\n  2:5\n  7:1"
            return True, "No references found."
        return False, "unsupported"


def test_find_symbol_lsp_answers_identically_when_everything_is_hidden(
    oracle_repo: Path,
):
    factory = _engine_factory(_template_permissions())
    present_name, absent_name = "only_denied_fn", "absent_zzz_fn"
    tool = FindSymbolTool(
        lambda: str(oracle_repo),
        read_policy_factory=factory,
        lsp=_ScriptedLsp(oracle_repo, present_name),
    )
    # definitions via workspaceSymbol (language set, so the LSP is consulted)
    defs = {
        name: asyncio.run(
            tool.invoke({"name": name, "kind": "definition", "language": "python"})
        )
        for name in (present_name, absent_name)
    }
    _same(
        defs[present_name].data["content"],
        defs[absent_name].data["content"],
        present_name,
        absent_name,
    )
    assert "only.py" not in defs[present_name].data["content"]
    # references via findReferences from an allowed anchor
    refs = {
        line: asyncio.run(
            tool.invoke(
                {
                    "name": "x",
                    "kind": "references",
                    "file_path": "src/app.py",
                    "line": line,
                }
            )
        )
        for line in (2, 3)
    }
    assert refs[2].data["content"] == refs[3].data["content"]
    assert "only.py" not in refs[2].data["content"]
