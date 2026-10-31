"""Guard direct access to files belonging to another group's Session."""

import asyncio
import hashlib
import re

from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.server.jiuwenmate.cli_group_commands import parse_group_read, UnsafeGroupRead


def _normal(value: str) -> str:
    return re.sub(r"[/\\]+", "/", value).casefold()


def _blocked(reason: str) -> ToolOutput:
    return ToolOutput(success=False, data={"status": "forbidden", "content": reason}, error=reason)


class SessionArtifactSharing:
    """An invocation preflight, independent of default/automatic/full-access tool policies."""

    def __init__(self, sharing, group_port):
        self.sharing, self.port = sharing, group_port

    async def prepare(self, execution, tool_name: str, arguments: dict):
        if tool_name not in {"bash", "read_file", "list_files", "list_dir", "share_file"}:
            return None
        command = arguments.get("command", "") if tool_name == "bash" else ""
        if tool_name == "bash":
            try:
                if parse_group_read(command) is not None:
                    return None
            except UnsafeGroupRead:
                return None  # The Bash adapter rejects or handles the matched CLI operation.
        requested = arguments.get("file_path", arguments.get("path", ""))
        text = _normal(str(command or requested))
        if not text:
            return None
        if any(marker in text for marker in ("agents.sqlite3", "group_messages/", "cli_read_previews/")):
            return _blocked("群消息库和审批预览不能通过磁盘命令读取；请使用跨群搜索或明确的 CLI 消息读取。")
        loader = getattr(self.sharing._sessions, "session_artifact_candidates", None)
        if loader is None:
            return None
        rows = await asyncio.to_thread(loader, execution.namespace_id, execution.agent_id)
        current_prefix = f"sessions/{execution.session_id}/".casefold()
        selected = []
        for row in rows:
            path = _normal(str(row.get("file_path") or ""))
            filename = _normal(str(row.get("file_name") or ""))
            if path and path in text:
                selected.append(row)
            elif (filename and filename in text and path.startswith(current_prefix)
                  and not re.search(r"sessions/(?!" + re.escape(execution.session_id.casefold()) + r"/)", text)):
                selected.append(row)
        foreign_path = re.search(r"sessions/([^/\s\"']+)", text)
        if not selected:
            if foreign_path and foreign_path[1] != execution.session_id.casefold():
                return _blocked("其他 Session 的草稿或未登记文件不能直接读取；请检索已登记的跨群最终产物。")
            return None
        # A final artifact already registered in this group is readable here.
        # Identical files in other groups do not change its current-group visibility.
        origins = {candidate["artifact_id"]: candidate for candidate in selected}
        scope_query = core.GroupMessageQuery(query="file provenance", content_types=("files",))
        _, current = await asyncio.to_thread(self.sharing.scope, execution, scope_query)
        foreign = [row for row in origins.values() if row["source_group_id"] != current]
        if not foreign:
            return None
        denied = await asyncio.to_thread(self.sharing.repository.denied_groups, execution.namespace_id,
                                         execution.agent_id, execution.request_id)
        if any(row["source_group_id"] in denied for row in foreign):
            return _blocked("所有者拒绝或审批已过期，本次请求不再读取或分享该来源文件。")
        query = core.GroupMessageQuery(
            query="file-provenance:" + hashlib.sha256(repr(sorted(
                (row["artifact_id"], row["file_ref"], row["file_size"]) for row in foreign
            )).encode()).hexdigest(),
            keywords=tuple(sorted({row["file_name"] for row in foreign})),
            group_ids=tuple(sorted({row["source_group_id"] for row in foreign})),
            content_types=("files",), limit=500,
        )
        sources = await self.port.sharing_sources(execution, query)
        artifacts, more = await self.sharing.search_artifacts(execution, query, sources)
        expected = {row["artifact_id"] for row in foreign}
        artifacts = tuple(artifact for artifact in artifacts if artifact.artifact_id in expected)
        if {artifact.artifact_id for artifact in artifacts} != expected:
            return _blocked("文件来源或当前群授权不可用，未放行缓存内容。")
        result = core.GroupMessageSearchResult(**core.InvocationRef.from_execution(execution).model_dump(),
            status="found", artifacts=artifacts, hits=(), query_complete=not more)
        released = await self.sharing.authorize(execution, query, result, sources)
        requested_facts = {(item.artifact_id, item.file_digest, item.file_size) for item in artifacts}
        released_facts = {(item.artifact_id, item.file_digest, item.file_size) for item in released.artifacts}
        if released.status not in {"found", "incomplete"} or released_facts != requested_facts:
            return _blocked("跨群文件分享未获准，未返回文件内容。")
        # A local copy must still match the frozen blob before allowing its existing
        # read_file/share_file call; Bash is never replayed after a cross-group preflight.
        for row in selected:
            path = await asyncio.to_thread(self.sharing._files.resolve, execution.namespace_id, execution.agent_id,
                row["file_path"], row["file_ref"], row["file_size"], expected_session_id=row["session_id"])
            if path is None:
                return _blocked("本地副本已改变，请重新检索并审批来源文件。")
        if tool_name != "bash" and requested.startswith(f"sessions/{execution.session_id}/"):
            return None
        files = [{"file_path": item.imported_path, "file_name": item.file_name} for item in released.artifacts]
        return ToolOutput(success=True, data={"content": "文件已获准并导入当前 Session；请读取导入文件，交付时复制到当前 output/ 后登记。",
                                               "files": files})
