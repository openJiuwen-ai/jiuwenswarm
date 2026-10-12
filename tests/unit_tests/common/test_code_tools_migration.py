"""New default Code tools reach existing config.yaml files, once each.

Every test works on files inside ``tmp_path``. The real shipped template is
read (never written); the "merged tree" template is a copy of it with the new
tools appended, as the branches that ship those tools do.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from jiuwenswarm.common import utils as common_utils
from jiuwenswarm.common._build_config import VERSION
from jiuwenswarm.common.config import (
    CODE_TOOLS_INTRODUCED_SINCE_TEMPLATE,
    CODE_TOOLS_OFFERED_STATE_FILE,
    offer_new_code_tools,
)
from jiuwenswarm.common.utils import (
    _find_config_template_path,
    ensure_config_migrated_from_template,
    ensure_new_code_tools_offered,
)


@contextlib.contextmanager
def _capture_logger(caplog, name: str, level: int = logging.WARNING):
    """Capture records from logger ``name`` on any pytest version.

    ``setup_logger()`` sets ``propagate = False`` on the ``jiuwenswarm`` logger,
    so records never reach the root handler that ``caplog`` installs on the
    pytest pinned by uv.lock (9.0); pytest 9.1 also attaches to non-propagating
    loggers. Attach caplog's handler to the emitting logger itself (as the base
    config tests do) and stop propagation meanwhile, so each record is
    captured once.
    """
    target = logging.getLogger(name)
    added = caplog.handler not in target.handlers
    saved = (target.propagate, target.level, caplog.handler.level)
    if added:
        target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(level)
    caplog.handler.setLevel(level)
    try:
        yield
    finally:
        if added:
            target.removeHandler(caplog.handler)
        target.propagate = saved[0]
        target.setLevel(saved[1])
        caplog.handler.setLevel(saved[2])


# The list an install created before these tools existed carries (the shape of
# a real desktop user's config on 2026-09-30).
EXISTING_TOOLS = [
    "web_free_search",
    "web_fetch_webpage",
    "web_paid_search",
    "user_todos",
    "skill_toolkit",
    "skill_retrieval",
]
NEW_TOOLS = ["git_prepare", "code_search", "find_symbol", "symbol_outline", "run_tests"]

USER_CONFIG = """\
# my own comment at the top
config_version: {version}
custom_operator_key: keep-me
modes:
  code:
    # tools I use
    tools:
{tools}
    rails: []
