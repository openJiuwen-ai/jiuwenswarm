"""Offline tests for the lark-cli seam: classification and the process it runs.

None of these needs a tenant. The classification cases feed it the envelopes and
stderr the CLI really produces; the command checks run the installed binary with
``--dry-run``, which validates verbs and flags without calling the platform; the
process cases use a shell stand-in for the binary.
"""

from __future__ import annotations

import json
import os

import pytest

from jiuwenswarm.clouddoc.providers.feishu.cli import LarkCli, _classify
from jiuwenswarm.clouddoc.providers.base import ProviderError


# --------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    "stderr,kind",
    [
        ("permission denied", "forbidden"),
        ("document not found", "not_found"),
        ("429 too many requests", "rate_limited"),
        ("request timed out", "transport"),
        ("invalid token", "auth"),
        ("something nobody predicted", "unknown"),
    ],
)
def test_failures_map_onto_the_shared_error_vocabulary(stderr, kind):
    """A Feishu failure should reach a person as the same kind of sentence a Google
    failure does, and an unrecognised one is unknown rather than guessed at.
    """
    assert _classify(1, stderr).kind == kind


# ------------------------------------------------- the commands the real CLI accepts


def _lark_binary() -> str | None:
    import shutil

    return shutil.which("lark-cli")


def _envelope(res) -> dict:
    """The JSON envelope a command answered with, failing if it gave none.

    A run that printed nothing would otherwise read as ``{}``, whose missing
    ``error.type`` passes every check below without the CLI having judged anything.
    """
    import json as _json

    text = (res.stderr or res.stdout or "").strip()
    assert text, f"CLI 没有输出（退出码 {res.returncode}）"
    payload = _json.loads(text)
    assert "ok" in payload, f"不是 CLI 的信封：{text[:200]}"
    return payload


# Every command this provider issues, as it issues it. Checked against the installed
# binary rather than against a help text pasted into a comment, so a flag that is
# renamed upstream fails here instead of at a customer's first write.
_ISSUED_COMMANDS = [
    ["docs", "+fetch", "--doc", "T", "--detail", "with-ids", "--scope", "full"],
    ["docs", "+update", "--doc", "T", "--command", "str_replace",
     "--pattern", "old", "--content", "new", "--doc-format", "markdown",
     "--revision-id", "5"],
    ["docs", "+update", "--doc", "T", "--command", "append",
     "--content", "x", "--doc-format", "markdown"],
    ["docs", "+fetch", "--doc", "T", "--doc-format", "markdown"],
    ["docs", "+create", "--title", "T"],
    ["drive", "+list-comments", "--token", "T", "--type", "docx",
     "--need-relation", "--solved-status", "all", "--page-size", "100", "--page-token", "P"],
    ["drive", "+list-replies", "--token", "T", "--type", "docx",
     "--comment-id", "c1", "--page-size", "100", "--page-token", "P"],
    ["drive", "+add-reply", "--token", "T", "--type", "docx",
     "--comment-id", "c1", "--content", '[{"type":"text","text":"hi"}]'],
    ["drive", "+member-list", "--token", "T", "--type", "docx"],
    ["drive", "permission.members", "auth", "--token", "T", "--type", "docx", "--action", "edit"],
    ["drive", "+member-add", "--token", "T", "--type", "docx",
     "--member-id", "ou_x", "--member-type", "openid", "--perm", "edit", "--yes"],
]


