"""CLI command matching and host-private original-Bash owner approval."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing, NOW


@pytest.mark.parametrize("command,operation", [
    ('lark-cli im +chat-messages-list --chat-id oc_source', "list"),
    ('lark-cli im +messages-search --query launch --chat-id oc_a,oc_b', "search"),
    ('lark-cli im +messages-mget --message-ids om_a,om_b', "get"),
    ('lark-cli im +messages-resources-download --message-id om_a --file-key file_a --type file', "download"),
    ('lark-cli im +threads-messages-list --thread omt_a', "thread"),
    ('cmd /c "lark-cli im +chat-messages-list --chat-id oc_source"', "list"),
    ('powershell -Command "& \'C:\\private cli\\lark-cli.ps1\' im +messages-mget --message-ids om_a"', "get"),
    ('cd /d "C:\\office" && set LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1&& lark-cli im +chat-messages-list --chat-id oc_source', "list"),
    ('lark-cli --as bot im +messages-mget --message-ids om_a', "get"),
    ('lark-cli im messages get --path \'{"message_id":"om_a"}\'', "get"),
    ('lark-cli api GET /open-apis/im/v1/messages --params \'{"container_id_type":"chat","container_id":"oc_source"}\'', "list"),
    ('lark-cli api GET /open-apis/im/v1/messages/om_a', "get"),
    ('lark-cli api GET /open-apis/im/v1/messages/om_a/resources/file_a --params \'{"type":"file"}\'', "download"),
    ('lark-cli api POST /open-apis/search/v2/message --data \'{"query":"launch","chat_ids":["oc_source"]}\'', "search"),
    ('lark-cli i^m +messages-mget --message-ids om_a', "get"),
    ('lark-cli i`m +messages-mget --message-ids om_a', "get"),
])
def test_parse_supported_message_reads(command, operation):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    plan = parse_group_read(command)
    assert plan is not None and plan.operation == operation
    assert plan.identity in {"bot", "user"}


@pytest.mark.parametrize("command", [
    'python -c "print(123)"', 'git status --short', 'ls -la output',
    'lark-cli auth status --json --verify', 'lark-cli calendar +agenda --as user',
    'lark-cli docs +create --title report', 'lark-cli drive +download --file-token doc_token',
    'lark-cli im +chat-search --query project', 'lark-cli im +chat-messages-list --help',
    'lark-cli --help', 'lark-cli --version',
    'lark-cli api GET /open-apis/calendar/v4/calendars',
    'lark-cli im +messages-send --chat-id oc_a --text hello',
    'lark-cli im +messages-reply --message-id om_a --text hello',
])
def test_other_commands_are_not_redirected(command):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    assert parse_group_read(command) is None


@pytest.mark.parametrize("command", [
    'echo changed > marker && lark-cli im +chat-messages-list --chat-id oc_source',
    'lark-cli im +chat-messages-list --chat-id oc_source > messages.json',
    'lark-cli im +messages-mget --message-ids om_a | jq .',
    'lark-cli im +messages-mget --message-ids om_a && lark-cli im +messages-mget --message-ids om_b',
    'lark-cli im +chat-messages-list --chat-id "$CHAT"',
    'lark-cli im +messages-resources-download --file-key file_a',
    'lark-cli im +chat-messages-list --chat-id oc_a --download-resources',
    'lark-cli im +messages-mget --message-ids om_a --profile other',
    'lark-cli im +messages-mget --message-ids om_a --jq .data.messages',
    'lark-cli im +new-message-read --chat-id oc_a',
    'lark-cli im pins list --chat-id oc_a',
    'lark-cli im files get --file-key file_a',
    'lark-cli api GET "$URL" --params @params.json',
    'lark-cli api GET /open-apis/im/v1/messages --params @params.json',
    'lark-cli api POST /open-apis/im/v1/messages --data @body.json',
    'curl -H "Authorization: Bearer $TOKEN" https://open.feishu.cn/open-apis/im/v1/messages',
    'Invoke-RestMethod -Uri https://open.feishu.cn/open-apis/im/v1/messages/om_a',
    'python -c "import requests; requests.get(\'https://open.feishu.cn/open-apis/im/v1/messages\')"',
    'python -c "client.im.v1.message.get(request)"',
    'python -c "subprocess.run([\'lark-cli\',\'im\',\'+messages-mget\'])"',
    'npx @larksuite/cli im +messages-mget --message-ids om_a',
    'node C:\\npm\\node_modules\\@larksuite\\cli\\scripts\\run.js im +messages-mget --message-ids om_a',
])
def test_opaque_or_unsafe_message_reads_never_fall_through(command):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    with pytest.raises(UnsafeGroupRead):
        parse_group_read(command)


def test_parse_rejects_unbounded_pages_and_keeps_exact_resource_identity():
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    plan = parse_group_read('lark-cli im +messages-mget --message-ids om_a,om_b --as user')
    assert plan.message_ids == ("om_a", "om_b") and plan.identity == "user"
    assert parse_group_read('lark-cli im +chat-messages-list --chat-id oc_a').chat_ids == ("oc_a",)
    with pytest.raises(UnsafeGroupRead):
        parse_group_read('lark-cli im +chat-messages-list --chat-id oc_a --page-all --page-limit 1000')


@pytest.fixture
def cli_host(sharing, tmp_path):
    from jiuwenswarm.server.jiuwenmate.cli_group_sharing import CliGroupSharing

    raw = {"message_id": "om_source", "chat_id": "oc_source", "create_time": str(int(NOW.timestamp() * 1000)),
           "sender": {"id": "user-1", "name": "Alice", "sender_type": "user"},
           "msg_type": "text", "content": "PRIVATE-CLI-BODY"}
    calls = []

    class Runtime:
        root = tmp_path / "host"

        def status(self, namespace, agent):
            return {"status": "ready", "app_id": "app-1"}

        async def run_group_read(self, namespace, agent, argv):
            calls.append(list(argv))
            if "+messages-resources-download" in argv:
                Path(argv[argv.index("--output") + 1]).write_bytes(b"private attachment")
                return {"ok": True, "data": {"file_name": "report.txt"}}
            return {"ok": True, "data": {"messages": [raw], "has_more": False}}

    cli = Runtime()
    sharing.gate._cli = cli
    sharing.delegate.sharing_sources.return_value = ({
        "group_id": "source-group", "name": "Source group", "app_id": "app-1", "platform": "feishu",
        "platform_chat_id": "oc_source", "visible_from": (NOW - timedelta(days=1)).isoformat(),
    },)
    workspace = tmp_path / "office" / "sessions" / "session-1"
    workspace.mkdir(parents=True)
    handler = CliGroupSharing(sharing=sharing.gate, group_port=sharing.delegate, cli=cli, session_workspace=workspace)
    return SimpleNamespace(**vars(sharing), raw=raw, calls=calls, cli=cli, handler=handler, workspace=workspace)


async def decide_cli(host, decision):
    row, = host.repo.pending_for_request("local", "request-1")
    host.repo.bind_native("local", "request-1", "native-1")
    host.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1", owner_principal="local",
                    decision=decision, now=NOW)
    return row


@pytest.mark.asyncio
async def test_cli_preview_pauses_and_returns_frozen_result_without_replaying_list(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    plan = parse_group_read(command)
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, plan)
    row = await decide_cli(cli_host, "once")
    preview = cli_host.repo.list_for_owner("local", "config-1")["approvals"][0]
    assert "PRIVATE-CLI-BODY" in preview["messages"][0]["text"]
    assert row["source_groups"] == [{"group_id": "source-group", "name": "Source group"}]
    resumed = replace(cli_host.execution, execution_id="execution-2")
    result = await cli_host.handler.execute(resumed, command, plan)
    assert "PRIVATE-CLI-BODY" in json.dumps(result, ensure_ascii=False)
    assert sum("+chat-messages-list" in args for args in cli_host.calls) == 1


@pytest.mark.asyncio
async def test_current_group_does_not_create_cross_group_approval(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read
    from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef

    cli_host.sessions.get_session = lambda *args: SimpleNamespace(conversation=ConversationRef.group("source-group"))
    command = 'lark-cli im +messages-mget --message-ids om_source'
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert result["ok"]
    assert not cli_host.repo.pending_for_request("local", "request-1")


@pytest.mark.asyncio
async def test_cli_rejection_does_not_release_candidates_or_requery(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "reject")
    count = len(cli_host.calls)
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not result["ok"] and "PRIVATE-CLI-BODY" not in json.dumps(result)
    assert len(cli_host.calls) == count
    changed = command + " --page-size 10"
    result = await cli_host.handler.execute(cli_host.execution, changed, parse_group_read(changed))
    assert not result["ok"] and len(cli_host.calls) == count
    assert not cli_host.repo.pending_for_request("local", "request-1")


@pytest.mark.asyncio
async def test_cli_attachment_is_private_until_file_specific_approval(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    cli_host.raw.update(msg_type="file", content=json.dumps({"file_key": "file_a", "file_name": "report.txt"}))
    command = 'lark-cli im +messages-resources-download --message-id om_source --file-key file_a --type file'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not list(cli_host.workspace.rglob("*.txt"))
    preview = cli_host.repo.list_for_owner("local", "config-1")["approvals"][0]
    assert preview["content_types"] == ["files"] and preview["artifacts"][0]["file_name"] == "report.txt"
    await decide_cli(cli_host, "once")
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    relative = result["data"]["files"][0]["file_path"]
    assert relative.startswith("sessions/session-1/imports/")
    assert (cli_host.workspace / "imports" / Path(relative).name).read_bytes() == b"private attachment"


@pytest.mark.asyncio
async def test_cli_unknown_source_is_never_returned_or_approved(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_unknown'
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not cli_host.calls and not cli_host.repo.pending_for_request("local", "request-1")


@pytest.mark.asyncio
async def test_cli_response_cannot_expand_an_explicit_source_filter(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    source, = cli_host.delegate.sharing_sources.return_value
    cli_host.delegate.sharing_sources.return_value = (source, {**source, "group_id": "different-group",
                                                              "platform_chat_id": "oc_different"})
    cli_host.raw["chat_id"] = "oc_different"
    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not cli_host.repo.pending_for_request("local", "request-1")


@pytest.mark.parametrize("command", [
    'lark-cli api GET /open-apis/im/v1/messages?container_id=oc_source&unknown_filter=secret',
    'lark-cli api POST /open-apis/search/v2/message --data \'{"query":"launch","unknown_filter":"secret"}\'',
    'lark-cli im +messages-search --query launch --sender-type user',
])
def test_filters_must_never_be_silently_dropped(command):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    with pytest.raises(UnsafeGroupRead):
        parse_group_read(command)


def test_raw_message_url_keeps_bounded_query_parameters():
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    plan = parse_group_read('lark-cli api GET "https://open.feishu.cn/open-apis/im/v1/messages?container_id=oc_source&container_id_type=chat&page_size=10"')
    assert plan.chat_ids == ("oc_source",) and plan.page_size == 10


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_encoded_inline_read_is_detected_before_execution(encoding):
    import base64
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    encoded = base64.b64encode(b"lark-cli im +messages-mget --message-ids om_a".decode().encode(encoding)).decode()
    with pytest.raises(UnsafeGroupRead):
        parse_group_read('powershell -EncodedCommand ' + encoded)


def test_literal_escaped_api_path_is_detected_before_execution():
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    with pytest.raises(UnsafeGroupRead):
        parse_group_read(r'''python -c "requests.get('https://open.feishu.cn/open-apis/\x69\x6d/v1/messages')"''')


@pytest.mark.asyncio
async def test_cli_profile_must_match_current_group_authorization(cli_host):
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    cli_host.cli.status = lambda *args: {"status": "ready", "app_id": "different-app"}
    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not cli_host.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["body", "chat", "access", "recalled"])
async def test_cli_approval_never_releases_changed_source(cli_host, change):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "once")
    if change == "access":
        source, = cli_host.delegate.sharing_sources.return_value
        cli_host.delegate.sharing_sources.return_value = ({**source, "visible_from": NOW.isoformat()},)
    elif change == "recalled":
        cli_host.raw["deleted"] = True
    else:
        cli_host.raw["content" if change == "body" else "chat_id"] = "changed"
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))


@pytest.mark.asyncio
async def test_cli_duplicate_calls_share_one_private_preview_and_card(cli_host):
    import asyncio
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    replies = await asyncio.gather(*(cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
                                     for _ in range(2)), return_exceptions=True)
    assert all(isinstance(reply, core.GroupContentApprovalRequired) for reply in replies)
    assert len(cli_host.repo.pending_for_request("local", "request-1")) == 1
    assert sum("+chat-messages-list" in args for args in cli_host.calls) == 1


@pytest.mark.asyncio
async def test_permanent_message_consent_does_not_authorize_attachment_download(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "permanent")
    assert (await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command)))["ok"]
    cli_host.raw.update(msg_type="file", content=json.dumps({"file_key": "file_a"}))
    command = 'lark-cli im +messages-resources-download --message-id om_source --file-key file_a --type file'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    pending, = cli_host.repo.pending_for_request("local", "request-1")
    view = next(item for item in cli_host.repo.list_for_owner("local", "config-1")["approvals"]
                if item["approval_id"] == pending["approval_id"])
    assert view["content_types"] == ["files"]
    assert not (cli_host.workspace / "imports").exists()


@pytest.mark.asyncio
async def test_permanent_cli_grant_reused_and_revocation_blocks_frozen_release(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "permanent")
    second = command + ' --page-size 10'
    assert (await cli_host.handler.execute(cli_host.execution, second, parse_group_read(second)))["ok"]
    grant, = cli_host.repo.list_for_owner("local", "config-1")["grants"]
    cli_host.repo.revoke(grant["grant_id"], namespace_id="local", config_id="config-1",
                        owner_principal="local", now=NOW)
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, second, parse_group_read(second))


@pytest.mark.asyncio
async def test_cli_file_tampering_is_detected_after_approval(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import UnsafeGroupRead, parse_group_read

    cli_host.raw.update(msg_type="file", content=json.dumps({"file_key": "file_a"}))
    command = 'lark-cli im +messages-resources-download --message-id om_source --file-key file_a --type file'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    cached, = cli_host.cli.root.rglob("*.bin")
    cached.write_bytes(b"tampered")
    await decide_cli(cli_host, "once")
    with pytest.raises(UnsafeGroupRead):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert not (cli_host.workspace / "imports").exists()


@pytest.mark.asyncio
async def test_cli_multi_group_results_have_independent_decisions(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    source, = cli_host.delegate.sharing_sources.return_value
    cli_host.delegate.sharing_sources.return_value = (source, {**source, "group_id": "source-two", "name": "Second group",
                                                             "platform_chat_id": "oc_two"})
    second = {**cli_host.raw, "chat_id": "oc_two", "message_id": "om_two", "content": "SECOND-PRIVATE-BODY"}
    async def read(*args):
        return {"ok": True, "data": {"messages": [cli_host.raw, second]}}
    cli_host.cli.run_group_read = read
    command = 'lark-cli im +messages-search --query private'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    rows = cli_host.repo.pending_for_request("local", "request-1")
    assert len(rows) == 2 and all(len(row["source_groups"]) == 1 for row in rows)
    cli_host.repo.bind_native("local", "request-1", "native-1")
    for row in rows:
        decision = "once" if row["source_groups"][0]["group_id"] == "source-group" else "reject"
        cli_host.repo.decide(row["approval_id"], namespace_id="local", config_id="config-1",
                            owner_principal="local", decision=decision, now=NOW)
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert result["ok"] and result["incomplete"]
    assert "PRIVATE-CLI-BODY" in json.dumps(result) and "SECOND-PRIVATE-BODY" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unrelated_group_join_does_not_invalidate_cli_preview(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read

    command = 'lark-cli im +chat-messages-list --chat-id oc_source'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "once")
    source, = cli_host.delegate.sharing_sources.return_value
    cli_host.delegate.sharing_sources.return_value = (source, {**source, "group_id": "unrelated",
                                                              "platform_chat_id": "oc_unrelated"})
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert result["ok"] and "PRIVATE-CLI-BODY" in json.dumps(result)


@pytest.mark.asyncio
async def test_cli_retains_current_group_hits_after_all_external_sources_are_rejected(cli_host):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read
    from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef

    cli_host.sessions.get_session = lambda *args: SimpleNamespace(conversation=ConversationRef.group("source-group"))
    source, = cli_host.delegate.sharing_sources.return_value
    cli_host.delegate.sharing_sources.return_value = (source, {**source, "group_id": "outside",
                                                              "platform_chat_id": "oc_outside"})
    outside = {**cli_host.raw, "chat_id": "oc_outside", "message_id": "om_outside", "content": "REJECTED-EXTERNAL"}
    async def read(*args):
        return {"ok": True, "data": {"messages": [cli_host.raw, outside]}}
    cli_host.cli.run_group_read = read
    command = 'lark-cli im +messages-search --query private'
    with pytest.raises(core.GroupContentApprovalRequired):
        await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    await decide_cli(cli_host, "reject")
    result = await cli_host.handler.execute(cli_host.execution, command, parse_group_read(command))
    assert result["ok"] and result["incomplete"]
    assert "PRIVATE-CLI-BODY" in json.dumps(result) and "REJECTED-EXTERNAL" not in json.dumps(result)