"""


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    return y


def _write_user_config(
    path: Path, tools: list[str] | None, version: str = "0.0.1"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tools is None:
        path.write_text(
            f"config_version: {version}\nmodes:\n  code:\n    rails: []\n",
            encoding="utf-8",
        )
    else:
        listed = "\n".join(f"      - {t}" for t in tools)
        path.write_text(
            USER_CONFIG.format(version=version, tools=listed), encoding="utf-8"
        )
    return path


def _user_tools(path: Path) -> list[str]:
    data = _yaml().load(path.read_text(encoding="utf-8"))
    return list(data["modes"]["code"]["tools"])


def _template_with(tmp_path: Path, extra: list[str]) -> Path:
    """Copy the shipped template with exactly ``extra`` of the new tools.

    The new tools are removed first, so the result is the same whether or not
    the shipped template already carries them (it does once #8/#11/#16 merge).
    """
    src = _find_config_template_path()
    data = _yaml().load(src.read_text(encoding="utf-8"))
    tools = data["modes"]["code"]["tools"]
    for name in NEW_TOOLS:
        while name in tools:
            tools.remove(name)
    for name in extra:
        if name not in tools:
            tools.append(name)
    out = tmp_path / "template" / "config.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        _yaml().dump(data, fh)
    return out


@pytest.fixture
def user_config(tmp_path: Path) -> Path:
    return _write_user_config(
        tmp_path / "ws" / "config" / "config.yaml", EXISTING_TOOLS
    )


def _state(path: Path) -> dict:
    return json.loads(
        path.with_name(CODE_TOOLS_OFFERED_STATE_FILE).read_text(encoding="utf-8")
    )


def test_opt_in_tools_are_never_offered() -> None:
    assert "git_publish" not in CODE_TOOLS_INTRODUCED_SINCE_TEMPLATE
    assert "git_review" not in CODE_TOOLS_INTRODUCED_SINCE_TEMPLATE
    assert list(CODE_TOOLS_INTRODUCED_SINCE_TEMPLATE) == NEW_TOOLS


def test_existing_six_tool_config_gets_the_five_new_tools(
    tmp_path: Path, user_config: Path
) -> None:
    template = _template_with(tmp_path, NEW_TOOLS)

    added = offer_new_code_tools(template, user_config)

    assert added == NEW_TOOLS
    assert _user_tools(user_config) == EXISTING_TOOLS + NEW_TOOLS
    assert len(_user_tools(user_config)) == 11
    assert sorted(_state(user_config)["offered"]) == sorted(NEW_TOOLS)


def test_comments_and_other_keys_survive(tmp_path: Path, user_config: Path) -> None:
    template = _template_with(tmp_path, NEW_TOOLS)

    offer_new_code_tools(template, user_config)

    text = user_config.read_text(encoding="utf-8")
    assert "# my own comment at the top" in text
    assert "# tools I use" in text
    assert "custom_operator_key: keep-me" in text
    data = _yaml().load(text)
    assert data["modes"]["code"]["rails"] == []
    assert data["config_version"] == "0.0.1"


def test_noop_with_shipped_template_that_lacks_the_tools(
    tmp_path: Path, user_config: Path
) -> None:
    """On a tree without the tool MRs the shipped template has none of them."""
    template = _find_config_template_path()
    shipped = _yaml().load(template.read_text(encoding="utf-8"))["modes"]["code"][
        "tools"
    ]
    if any(t in shipped for t in NEW_TOOLS):
        pytest.skip("shipped template already carries new tools on this tree")
    before = user_config.read_bytes()

    assert offer_new_code_tools(template, user_config) == []
    assert user_config.read_bytes() == before
    assert not user_config.with_name(CODE_TOOLS_OFFERED_STATE_FILE).exists()


def test_only_template_tools_are_added_and_later_template_tools_follow(
    tmp_path: Path, user_config: Path
) -> None:
    first = _template_with(tmp_path / "a", ["code_search", "git_prepare"])
    assert offer_new_code_tools(first, user_config) == ["git_prepare", "code_search"]
    assert "run_tests" not in _user_tools(user_config)
    assert sorted(_state(user_config)["offered"]) == ["code_search", "git_prepare"]

    # A later version ships run_tests in the template: it is offered then.
    later = _template_with(tmp_path / "b", ["code_search", "git_prepare", "run_tests"])
    assert offer_new_code_tools(later, user_config) == ["run_tests"]
    assert _user_tools(user_config)[-1] == "run_tests"


def test_second_start_changes_nothing(tmp_path: Path, user_config: Path) -> None:
    template = _template_with(tmp_path, NEW_TOOLS)
    offer_new_code_tools(template, user_config)
    config_bytes = user_config.read_bytes()
    state_bytes = user_config.with_name(CODE_TOOLS_OFFERED_STATE_FILE).read_bytes()

    assert offer_new_code_tools(template, user_config) == []
    assert user_config.read_bytes() == config_bytes
    assert (
        user_config.with_name(CODE_TOOLS_OFFERED_STATE_FILE).read_bytes() == state_bytes
    )


def test_removed_tool_is_not_added_back(tmp_path: Path, user_config: Path) -> None:
    template = _template_with(tmp_path, NEW_TOOLS)
    offer_new_code_tools(template, user_config)

    data = _yaml().load(user_config.read_text(encoding="utf-8"))
    data["modes"]["code"]["tools"].remove("run_tests")
    with open(user_config, "w", encoding="utf-8") as fh:
        _yaml().dump(data, fh)

    assert offer_new_code_tools(template, user_config) == []
    assert "run_tests" not in _user_tools(user_config)


def test_tool_the_user_already_had_is_recorded_not_duplicated(tmp_path: Path) -> None:
    config = _write_user_config(
        tmp_path / "ws" / "config" / "config.yaml", EXISTING_TOOLS + ["code_search"]
    )
    template = _template_with(tmp_path, NEW_TOOLS)

    added = offer_new_code_tools(template, config)

    assert "code_search" not in added
    assert _user_tools(config).count("code_search") == 1
    assert "code_search" in _state(config)["offered"]


def test_noop_when_user_config_has_no_tools_list(tmp_path: Path) -> None:
    config = _write_user_config(tmp_path / "ws" / "config" / "config.yaml", None)
    before = config.read_bytes()
    template = _template_with(tmp_path, NEW_TOOLS)

    assert offer_new_code_tools(template, config) == []
    assert config.read_bytes() == before
    assert not config.with_name(CODE_TOOLS_OFFERED_STATE_FILE).exists()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"offered": "run_tests"}),
        json.dumps(["run_tests"]),
        json.dumps({"offered": [1]}),
    ],
)
def test_unreadable_state_file_changes_nothing(
    tmp_path: Path, user_config: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    state = user_config.with_name(CODE_TOOLS_OFFERED_STATE_FILE)
    state.write_text(content, encoding="utf-8")
    before = user_config.read_bytes()
    template = _template_with(tmp_path, NEW_TOOLS)

    with _capture_logger(caplog, offer_new_code_tools.__module__):
        assert offer_new_code_tools(template, user_config) == []

    assert user_config.read_bytes() == before
    assert state.read_text(encoding="utf-8") == content
    assert "Delete the file" in caplog.text


def test_runs_even_when_config_version_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    config = _write_user_config(
        workspace / "config" / "config.yaml", EXISTING_TOOLS, VERSION
    )
    template = _template_with(tmp_path, NEW_TOOLS)
    monkeypatch.setattr(common_utils, "_find_config_template_path", lambda: template)

    # The template merge short-circuits on a current version ...
    assert ensure_config_migrated_from_template(workspace) is False
    # ... the tools step still runs.
    assert ensure_new_code_tools_offered(workspace) == NEW_TOOLS
    assert _user_tools(config) == EXISTING_TOOLS + NEW_TOOLS


def test_wrapper_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    _write_user_config(workspace / "config" / "config.yaml", EXISTING_TOOLS)

    def boom() -> Path:
        raise RuntimeError("config.yaml template not found")

    monkeypatch.setattr(common_utils, "_find_config_template_path", boom)
    assert ensure_new_code_tools_offered(workspace) == []


def test_wrapper_skips_missing_config(tmp_path: Path) -> None:
    assert ensure_new_code_tools_offered(tmp_path / "nowhere") == []


def test_runtime_startup_runs_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prepare_runtime_workspace`` (desktop, app, gateway, AgentServer) runs it."""
    workspace = tmp_path / "runtime"
    config = _write_user_config(
        workspace / "config" / "config.yaml", EXISTING_TOOLS, VERSION
    )
    (workspace / "agent" / "workspace" / "mcp" / "mcp_builtins").mkdir(parents=True)
    template = _template_with(tmp_path, NEW_TOOLS)

    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: workspace)
    monkeypatch.setattr(common_utils, "cleanup_team_files", lambda _workspace: None)
    monkeypatch.setattr(
        common_utils, "mcp_builtins_seed_update_needed", lambda _workspace: False
    )
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)
    monkeypatch.setattr(common_utils, "_find_config_template_path", lambda: template)

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert _user_tools(config) == EXISTING_TOOLS + NEW_TOOLS


def test_runtime_startup_without_migrate_config_skips_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "runtime"
    config = _write_user_config(
        workspace / "config" / "config.yaml", EXISTING_TOOLS, VERSION
    )
    (workspace / "agent" / "workspace" / "mcp" / "mcp_builtins").mkdir(parents=True)
    template = _template_with(tmp_path, NEW_TOOLS)

    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: workspace)
    monkeypatch.setattr(common_utils, "cleanup_team_files", lambda _workspace: None)
    monkeypatch.setattr(
        common_utils, "mcp_builtins_seed_update_needed", lambda _workspace: False
    )
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)
    monkeypatch.setattr(common_utils, "_find_config_template_path", lambda: template)

    common_utils.prepare_runtime_workspace(
        cleanup_stale_descs=False, migrate_config=False
    )

    assert _user_tools(config) == EXISTING_TOOLS