@pytest.mark.skipif(_lark_binary() is None, reason="lark-cli 未安装")
@pytest.mark.parametrize("args", _ISSUED_COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_the_cli_accepts_every_command_the_provider_issues(args):
    """--dry-run parses and validates without calling the platform, so this needs no
    tenant: an unknown flag comes back as "validation", while a well-formed call gets
    as far as "config"/not_configured, which is the credentials talking, not the
    command.
    """
    import subprocess

    res = subprocess.run(
        [_lark_binary(), *args, "--as", "bot", "--dry-run"],
        capture_output=True, text=True, timeout=60,
    )
    payload = _envelope(res)
    kind = (payload.get("error") or {}).get("type")
    assert kind != "validation", f"CLI 拒绝了这条命令：{payload.get('error')}"


@pytest.mark.skipif(_lark_binary() is None, reason="lark-cli 未安装")
def test_a_wrong_flag_is_actually_caught_by_that_check():
    """The check above is only worth having if it fails on a bad command. --text is
    what this provider used before the CLI was read; block content is --content.
    """
    import subprocess

    res = subprocess.run(
        [_lark_binary(), "docs", "+update", "--doc", "T", "--command", "block_replace",
         "--block-id", "0", "--text", "x", "--as", "bot", "--dry-run"],
        capture_output=True, text=True, timeout=60,
    )
    payload = _envelope(res)
    assert (payload.get("error") or {}).get("type") == "validation"


@pytest.mark.skipif(_lark_binary() is None, reason="lark-cli 未安装")
def test_the_error_envelope_is_read_from_its_fields_not_its_prose(tmp_path):
    """Classification reads error.type, because prose is what changes between
    releases. Run against the real binary with no credentials configured.
    """
    import subprocess

    # An unconfigured store: every place the CLI may look for one points at an empty
    # directory, so a store configured on the machine running this cannot answer.
    empty = str(tmp_path)
    env = {**os.environ, "HOME": empty, "USERPROFILE": empty, "APPDATA": empty,
           "XDG_CONFIG_HOME": empty, "LARKSUITE_CLI_CONFIG_DIR": empty}
    res = subprocess.run(
        [_lark_binary(), "whoami", "--as", "bot"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    err = _classify(res.returncode, res.stderr)
    assert err.kind == "auth"
    # The hint is the actionable half and must survive into the message.
    assert "config init" in str(err)


# ------------------------------------------- an environment the host prepared


def _fake_cli(directory, body: str):
    """A ``lark-cli`` stand-in: a shell script, so these run without the real binary."""
    import os

    path = directory / "lark-cli"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX shell script")
@pytest.mark.asyncio
async def test_an_injected_environment_selects_the_binary_and_its_profile(tmp_path):
    """JiuwenMate hands out its private binary and per-agent profile as an environment
    (PATH and LARKSUITE_CLI_CONFIG_DIR). The binary is looked up on that PATH, and the
    command runs with that environment, so the provider needs no host import.
    """
    bindir = tmp_path / "private-bin"
    bindir.mkdir()
    _fake_cli(bindir, 'printf \'{"ok": true, "data": {"config": "%s", "as": "%s"}}\' '
                      '"$LARKSUITE_CLI_CONFIG_DIR" "$2"\n')
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin",
           "LARKSUITE_CLI_CONFIG_DIR": str(tmp_path / "agent-profile")}

    cli = LarkCli(env=env)
    assert cli.available
    assert await cli.json(["whoami"]) == {"config": str(tmp_path / "agent-profile"), "as": "bot"}

    # Without the injected PATH the private binary is not there to find.
    assert not LarkCli(env={"PATH": "/nonexistent"}).available


async def _child_of_a_hung_command(tmp_path, stop, *, launcher_waits=True):
    """Run a stand-in that starts a child the way the npm launcher starts the real
    binary, stop it with ``stop``, and return whether that child is still running.

    With ``launcher_waits`` false the stand-in exits straight after starting the
    child, which keeps the output pipes open, so the command still hangs with its
    launcher already gone.
    """
    import asyncio

    pidfile = tmp_path / "child.pid"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tail = "wait\n" if launcher_waits else "exit 0\n"
    _fake_cli(bindir, f'sleep 30 &\necho $! > "{pidfile}"\n{tail}')
    cli = LarkCli(env={"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin"})
    task = asyncio.ensure_future(cli.run(["whoami"], timeout=stop.get("timeout")))
    for _ in range(250):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        if task.done():
            break
        await asyncio.sleep(0.02)
    assert pidfile.exists() and pidfile.read_text().strip(), (
        "the stand-in never started its child; the check below would prove nothing"
    )
    child = int(pidfile.read_text())
    if stop.get("cancel"):
        task.cancel()
    # Each stop has its own outcome, and the two must not be confused: a timeout
    # is a transport failure the caller may retry, a cancellation propagates as
    # itself so the turn it belonged to ends.
    if stop.get("cancel"):
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ProviderError) as caught:
            await task
        assert caught.value.kind == "transport"
    import psutil

    # A killed child can linger briefly as a zombie until its new parent reaps it;
    # that is not running.
    for _ in range(50):
        try:
            if psutil.Process(child).status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        await asyncio.sleep(0.02)
    try:
        os.kill(child, 9)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX shell script")
@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [{"timeout": 0.5}, {"cancel": True, "timeout": 30}],
                         ids=["timeout", "cancel"])
