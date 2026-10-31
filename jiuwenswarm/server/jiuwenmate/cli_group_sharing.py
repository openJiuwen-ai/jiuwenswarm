"""Execute matched CLI reads privately and release frozen results through owner consent."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.server.jiuwenmate.cli_group_commands import CliReadPlan, UnsafeGroupRead
from jiuwenswarm.server.jiuwenmate.cli_group_snapshot import CliAttachment, CliReadSnapshot
from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import _lock, _part, _write_json, cli_preview_root

MAX_JSON_BYTES = 2 * 1024 * 1024


def _check_private_path(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise UnsafeGroupRead("private preview path escaped its root")
    for node in (path, *path.parents):
        if node.is_symlink() or getattr(node, "is_junction", lambda: False)():
            raise UnsafeGroupRead("linked private preview path is not allowed")
        if node == root:
            break


def _source_facts(items):
    return {item["group_id"]: {key: item[key] for key in ("app_id", "visible_from", "platform_chat_id")}
            for item in items}


def preview_directory(cli, snapshot) -> Path:
    request = hashlib.sha256(_part(snapshot.request_id).encode()).hexdigest()[:16]
    return (cli_preview_root(cli.root, snapshot.namespace_id, snapshot.agent_id)
            / request / snapshot.command_digest[:24])


def _messages(payload: dict) -> tuple[list[dict], bool]:
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise UnsafeGroupRead("CLI message read did not return a successful result")
    data = payload.get("data", payload)
    if isinstance(data, list):
        rows, more = data, False
    elif isinstance(data, dict):
        rows = data.get("messages", data.get("items"))
        if rows is None and isinstance(data.get("message"), dict):
            rows = [data["message"]]
        if rows is None and isinstance(data.get("message_id"), str):
            rows = [data]
        more = bool(data.get("has_more", False))
    else:
        raise UnsafeGroupRead("unrecognized CLI message response")
    if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, dict) for row in rows):
        raise UnsafeGroupRead("unrecognized or excessive CLI message results")
    return rows, more


def _message(row: dict):
    if row.get("deleted") or row.get("recalled") or row.get("status") in {"deleted", "recalled"}:
        raise UnsafeGroupRead("CLI source message was removed or recalled")
    message_id = row.get("message_id")
    if not isinstance(message_id, str) or re.fullmatch(r"om_[\w-]+", message_id) is None:
        raise UnsafeGroupRead("CLI result has no stable message ID")
    stamp = row.get("create_time", row.get("received_at"))
    try:
        numeric = float(stamp)
        received = datetime.fromtimestamp(numeric / 1000 if numeric > 10**11 else numeric, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            received = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if received.tzinfo is None:
                raise ValueError("naive date")
        except (ValueError, TypeError):
            raise UnsafeGroupRead("CLI message time could not be verified") from None
    sender = row.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {"id": str(sender)}
    sender_id = sender.get("id", row.get("sender_id", "unknown"))
    if isinstance(sender_id, dict):
        sender_id = sender_id.get("open_id", sender_id.get("user_id", "unknown"))
    body = row.get("body")
    content = row.get("content", row.get("text", body.get("content", "") if isinstance(body, dict) else ""))
    if isinstance(content, str):
        try:
            body = json.loads(content)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("text"), str):
            content = body["text"]
    else:
        content = json.dumps(content, ensure_ascii=False)
    if len(str(content)) > 32000:
        raise UnsafeGroupRead("CLI message preview is too large; narrow the request")
    attachments = tuple(dict.fromkeys(re.findall(r"(?:file|img)_[A-Za-z0-9_-]+", str(content))))
    return core.GroupMessage(
        message_id=message_id, sender_id=str(sender_id or "unknown"),
        sender_name=str(sender.get("name", row.get("sender_name", sender_id)) or "群成员")[:512],
        sender_kind="agent" if sender.get("sender_type") in {"app", "bot"} else "human",
        received_at=received, text=str(content), attachment_refs=attachments,
    )


async def verified_cli_rows(cli, namespace: str, agent: str, rows: list[dict], identity: str) -> list[dict]:
    """CLI shortcuts format dates to minutes; fetch exact raw facts before authorization."""
    verified = []
    for row in rows:
        try:
            _message(row)
        except UnsafeGroupRead as exc:
            if str(exc) != "CLI message time could not be verified":
                raise
            message_id = row.get("message_id", "")
            if not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
                raise UnsafeGroupRead("raw message identity is unavailable") from None
            payload = await cli.run_group_read(namespace, agent, [
                "api", "GET", f"/open-apis/im/v1/messages/{message_id}", "--as", identity, "--format", "json",
            ])
            if payload.get("code", 0) != 0:
                raise UnsafeGroupRead("raw source message is not readable")
            exact, _ = _messages(payload)
            if (len(exact) != 1 or exact[0].get("message_id") != message_id
                    or row.get("chat_id") and exact[0].get("chat_id") != row["chat_id"]):
                raise UnsafeGroupRead("raw source message identity changed")
            row = exact[0]
            _message(row)
        verified.append(row)
    return verified


async def verify_cli_snapshot(cli, snapshot: CliReadSnapshot) -> bool:
    """Recheck exact message bodies and cached file bytes without returning new data to the model."""
    if cli is None:
        return False
    try:
        status = cli.status(snapshot.namespace_id, snapshot.agent_id)
        source_ids = {hit.group_id for hit in snapshot.hits}
        if status.get("status") != "ready" or any(
            snapshot.source_authorizations.get(group_id, {}).get("app_id") != status.get("app_id")
            for group_id in source_ids
        ):
            return False
        expected = {hit.message.message_id: hit.message for hit in snapshot.hits}
        groups = {hit.message.message_id: hit.group_id for hit in snapshot.hits}
        seen = {}
        ids = tuple(expected)
        for start in range(0, len(ids), 50):
            payload = await cli.run_group_read(snapshot.namespace_id, snapshot.agent_id, [
                "im", "+messages-mget", "--message-ids", ",".join(ids[start:start + 50]),
                "--as", str(snapshot.plan.get("identity", "bot")), "--format", "json", "--no-reactions",
            ])
            rows, _ = _messages(payload)
            rows = await verified_cli_rows(cli, snapshot.namespace_id, snapshot.agent_id, rows,
                                           str(snapshot.plan.get("identity", "bot")))
            for row in rows:
                message = _message(row)
                if message.message_id in expected:
                    source = snapshot.source_authorizations.get(groups[message.message_id], {})
                    if row.get("chat_id") != source.get("platform_chat_id"):
                        return False
                    seen[message.message_id] = message
        if seen != expected:
            return False
        directory = preview_directory(cli, snapshot)
        _check_private_path(cli.root, directory)
        for file in snapshot.artifacts:
            path = directory / file.cache_name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != file.file_size:
                return False
            if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != file.file_digest:
                return False
        return True
    except Exception:
        return False


class CliGroupSharing:
    """Invocation-bound adapter; all candidates remain in the private host until consent is durable."""

    def __init__(self, *, sharing, group_port, cli, session_workspace: Path):
        self.sharing, self.port, self.cli = sharing, group_port, cli
        self.workspace = Path(session_workspace)

    async def _sources(self, execution):
        query = core.GroupMessageQuery(query="CLI source authorization", content_types=("messages",), limit=500)
        sources = await self.port.sharing_sources(execution, query)
        sources = tuple(item for item in sources if item.get("platform") == "feishu")
        if not sources or any(not item.get("platform_chat_id") or not item.get("visible_from") for item in sources):
            raise UnsafeGroupRead("CLI source group authorization is unavailable")
        status = self.cli.status(execution.namespace_id, execution.agent_id)
        if status.get("status") != "ready":
            raise UnsafeGroupRead("Agent Feishu CLI profile is not ready")
        sources = tuple(item for item in sources if item.get("app_id") == status.get("app_id"))
        if not sources:
            raise UnsafeGroupRead("CLI profile does not match this Agent's group authorizations")
        return sources

    async def _preview(self, execution, plan, digest, sources) -> CliReadSnapshot:
        by_chat = {item["platform_chat_id"]: item for item in sources}
        if not set(plan.chat_ids).issubset(by_chat):
            raise UnsafeGroupRead("CLI source group is not authorized for this Agent")
        payload = await self.cli.run_group_read(execution.namespace_id, execution.agent_id,
                                               plan.argv(allowed_chats=tuple(by_chat)))
        rows, more = _messages(payload)
        rows = await verified_cli_rows(self.cli, execution.namespace_id, execution.agent_id, rows, plan.identity)
        hits = []
        for row in rows:
            chat_id = row.get("chat_id") or (plan.chat_ids[0] if len(plan.chat_ids) == 1 else None)
            if plan.chat_ids and chat_id not in plan.chat_ids:
                raise UnsafeGroupRead("CLI response exceeded the requested source group filter")
            source = by_chat.get(chat_id)
            if source is None:
                raise UnsafeGroupRead("CLI returned an unverifiable source group")
            message = _message(row)
            if plan.message_ids and message.message_id not in plan.message_ids:
                continue
            if message.received_at < datetime.fromisoformat(source["visible_from"].replace("Z", "+00:00")):
                continue
            hits.append(core.GroupMessageSearchHit(group_id=source["group_id"], group_name=source["name"],
                message_ref=f"cli:{source['group_id']}:{message.message_id}", message=message))
        snapshot = CliReadSnapshot(
            **core.InvocationRef.from_execution(execution).model_dump(), command_digest=digest,
            plan=json.loads(json.dumps(asdict(plan))), content_types=("files",) if plan.operation == "download" else ("messages",),
            hits=tuple(hits), has_more=more,
            source_authorizations=_source_facts([item for item in sources
                                                if item["group_id"] in {hit.group_id for hit in hits}]),
        )
        if plan.operation == "download":
            if len(hits) != 1 or plan.file_key not in hits[0].message.attachment_refs:
                raise UnsafeGroupRead("attachment does not match an authorized source message")
            directory = preview_directory(self.cli, snapshot)
            _check_private_path(self.cli.root, directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache_name = hashlib.sha256(plan.file_key.encode()).hexdigest()[:24] + ".bin"
            target = directory / cache_name
            if target.is_symlink():
                raise UnsafeGroupRead("invalid private attachment cache")
            result = await self.cli.run_group_read(execution.namespace_id, execution.agent_id, [
                "im", "+messages-resources-download", "--message-id", plan.message_ids[0],
                "--file-key", plan.file_key, "--type", plan.resource_type, "--output", str(target),
                "--as", plan.identity, "--format", "json",
            ])
            if not target.is_file() or target.stat().st_size > 20 * 1024 * 1024:
                raise UnsafeGroupRead("attachment exceeds the private approval limit")
            name = (result.get("data") or {}).get("file_name") or "attachment.bin"
            name = re.sub(r'[/\\\x00:<>"|?*]', "_", str(name))[:180].strip(". ") or "attachment.bin"
            file = CliAttachment(
                group_id=hits[0].group_id, group_name=hits[0].group_name,
                artifact_id="cli-file-" + hashlib.sha256((digest + plan.file_key).encode()).hexdigest(),
                title=name, file_name=name, file_size=target.stat().st_size,
                file_digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(), cache_name=cache_name,
                message_id=plan.message_ids[0], file_key=plan.file_key, resource_type=plan.resource_type,
                created_at=hits[0].message.received_at,
            )
            snapshot = snapshot.model_copy(update={"artifacts": (file,)})
        return snapshot

    async def execute(self, execution, command: str, plan: CliReadPlan) -> dict:
        marker = CliReadSnapshot(**core.InvocationRef.from_execution(execution).model_dump(),
                                 command_digest=hashlib.sha256(command.encode("utf-8")).hexdigest(), plan={},
                                 content_types=("messages",))
        directory = preview_directory(self.cli, marker)
        _check_private_path(self.cli.root, directory)
        async with _lock(directory / ".lock"):
            return await self._execute(execution, command, plan)

    async def _execute(self, execution, command: str, plan: CliReadPlan) -> dict:
        from jiuwenswarm.server.jiuwenmate.group_sharing_service import _digest

        sources = await self._sources(execution)
        by_group = {item["group_id"]: item for item in sources}
        repo = self.sharing.repository
        denied_groups = await asyncio.to_thread(repo.denied_groups, execution.namespace_id, execution.agent_id,
                                               execution.request_id)
        requested = {item["group_id"] for item in sources if item["platform_chat_id"] in plan.chat_ids}
        if requested and requested.issubset(denied_groups):
            return {"ok": False, "error": "本次请求对此来源群的分享已被拒绝或过期，不再重复申请。"}
        digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        marker = CliReadSnapshot(**core.InvocationRef.from_execution(execution).model_dump(),
                                 command_digest=digest, plan=json.loads(json.dumps(asdict(plan))), content_types=("messages",))
        directory = preview_directory(self.cli, marker)
        cache = directory / "preview.json"
        if cache.exists():
            if cache.is_symlink() or cache.stat().st_size > MAX_JSON_BYTES:
                raise UnsafeGroupRead("invalid private CLI preview")
            preview = CliReadSnapshot.model_validate_json(cache.read_text(encoding="utf-8"))
            if any(getattr(preview, key) != getattr(execution, key) for key in (
                "namespace_id", "agent_id", "session_id", "request_id", "source_ref",
            )) or preview.command_digest != digest or preview.plan != json.loads(json.dumps(asdict(plan))):
                raise UnsafeGroupRead("CLI preview belongs to another request")
        else:
            preview = await self._preview(execution, plan, digest, sources)
            encoded = preview.model_dump(mode="json")
            if len(json.dumps(encoded).encode("utf-8")) > MAX_JSON_BYTES:
                raise UnsafeGroupRead("CLI preview is too large; narrow the request")
            _write_json(cache, encoded)
        latest = await self._sources(execution)
        actual = {hit.group_id for hit in preview.hits}
        before, after = _source_facts(sources), _source_facts(latest)
        if (set(preview.source_authorizations) != actual
                or any(preview.source_authorizations[group_id] != before.get(group_id)
                       or preview.source_authorizations[group_id] != after.get(group_id) for group_id in actual)):
            raise UnsafeGroupRead("CLI source authorization changed")
        ids = tuple(sorted({hit.group_id for hit in preview.hits}))
        query = core.GroupMessageQuery(query=f"CLI {plan.operation} [{digest}]", group_ids=ids,
                                       content_types=preview.content_types, limit=500)
        scope, current = await asyncio.to_thread(self.sharing.scope, execution, query)
        batch = _digest(query)
        rows = await asyncio.to_thread(repo.for_batch, execution.namespace_id, execution.agent_id,
                                       execution.request_id, batch)
        if not rows:
            snapshots = []
            for group_id in ids:
                if group_id == current or group_id in denied_groups:
                    continue
                source = by_group[group_id]
                frozen = preview.model_copy(update={
                    "hits": tuple(hit for hit in preview.hits if hit.group_id == group_id),
                    "artifacts": tuple(file for file in preview.artifacts if file.group_id == group_id),
                })
                per_scope = {**scope, "origin": "cli", "cli_digest": digest, "search_digest": batch,
                             "source_authorizations": {group_id: {key: source[key] for key in ("app_id", "visible_from")}}}
                snapshots.append((per_scope, _digest(query, group_id), frozen.model_dump(mode="json"), [source]))
            if snapshots:
                rows = await asyncio.to_thread(repo.create_or_get_batch, snapshots, now=self.sharing._clock())
        approved = [preview.model_copy(update={
            "hits": tuple(hit for hit in preview.hits if hit.group_id == current),
            "artifacts": tuple(file for file in preview.artifacts if file.group_id == current),
        })]
        pending, denied = [], bool(denied_groups.intersection(hit.group_id for hit in preview.hits))
        for row in rows:
            group_id = row["source_groups"][0]["group_id"]
            saved = row["scope"]
            expected = {group_id: {key: by_group[group_id][key] for key in ("app_id", "visible_from")}}
            if any(saved.get(key) != scope.get(key) for key in (
                "namespace_id", "config_id", "agent_id", "request_id", "principal_ref", "target", "query",
            )) or saved.get("source_authorizations") != expected or saved.get("cli_digest") != digest:
                raise UnsafeGroupRead("CLI sharing scope changed")
            if row["status"] == "pending":
                await asyncio.to_thread(repo.authorize_from_grants, row["approval_id"], now=self.sharing._clock())
                row = await asyncio.to_thread(repo.get, row["approval_id"])
            if row["status"] == "pending":
                pending.append(row)
            elif row["status"] in {"approved_once", "approved_permanent"}:
                frozen = CliReadSnapshot.model_validate(row["result"])
                if row["status"] == "approved_permanent" and not await asyncio.to_thread(
                    repo.has_grant, saved, group_id, list(frozen.content_types),
                ):
                    raise UnsafeGroupRead("CLI sharing authorization was revoked")
                approved.append(frozen)
            else:
                denied = True
        if pending:
            await asyncio.to_thread(self.sharing.notify_owner, pending[0]["scope"])
            raise core.GroupContentApprovalRequired(approval_id=pending[0]["approval_id"])
        messages, files = [], []
        for frozen in approved:
            if not frozen.hits:
                continue
            if not await verify_cli_snapshot(self.cli, frozen):
                raise UnsafeGroupRead("approved CLI source changed or is no longer readable")
            if "messages" in frozen.content_types:
                messages.extend(hit.message.model_dump(mode="json") | {"source_group": hit.group_name}
                                for hit in frozen.hits)
            for file in frozen.artifacts:
                imports = self.workspace / "imports"
                _check_private_path(self.workspace, imports)
                imports.mkdir(parents=True, exist_ok=True)
                name = file.artifact_id[:25] + "-" + file.file_name
                target = imports / name
                if target.is_symlink() or target.exists() and not target.is_file():
                    raise UnsafeGroupRead("invalid imported filename")
                shutil.copyfile(preview_directory(self.cli, frozen) / file.cache_name, target)
                files.append({"file_path": f"sessions/{execution.session_id}/imports/{name}",
                              "file_name": file.file_name, "file_size": file.file_size})
        if denied and not messages and not files:
            return {"ok": False, "error": "所有者拒绝或审批已过期，未返回跨群内容。"}
        return {"ok": True, "data": {"messages": messages, "files": files, "has_more": preview.has_more},
                "incomplete": denied, "notice": "已返回宿主冻结并获准的 CLI 读取结果，未重跑原命令链。"}
