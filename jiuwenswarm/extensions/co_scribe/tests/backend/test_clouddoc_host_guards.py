"""Host-layer guards for co-scribe: the file fence and the routing titles.

Both enforce at the layer that owns the resource (the 2026-08-13 principle,
generalized): the mandate machinery guards the cloud document; these guard the
host's side of it -- its own persisted files against generic tools, and the
model's tool routing against file-looking titles.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as _NS

import pytest

from jiuwenswarm.agents.harness.common.rails.clouddoc_file_guard_rail import (
    _is_guarded_cli_command,
    _shell_tools,
    CloudDocFileGuardRail,
    _is_guarded_path,
)


class _Ctx:
    def __init__(self, tool_name, tool_args):
        self.inputs = {"tool_name": tool_name, "tool_args": tool_args,
                       "tool_call": _NS(id="tc-1")}
        self.extra = {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/home/u/.jiuwenswarm/config/clouddoc-workmode.md",
    "~/.jiuwenswarm/config/clouddoc-receipts.json",
    "clouddoc-watches.json",
    "/x/clouddoc-watches-audit.jsonl",
    "/home/u/.jiuwenswarm/config/clouddoc-keys/feishu.json",
    "C:\\\\u\\\\clouddoc-state.json",
])
async def test_generic_writes_to_coscribe_files_are_refused(path):
    ctx = _Ctx("edit_file", {"file_path": path, "new_string": "x", "old_string": "y"})
    await CloudDocFileGuardRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "CLOUDDOC_FILE_GUARDED" in ctx.inputs["tool_result"]
    assert "clouddoc_workmode_edit" in ctx.inputs["tool_msg"].content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("read_file", {"file_path": "/home/u/.jiuwenswarm/config/clouddoc-workmode.md"}),
    ("edit_file", {"file_path": "/home/u/project/clouddocs.md"}),
    ("edit_file", {"file_path": "/home/u/notes/readme.md"}),
    ("write_file", {"file_path": "/home/u/project/my-clouddoc-notes/plan.md"}),
    ("write_file", {"file_path": "/home/u/project/clouddoc-notes.md"}),
])
async def test_reads_and_unrelated_paths_pass(tool, args):
    ctx = _Ctx(tool, args)
    await CloudDocFileGuardRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", [
    # Observed live 2026-09-10: the toolkit's read failed for want of a scope, the
    # model said it would "see whether the local lark-cli can create a sheet", and
    # did -- a worksheet appeared in the owner's spreadsheet with no receipt.
    "lark-cli sheets +sheet-create --spreadsheet-token X --title 云文档台账",
    "lark-cli sheets +cells-set --spreadsheet-token X --range A1 --values 1",
    "lark-cli drive +add-reply --token X --comment-id C --content Y",
    # The shape a model reaches for once a bare call is refused.
    "echo start && lark-cli sheets +sheet-create --title T",
    # A verb this build has never heard of must refuse, not pass: that is the whole
    # reason the read side is an allow-list.
    "lark-cli sheets +some-future-verb --token X",
    # The bare read marker "update" (meant for the CLI's self-update) let every
    # ``+update`` write through; the marker is anchored to the binary name now.
    "lark-cli docs +update --doc X --command str_replace --pattern a --content b",
    "lark-cli slides +update-slide --presentation X --slide-id p1",
    "lark-cli drive +update-reply --token X --comment-id C --reply-id R --content Y",
])
async def test_platform_cli_writes_through_a_shell_are_refused(cmd):
    ctx = _Ctx("bash", {"command": cmd})
    await CloudDocFileGuardRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "CLOUDDOC_CLI_GUARDED" in ctx.inputs["tool_result"]
    # The refusal has to name the road back, or the model invents one.
    assert "clouddoc_write_region" in ctx.inputs["tool_msg"].content


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", [
    "lark-cli sheets +cells-get --spreadsheet-token X --range A1:Z30",
    "lark-cli drive +list-comments --token X --type wiki",
    "lark-cli drive metas batch_query --data {}",
    "lark-cli sheets --help",
    "lark-cli auth status",
    "lark-cli update",
    "ls -la /tmp",
    "python -c 'print(1)'",
])
async def test_reads_and_unrelated_commands_pass(cmd):
    """Reads change nothing and owe the ledger nothing, so they stay allowed."""
    ctx = _Ctx("bash", {"command": cmd})
    await CloudDocFileGuardRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is None


def test_guarded_path_shapes():
    assert _is_guarded_path("clouddoc-watches.json")
    assert _is_guarded_path("x/clouddoc-watches.json.lock")
    assert _is_guarded_path("x/clouddoc-receipts.tmp")
    assert _is_guarded_path("/x/clouddoc-watches-audit.jsonl")
    assert _is_guarded_path("a/clouddoc-keys/k.json")
    assert _is_guarded_path("a/clouddoc-keys")  # the key dir itself is guarded too
    # Exact stems, not a prefix: a person's own file is not mechanism state.
    assert not _is_guarded_path("clouddoc-notes.md")
    assert not _is_guarded_path("clouddoc-anything.json")
    assert not _is_guarded_path("myclouddoc-notes.md")
    assert not _is_guarded_path(None)


@pytest.mark.parametrize("cmd", [
    # A write chained behind a read marker, or a marker inside an ordinary
    # argument, used to pass the substring check. The judgment is per argv now.
    "lark-cli --version && lark-cli sheets +cells-set --spreadsheet-token X --range A1 --values 1",
    "lark-cli sheets +cells-get --spreadsheet-token X --range A1 && lark-cli sheets +cells-set --spreadsheet-token X --range A1 --values 1",
    "lark-cli sheets +sheet-create --title config",
    "lark-cli sheets +sheet-create --title +get",
    "lark-cli sheets +cells-get --range A1 | lark-cli sheets +cells-set --range A1",
    "lark-cli sheets +cells-get --range $(lark-cli sheets +sheet-create --title T)",
    "bash -c 'lark-cli sheets +cells-set --range A1 --values 1'",
    "lark-cli --profile p sheets +cells-get --range A1",
])
def test_mixed_commands_and_marker_arguments_are_refused(cmd):
    assert _is_guarded_cli_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "lark-cli drive permission.members auth --token X --type docx --action edit",
    "lark-cli sheets +cells-set --help",
    "which lark-cli",
    "/home/u/.local/bin/lark-cli drive +list-comments --token X --type docx",
    "lark-cli",
])
def test_reads_are_still_admitted_by_position(cmd):
    assert _is_guarded_cli_command(cmd) is False


def _adopted(tmp_path, monkeypatch, count):
    """Persist ``count`` titled documents where the panel keeps them; return their ids."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers import kinds as kinds_mod

    state = tmp_path / "clouddoc-state.json"
    docs = {f"d{i}": {"panel_meta": {"title": f"文档{i}"}} for i in range(count)}
    # The live file's top level IS the doc map (no wrapper) -- the shape that
    # slipped past the first version of adopted_titles. Assert on it directly.
    state.write_text(json.dumps(docs), encoding="utf-8")
    monkeypatch.setattr(kinds_mod, "get_clouddoc_state_path", lambda: state)
    return list(docs)