async def test_a_stopped_command_takes_its_children_with_it(tmp_path, stop):
    """The npm installation's lark-cli is a Node launcher running the real binary as a
    child; killing the launcher alone leaves the binary holding its token cache. A
    timeout and a cancelled turn both stop the whole tree.
    """
    assert await _child_of_a_hung_command(tmp_path, stop) is False


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX shell script")
@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [{"timeout": 1.0}, {"cancel": True, "timeout": 30}],
                         ids=["timeout", "cancel"])
async def test_a_child_outliving_its_launcher_is_stopped_too(tmp_path, stop):
    """A launcher that exits after starting its child already has a return code,
    while the child still holds the pipes the command is waiting on. Its own process
    group is stopped regardless, so the child does not outlive the command.
    """
    assert await _child_of_a_hung_command(tmp_path, stop, launcher_waits=False) is False


def test_an_unmapped_envelope_still_reads_its_own_message():
    """A structured error whose ``type`` is unknown must not throw away its message.

    Measured on the live tenant: a deleted document answers
    ``{"type": "api", "subtype": "unknown", "message": "Resource is deleted"}``.
    The structured path mapped ``api`` to nothing, returned ``unknown``, and never
    reached the prose rules -- which recognise that sentence perfectly well. Because
    ``unknown`` reads as transient, admission retried the document **every poll**:
    1827 consecutive failures, one platform round trip each, and every other log line
    buried under them.

    Structure is still preferred over prose. This only stops an unmapped type from
    discarding an answer the same envelope already carries.
    """
    from jiuwenswarm.clouddoc.providers.feishu.cli import _classify

    envelope = json.dumps({
        "ok": False,
        "error": {"type": "api", "subtype": "unknown", "code": 1063005,
                  "message": "Resource is deleted"},
    })
    err = _classify(1, envelope)
    assert err.kind == "not_found", f"仍然是 {err.kind}"

    # A type the map does know is still answered from the type, not the prose.
    known = json.dumps({"error": {"type": "permission", "message": "nope"}})
    assert _classify(1, known).kind == "forbidden"

    # And a genuinely unrecognisable one stays unknown rather than being guessed at.
    opaque = json.dumps({"error": {"type": "api", "subtype": "unknown",
                                   "message": "something nobody predicted"}})
    assert _classify(1, opaque).kind == "unknown"


def test_a_platform_code_is_read_from_the_envelope_not_the_prose():
    """Code 20008's own text ("The user does not exist") is misleading, so the
    explanation keyed on that code must hold when the code arrives only as the
    envelope's ``code`` field, and a code's digits must not trip a prose rule.
    """
    envelope = json.dumps({"ok": False, "error": {
        "type": "api", "code": 20008, "message": "The user does not exist"}})
    err = _classify(1, envelope)
    assert err.kind == "not_found"
    assert "20008" in str(err) and "工作表名" in str(err)

    digits = json.dumps({"ok": False, "error": {
        "type": "api", "code": 1254290, "message": "something nobody predicted"}})
    assert _classify(1, digits).kind == "unknown"


