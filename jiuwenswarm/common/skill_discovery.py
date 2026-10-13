"""Adapt Core skill discovery to the desktop behavior channel.

Events without a real tool/session are retained locally until the desktop/cloud
contract for standalone installation is agreed. Never invent a tool invocation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

from openjiuwen.core.single_agent.skills.skill_observer import SkillDirectoryObserver, SkillDiscoveryEvent

from jiuwenswarm.common.behavior_security import BehaviorSecurityBridge, desktop_security_active, raw_event
from jiuwenswarm.common.utils import get_agent_skills_dir, logger


class SkillDiscoveryReporter:
    def __init__(self, roots: list[Path], state_path: Path, bridge: BehaviorSecurityBridge, *, interval: float = 2):
        self.roots = roots
        self.state_path = state_path
        self.bridge = bridge
        self.interval = interval
        self.records: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._watch_task = None
        self._flush_task = None
        self._stopped = False
        self._observer = self.new_observer()
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if saved.get("version") == 1 and isinstance(saved.get("records"), dict):
                self.records = saved["records"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            logger.warning("[skill-discovery] unable to load local discovery records")

    def new_observer(self) -> SkillDirectoryObserver:
        return SkillDirectoryObserver(self.on_discovery)

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "records": self.records}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)

    async def _persist(self):
        """Keep the caller's lock until a background disk write actually ends."""
        task = asyncio.create_task(asyncio.to_thread(self._save))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop its writer. Releasing the lock
            # early would let the next save reuse the same temporary filename.
            await task
            raise

    @staticmethod
    def _key(path: Path, fingerprint: str) -> str:
        return hashlib.sha256(f"{os.path.normcase(str(path.resolve()))}:{fingerprint}".encode()).hexdigest()

    async def on_discovery(self, discovery: SkillDiscoveryEvent) -> bool:
        snapshot = discovery.snapshot
        key = self._key(snapshot.directory, snapshot.fingerprint)
        ctx = discovery.context
        event = None
        if ctx is not None and getattr(getattr(ctx, "inputs", None), "tool_call", None) is not None:
            event = raw_event(ctx, "skill.discovered")
            if not event.get("sessionId") or not event["tool"].get("id"):
                event = None
        async with self._lock:
            previous = self.records.get(key)
            if previous and (previous.get("status") in ("queued", "installer_event", "pending") or event is None):
                return True
            # Bound persisted document snapshots. Full records are retried by the
            # Core observer rather than silently treating a dropped event as sent.
            pending = sum(record.get("status") not in ("queued", "installer_event") for record in self.records.values())
            if not previous and pending >= 512:
                logger.warning("[skill-discovery] local pending queue full")
                return False
            skill = {"name": snapshot.directory.name, "source": "filesystem",
                     "stagedPath": str(snapshot.directory), "destination": str(snapshot.directory),
                     "documentBase64": base64.b64encode(snapshot.content).decode("ascii")}
            if event is not None:
                event.update({"eventId": key, "mode": "report", "skill": skill})
            self.records[key] = {"status": "pending" if event else "awaiting_context",
                                 "path": str(snapshot.directory), "fingerprint": snapshot.fingerprint,
                                 "skill": skill, "event": event}
            try:
                await self._persist()
            except OSError:
                if previous is None:
                    self.records.pop(key, None)
                else:
                    self.records[key] = previous
                raise
        logger.info("[skill-discovery] req=%s skill=%r status=%s", key, skill["name"], self.records[key]["status"])
        if event is not None:
            self._schedule_flush()
        return True

    async def mark_install_reported(self, destination: Path, fingerprint: str) -> bool:
        """An explicit before-install event already represents this document."""
        key = self._key(destination, fingerprint)
        async with self._lock:
            previous = self.records.get(key)
            self.records[key] = {"status": "installer_event", "path": str(destination), "fingerprint": fingerprint}
            try:
                await self._persist()
            except OSError:
                if previous is None:
                    self.records.pop(key, None)
                else:
                    self.records[key] = previous
                # Reporting bookkeeping must not turn an allowed install into
                # a denied one. A later discovery may safely report it again.
                logger.warning("[skill-discovery] req=%s install record persistence failed", key)
                return False
        return True

    def _schedule_flush(self):
        if not self._stopped and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.create_task(self.flush())

    async def flush(self):
        async with self._delivery_lock:
            await self._flush_pending()

    async def _flush_pending(self):
        for key, record in list(self.records.items()):
            if self._stopped:
                return
            if record.get("status") != "pending" or not record.get("event"):
                continue
            reply = await self.bridge._send(record["event"])
            if reply.get("decision") != "accepted":
                continue
            async with self._lock:
                self.records[key] = {"status": "queued", "path": record["path"], "fingerprint": record["fingerprint"]}
                try:
                    await self._persist()
                except OSError:
                    self.records[key] = record
                    logger.warning("[skill-discovery] req=%s persistence failed", key)
                    continue
            logger.info("[skill-discovery] req=%s status=desktop_queued", key)

    async def scan(self):
        await self._observer.scan(self.roots, settle_seconds=1)
        self._schedule_flush()

    def start(self):
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch())

    async def _watch(self):
        while not self._stopped:
            try:
                await self.scan()
            except Exception as exc:
                logger.warning("[skill-discovery] scan failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.interval)

    async def close(self):
        self._stopped = True
        tasks = [task for task in (self._watch_task, self._flush_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.bridge.close()


_reporter: SkillDiscoveryReporter | None = None


def get_skill_discovery_reporter() -> SkillDiscoveryReporter | None:
    global _reporter
    if not desktop_security_active():
        return None
    if _reporter is None:
        from jiuwenswarm.common.config import get_config
        root = get_agent_skills_dir()
        _reporter = SkillDiscoveryReporter([root], root.parent / ".skill-behavior-reports.json",
                                           BehaviorSecurityBridge(get_config()))
    return _reporter


async def stop_skill_discovery() -> None:
    global _reporter
    reporter, _reporter = _reporter, None
    if reporter is not None:
        await reporter.close()