def test_the_titles_note_names_the_adopted_documents_and_is_capped(tmp_path, monkeypatch):
    """The routing evidence: registered titles, capped, and nothing when none exist."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.kinds import adopted_titles_note

    ids = _adopted(tmp_path, monkeypatch, 14)
    note = adopted_titles_note(ids)
    assert "《文档0》" in note and "等 14 篇" in note
    assert "《文档13》" not in note  # capped at 12
    assert adopted_titles_note([]) == ""


def test_tool_cards_do_not_change_with_what_is_adopted(tmp_path, monkeypatch):
    """Tool schemas open the cached prefix, so adoption must not reach into them:
    two deployments that differ only in their documents present identical cards."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import CloudDocToolkit

    ids = _adopted(tmp_path, monkeypatch, 3)

    class _P:
        kind = "fake"
        receipt_sink = None
        def parse_doc_ref(self, r): return r
        def doc_url(self, d, k=""): return d

    def cards(watched):
        kit = CloudDocToolkit(_P(), turn_address=lambda: "x", watched_docs=lambda: watched)
        return [(t.card.name, t.card.description, json.dumps(t.card.input_params, sort_keys=True))
                for t in kit.get_tools()]

    assert cards(ids) == cards([])
    assert not any("文档0" in description for _, description, _ in cards(ids))


