"""AgentServer hosting poll loop + RPC-facing operations."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_hosting.auto_host import (
    channel_allows_auto_host,
    identity_ids,
    select_auto_host_candidates,
)
from jiuwenswarm.server.im.im_hosting.cli_resolve import resolve_cli_path
from jiuwenswarm.server.im.im_hosting.connectors import build_connector, channel_label
from jiuwenswarm.server.im.im_hosting.policy import CHANNEL_IDS, HostingPolicyStore
from jiuwenswarm.server.im.im_hosting.poller import run_poll_once
from jiuwenswarm.server.im.im_hosting.reply_bridge import AgentManagerLike
from jiuwenswarm.server.im.im_hosting.store import HostingStore

LOGGER = logging.getLogger(__name__)

_TICK_SECONDS = 5.0

_service: Optional["HostingPollService"] = None


def get_hosting_service() -> "HostingPollService":
    global _service
    if _service is None:
        _service = HostingPollService()
    return _service


def reset_hosting_service() -> None:
    global _service
    _service = None


class HostingPollService:
    def __init__(
        self,
        *,
        store: HostingStore | None = None,
        policy: HostingPolicyStore | None = None,
        connectors: dict[str, ChannelPlugin] | None = None,
    ) -> None:
        self.store = store or HostingStore()
        self.policy = policy or HostingPolicyStore(store=self.store)
        self._connectors = connectors
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_round_at_ms: int | None = None
        self._running = False
        self._agent_manager: AgentManagerLike | None = None
        self._last_discover_at_ms: dict[str, int] = {}

    def set_agent_manager(self, agent_manager: AgentManagerLike | None) -> None:
        """测试可注入假 runtime；线上默认走 TenantAgentPool。"""
        self._agent_manager = agent_manager

    def _plugin(self, channel_id: str) -> Optional[ChannelPlugin]:
        if self._connectors is not None:
            return self._connectors.get(channel_id)
        return build_connector(channel_id)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="im-hosting-poll")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_due()
            except Exception:
                LOGGER.exception("[im_hosting] poll round failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                continue

    def status(self) -> dict[str, Any]:
        policy = self.policy.load()
        channels = []
        for cid in CHANNEL_IDS:
            path = resolve_cli_path(cid)
            channels.append(
                {
                    "id": cid,
                    "label": channel_label(cid),
                    "cli_available": bool(path),
                    "cli_found": bool(path),
                }
            )
        return {
            "running": self._running,
            "last_round_at_ms": self._last_round_at_ms,
            "channels": channels,
            "policy": policy,
        }

    async def probe_channel(self, channel_id: str) -> dict[str, Any]:
        plugin = self._plugin(channel_id)
        if plugin is None:
            return {
                "id": channel_id,
                "label": channel_label(channel_id),
                "cli_available": False,
                "logged_in": False,
                "message": "未找到 CLI，请安装并登录后重试",
            }
        result = await plugin.test_connection()
        return {
            "id": channel_id,
            "label": channel_label(channel_id),
            "cli_available": True,
            "logged_in": bool(result.ok),
            "message": result.message,
            "error_code": result.error_code,
        }

    async def discover(self, channel_id: str, *, query_count: int | None = None) -> dict[str, Any]:
        plugin = self._plugin(channel_id)
        if plugin is None:
            raise RuntimeError(f"{channel_label(channel_id)} CLI 不可用")
        policy = self.policy.load().get(channel_id) or {}
        count = int(query_count or policy.get("discover_count") or 50)
        convs = await plugin.discover_conversations(query_count=count)
        if not convs:
            probe = await plugin.test_connection()
            if not probe.ok:
                raise RuntimeError(probe.message or f"{channel_label(channel_id)} 未登录，无法拉取近期会话")
        hosted = self.store.hosted_keys(channel_id)
        items = []
        for conv in convs:
            key = (conv.kind, conv.external_id)
            items.append(
                {
                    "channel_id": channel_id,
                    "target_kind": conv.kind,
                    "external_id": conv.external_id,
                    "title": conv.title,
                    "already_hosted": key in hosted,
                    "excluded": False,
                }
            )
        return {"channel_id": channel_id, "conversations": items}

    def _drop_auto_for_disabled_kinds(self, channel_id: str, ch_policy: dict[str, Any]) -> int:
        drop_kinds: list[str] = []
        if not ch_policy.get("auto_host_groups"):
            drop_kinds.append("group")
        if not ch_policy.get("auto_host_users"):
            drop_kinds.append("user")
        if not drop_kinds:
            return 0
        dropped = self.store.drop_auto_targets(channel_id, kinds=drop_kinds)
        if dropped:
            LOGGER.info(
                "[im_hosting] auto-host off %s dropped=%s kinds=%s",
                channel_id,
                dropped,
                drop_kinds,
            )
        return dropped

    async def apply_channel_policy(self, channel_id: str, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
        policy = self.policy.patch_channel(channel_id, patch)
        self._last_discover_at_ms.pop(channel_id, None)
        ch = policy.get(channel_id) or {}
        self._drop_auto_for_disabled_kinds(channel_id, ch)
        if channel_allows_auto_host(ch):
            summary = await self.register_auto_targets(channel_id)
            added = summary.get("added") or []
            if added:
                LOGGER.info("[im_hosting] policy auto-host %s added=%s", channel_id, len(added))
        return policy

    async def register_auto_targets(
        self,
        channel_id: str,
        *,
        conversations: list | None = None,
    ) -> dict[str, Any]:
        """按通道策略把近期新会话登记进名单；已在名单的不占封顶。"""
        ch_policy = self.policy.load().get(channel_id) or {}
        if not channel_allows_auto_host(ch_policy):
            return {"channel_id": channel_id, "added": [], "skipped": "auto_host_disabled"}
        plugin = self._plugin(channel_id)
        if plugin is None:
            return {"channel_id": channel_id, "added": [], "skipped": "cli_unavailable"}
        count = int(ch_policy.get("discover_count") or 50)
        try:
            identity = await plugin.resolve_identity()
            convs = conversations if conversations is not None else await plugin.discover_conversations(query_count=count)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[im_hosting] auto discover %s failed: %s", channel_id, exc)
            return {"channel_id": channel_id, "added": [], "error": str(exc)}
        candidates = select_auto_host_candidates(
            convs,
            hosted=self.store.hosted_keys(channel_id),
            self_ids=identity_ids(identity),
            auto_host_groups=bool(ch_policy.get("auto_host_groups")),
            auto_host_users=bool(ch_policy.get("auto_host_users")),
            max_new=int(ch_policy.get("auto_host_max_new") or 20),
        )
        added = []
        for conv in candidates:
            target = self.store.add_target(
                channel_id=channel_id,
                target_kind=conv.kind,
                external_id=conv.external_id,
                title=conv.title or conv.external_id,
                source="auto",
            )
            added.append({"id": target["id"], "target_kind": conv.kind, "external_id": conv.external_id})
        return {"channel_id": channel_id, "added": added}

    async def _discover_due(self, policy: dict[str, dict[str, Any]], now_ms: int) -> None:
        for cid in CHANNEL_IDS:
            ch_policy = policy.get(cid) or {}
            if not channel_allows_auto_host(ch_policy):
                continue
            interval_s = int(ch_policy.get("discover_interval_seconds") or 300)
            last = self._last_discover_at_ms.get(cid) or 0
            if last and (now_ms - last) < interval_s * 1000:
                continue
            summary = await self.register_auto_targets(cid)
            self._last_discover_at_ms[cid] = now_ms
            added = summary.get("added") or []
            if added:
                LOGGER.info("[im_hosting] auto-host %s added=%s", cid, len(added))

    async def poll_due(self) -> list[dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        policy = self.policy.load()
        for cid in CHANNEL_IDS:
            self._drop_auto_for_disabled_kinds(cid, policy.get(cid) or {})
        await self._discover_due(policy, now_ms)
        results: list[dict[str, Any]] = []
        for target in self.store.list_targets():
            if not target.get("enabled"):
                continue
            cid = target["channel_id"]
            ch_policy = policy.get(cid) or {}
            interval_s = target.get("poll_interval_seconds") or ch_policy.get("poll_interval_seconds") or 180
            last = target.get("last_poll_at_ms") or 0
            if last and (now_ms - int(last)) < int(interval_s) * 1000:
                continue
            results.append(await self._poll_target(target, ch_policy))
        if results:
            self._last_round_at_ms = now_ms
        return results

    async def poll_now(self, target_id: Optional[str] = None) -> list[dict[str, Any]]:
        policy = self.policy.load()
        if target_id:
            target = self.store.get_target(target_id)
            if target is None:
                raise KeyError(target_id)
            ch_policy = policy.get(target["channel_id"]) or {}
            return [await self._poll_target(target, ch_policy)]
        results = []
        for target in self.store.list_targets():
            if not target.get("enabled"):
                continue
            ch_policy = policy.get(target["channel_id"]) or {}
            results.append(await self._poll_target(target, ch_policy))
        self._last_round_at_ms = int(time.time() * 1000)
        return results

    async def _poll_target(self, target: dict[str, Any], ch_policy: dict[str, Any]) -> dict[str, Any]:
        plugin = self._plugin(str(target["channel_id"]))
        if plugin is None:
            err = f"{channel_label(target['channel_id'])} CLI 不可用"
            self.store.record_poll(str(target["id"]), preview=target.get("last_preview"), error=err)
            return {"target_id": target["id"], "ok": False, "error": err}
        fetch_count = target.get("fetch_count") or ch_policy.get("fetch_count") or 50
        try:
            summary = await run_poll_once(
                self.store,
                plugin,
                target,
                fetch_count=int(fetch_count),
                channel_policy=ch_policy,
                agent_manager=self._agent_manager,
            )
            summary["ok"] = True
            return summary
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[im_hosting] poll %s failed: %s", target.get("id"), exc)
            self.store.record_poll(str(target["id"]), preview=target.get("last_preview"), error=str(exc))
            return {"target_id": target["id"], "ok": False, "error": str(exc)}