def test_a_message_read_by_a_person_is_the_envelopes_text_not_its_json():
    """An unmapped type answered by a prose rule reaches a person as the envelope's
    message and hint, not the raw JSON it came in.
    """
    envelope = json.dumps({"ok": False, "error": {
        "type": "api", "message": "document not found", "hint": "check the link"}})
    err = _classify(1, envelope)
    assert err.kind == "not_found"
    assert str(err) == "document not found check the link"


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX shell script")
@pytest.mark.asyncio
async def test_an_ok_false_on_stdout_is_classified_as_on_stderr(tmp_path):
    """The CLI may exit 0 with ``ok: false`` on stdout. That envelope goes through the
    same classification as one on stderr, prose fallback included -- a deleted
    document is ``not_found`` on either stream, never a transient ``unknown``.
    """
    envelope = json.dumps({"ok": False, "error": {
        "type": "api", "subtype": "unknown", "message": "Resource is deleted"}})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_cli(bindir, f"cat <<'END'\n{envelope}\nEND\n")
    cli = LarkCli(env={"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin"})
    with pytest.raises(ProviderError) as caught:
        await cli.json(["whoami"])
    assert caught.value.kind == _classify(1, envelope).kind == "not_found"


@pytest.mark.asyncio
async def test_a_missing_binary_is_a_refusal_in_the_shared_vocabulary():
    """No executable means nothing was sent. The kind is one the callers branch on
    and that closes a receipt as aborted, not a word only this module knows.
    """
    from jiuwenswarm.clouddoc.providers.base import PRE_MUTATION_KINDS

    cli = LarkCli(env={"PATH": "/nonexistent"})
    with pytest.raises(ProviderError) as caught:
        await cli.run(["whoami"])
    assert caught.value.kind == "auth"
    assert caught.value.kind in PRE_MUTATION_KINDS


def test_an_injected_environment_without_path_finds_nothing_on_path():
    """The lookup uses the environment the subprocess will run with; one that has no
    PATH does not borrow this process's.
    """
    assert not LarkCli(env={"LARKSUITE_CLI_CONFIG_DIR": "/x"}).available


def _overlap_cli(directory):
    """A stand-in that records whether another command was running when it started."""
    marker = directory / "running"
    overlap = directory / "overlap"
    _fake_cli(directory, f'mkdir "{marker}" 2>/dev/null || touch "{overlap}"\n'
                         f'sleep 0.3\nrmdir "{marker}" 2>/dev/null\n'
                         'printf \'{"ok": true, "data": null}\'\n')
    return overlap


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX shell script")
@pytest.mark.asyncio
async def test_two_instances_on_one_profile_never_run_at_once(tmp_path):
    """The CLI's token cache is per store, not per LarkCli: two instances built on
    the same profile still take turns, while a different profile need not wait.
    """
    import asyncio

    bindir = tmp_path / "bin"
    bindir.mkdir()
    overlap = _overlap_cli(bindir)
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin",
           "LARKSUITE_CLI_CONFIG_DIR": str(tmp_path / "store")}
    await asyncio.gather(*(LarkCli(env=env, profile="p").json(["whoami"]) for _ in range(3)))
    assert not overlap.exists(), "two commands on one store ran concurrently"

    await asyncio.gather(LarkCli(env=env, profile="a").json(["whoami"]),
                         LarkCli(env=env, profile="b").json(["whoami"]))
    assert overlap.exists(), "different profiles were serialised as well"


def test_the_resolved_executable_leads_the_command():
    """The argv starts with the executable that was resolved, followed by the
    identity flags, without depending on the order of _base_args.
    """
    args = LarkCli(binary="lark-cli", profile="p")._base_args("/opt/x/lark-cli")
    assert args == ["/opt/x/lark-cli", "--as", "bot", "--profile", "p"]