class _Attachments:
    """The slice of the host's prompt-attachment manager the rail uses."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.sections: dict[str, str] = {}

    def bind_context(self, ctx):
        return self

    async def add_section(self, section, content, kind, source, **_):
        self.sections[section] = content

    async def clear_section(self, section):
        return 1 if self.sections.pop(section, None) is not None else 0


def _enable_clouddoc(monkeypatch, documents):
    import jiuwenswarm.common.config as config_mod

    cfg = {"clouddoc": {"enabled": True, "connections": [
        {"provider": "google", "credentials_file": "/k/sa.json", "documents": list(documents)},
    ]}}
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)


@pytest.mark.asyncio
async def test_a_chat_turn_gets_the_titles_as_a_prompt_attachment(tmp_path, monkeypatch):
    ids = _adopted(tmp_path, monkeypatch, 2)
    _enable_clouddoc(monkeypatch, ids)
    manager = _Attachments("sess-chat")
    ctx = _NS(agent=_NS(prompt_attachment_manager=manager), inputs={}, extra={})

    await CloudDocFileGuardRail().before_invoke(ctx)
    assert "《文档0》" in manager.sections["clouddoc.adopted_titles"]

    # Nothing adopted any more: the note is withdrawn rather than left stale.
    _enable_clouddoc(monkeypatch, [])
    await CloudDocFileGuardRail().before_invoke(ctx)
    assert manager.sections == {}


@pytest.mark.asyncio
async def test_an_unattended_turn_gets_no_document_list(tmp_path, monkeypatch):
    """It is bound to one document; the others are none of its business."""
    ids = _adopted(tmp_path, monkeypatch, 2)
    _enable_clouddoc(monkeypatch, ids)
    manager = _Attachments("clouddoc_d0_1")
    ctx = _NS(agent=_NS(prompt_attachment_manager=manager), inputs={}, extra={})

    await CloudDocFileGuardRail().before_invoke(ctx)
    assert manager.sections == {}


@pytest.mark.asyncio
async def test_a_host_without_attachments_still_runs_the_turn():
    await CloudDocFileGuardRail().before_invoke(_NS(agent=_NS(), inputs={}, extra={}))


# ------------------------- review: the tools the host actually registers, and the wrappers


_WRITE = "lark-cli sheets +cells-set --spreadsheet-token X --range A1 --values 1"
_READ = "lark-cli sheets +cells-get --spreadsheet-token X --range A1"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,key", [
    ("mcp_exec_command", "command"),     # the host's cross-platform command tool
    ("create_terminal", "cmd"),          # the ACP terminal tool
    ("powershell", "command"),
    ("cmd", "command"),
])
async def test_the_registered_command_tools_are_guarded(tool, key):
    """The rail used to name a synthetic ``bash`` and miss the two tools the host
    registers, so the write below passed through both of them unchanged."""
    ctx = _Ctx(tool, {key: _WRITE})
    await CloudDocFileGuardRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
    assert "CLOUDDOC_CLI_GUARDED" in ctx.inputs["tool_result"]
    ok = _Ctx(tool, {key: _READ})
    await CloudDocFileGuardRail().before_tool_call(ok)
    assert ok.extra.get("_skip_tool") is None


def test_the_rail_covers_the_hosts_own_shell_classification():
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
        _SHELL_TOOLS as host_shell_tools,
    )

    assert set(host_shell_tools) <= _shell_tools()
    assert {"mcp_exec_command", "create_terminal"} <= _shell_tools()


@pytest.mark.parametrize("cmd", [
    "command lark-cli sheets +cells-set --range A1 --values 1",   # ``command`` runs it
    "exec lark-cli sheets +cells-set --range A1 --values 1",
    "env LARK_PROFILE=p lark-cli sheets +cells-set --range A1 --values 1",
    "LARK_PROFILE=p lark-cli sheets +cells-set --range A1 --values 1",
    "nohup lark-cli sheets +cells-set --range A1 --values 1",
    "timeout 30 lark-cli sheets +cells-set --range A1 --values 1",
    "env -i lark-cli sheets +cells-get --range A1",                # an option the wrapper eats: not judged
])
def test_execution_wrappers_do_not_hide_a_write(cmd):
    assert _is_guarded_cli_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "command -v lark-cli",
    "command -V lark-cli",
    "command lark-cli sheets +cells-get --range A1",
    "env LARK_PROFILE=p lark-cli drive +list-comments --token X --type docx",
    "timeout 30 lark-cli sheets +cells-get --range A1",
])
def test_wrapped_reads_and_inspections_still_pass(cmd):
    assert _is_guarded_cli_command(cmd) is False
