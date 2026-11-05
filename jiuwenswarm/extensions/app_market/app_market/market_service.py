from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
import uuid
from datetime import datetime

from .models import AppManifest, AppInstance, InstanceStatus, platform_key
from .storage import MarketStorage
from .port_manager import PortManager
from .process_supervisor import ProcessSupervisor
from .status_manager import StatusManager
from .skill_adapter import SkillAdapter

logger = logging.getLogger(__name__)


class MarketService:
    """应用市场核心编排器 — 协调发布、启动、停止、删除等操作。

    责任边界：
    - 调用 SkillAdapter 适配清单和脚本
    - 调用 PortManager 分配端口
    - 调用 ProcessSupervisor 启停进程
    - 调用 StatusManager 追踪状态
    - 调用 MarketStorage 持久化数据
    """

    def __init__(self, data_dir: str, market_port: int = 8099, logger=None):
        self._logger = logger or logging.getLogger(__name__)
        self._market_port = market_port
        self._data_dir = data_dir
        self._storage = MarketStorage(data_dir)
        self._port_manager = PortManager()
        self._supervisor = ProcessSupervisor(self._logger)
        self._status_manager = StatusManager(self._logger)
        self._skill_adapter = SkillAdapter(self._logger)
        self._start_locks = {}
        self._gateway_port: int | None = None
        self._gateway_web_port: int | None = None
        self._gateway_host: str | None = None
        self._app_host = "127.0.0.1"

    def configure_gateway(self, gateway_port=None, web_port=None, gateway_host=None, app_host="127.0.0.1"):
        """Record the addresses injected by the running WorkSwarm instance.

        There is deliberately no default Gateway address here.  A missing
        address must reach the application as an explicit configuration error,
        rather than silently selecting a local or bridge endpoint.
        """
        self._gateway_port = self._valid_port(gateway_port)
        self._gateway_web_port = self._valid_port(web_port)
        self._gateway_host = str(gateway_host or "").strip() or None
        self._app_host = str(app_host or "127.0.0.1").strip() or "127.0.0.1"

    @staticmethod
    def _valid_port(value) -> int | None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        return port if 1 <= port <= 65535 else None

    def _gateway_env(self):
        if not self._gateway_host or not self._gateway_port or not self._gateway_web_port:
            return {}
        return {
            "WORKSWARM_GATEWAY_HOST": self._gateway_host,
            "WORKSWARM_GATEWAY_PORT": str(self._gateway_port),
            "WORKSWARM_GATEWAY_URL": f"ws://{self._gateway_host}:{self._gateway_port}/tui",
            "WORKSWARM_GATEWAY_WEB_PORT": str(self._gateway_web_port),
            "WORKSWARM_GATEWAY_API_URL": f"ws://{self._gateway_host}:{self._gateway_web_port}/ws",
        }

    # ── 应用发布 ──────────────────────────────────────────

    async def publish_app(self, app_dir: str, options: dict) -> AppManifest | None:
        """发布应用：SkillAdapter 适配 → 验证 → 登记。

        验证失败保留草稿，不覆盖已有应用。
        """
        try:
            manifest = self._skill_adapter.adapt(app_dir, options)
            if not self._skill_adapter.validate(manifest, app_dir):
                self._logger.error("应用验证失败: %s", manifest.app_id)
                return None
            self._storage.save_manifest(manifest.app_id, manifest)
            self._logger.info("应用已上架: %s v%s", manifest.app_id, manifest.version)
            return manifest
        except Exception as e:
            self._logger.error("发布应用失败: %s", e)
            return None

    # ── 应用启动 ──────────────────────────────────────────

    async def start_app(self, app_id: str, owner_id: str = "") -> AppInstance | None:
        """Start one app per owner at a time, making repeated starts idempotent."""
        key = f"{owner_id}\x00{app_id}"
        lock = self._start_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._start_app_locked(app_id, owner_id)

    async def _start_app_locked(self, app_id: str, owner_id: str = "") -> AppInstance | None:
        """启动应用实例。

        流程：分配端口 → 创建实例 → 启动脚本 → 等待 running → 保存记录。
        启动超时 30 秒，超时后进入清理。
        """
        instance = None
        manifest = None
        try:
            manifest = self._storage.load_manifest(app_id)
            if not manifest:
                self._logger.error("应用不存在: %s", app_id)
                return None

            if manifest.owner_id and manifest.owner_id != owner_id:
                raise PermissionError("Application belongs to another user")
            # Starting an already running app should be idempotent. Reuse the
            # existing instance so opening the market card twice cannot create
            # duplicate static servers and duplicate AI sessions.
            for existing in self._storage.list_instances(app_id):
                if not owner_id or existing.owner_id != owner_id or existing.state != "running":
                    continue
                if existing.pid and self._supervisor.is_alive(existing.pid):
                    return existing
                existing.state = "stopping"
                self._storage.save_instance(existing)
            # Legacy local registrations bind to the admitted WebChannel owner.
            manifest.owner_id = owner_id
            entry = Path(manifest.work_dir) / (manifest.page_entry_path or "index.html")
            if entry.is_file() and "WorkSwarmApp" in entry.read_text(encoding="utf-8", errors="replace"):
                if "ai" not in manifest.permissions:
                    manifest.permissions.append("ai")
            self._storage.save_manifest(app_id, manifest)
            self._skill_adapter.upgrade_static_scripts(manifest)
            port = self._port_manager.allocate(app_id, manifest.default_port)
            if port == 0:
                self._logger.error("端口分配失败: %s", app_id)
                return None

            instance_id = str(uuid.uuid4())
            status_token = uuid.uuid4().hex
            now = datetime.now().isoformat()

            instance = AppInstance(
                instance_id=instance_id,
                app_id=app_id,
                port=port,
                state="starting",
                work_dir=manifest.work_dir,
                created_at=now,
                started_at=now,
                status_token=status_token,
                owner_id=owner_id,
            )

            # 构建环境变量
            env = dict(os.environ)
            for key in (
                "WORKSWARM_GATEWAY_HOST", "WORKSWARM_GATEWAY_PORT",
                "WORKSWARM_GATEWAY_URL", "WORKSWARM_GATEWAY_WEB_PORT",
                "WORKSWARM_GATEWAY_API_URL",
            ):
                env.pop(key, None)
            env.update(self._gateway_env())
            env["WORKSWARM_SOURCE_SESSION"] = manifest.source_session or ""
            env["WORKSWARM_APP_HOST"] = self._app_host
            env["WORKSWARM_APP_PORT"] = str(port)
            env["PORT"] = str(port)
            env["WORKSWARM_APP_URL"] = f"http://{self._app_host}:{port}"
            env["WORKSWARM_INSTANCE_CONTEXT"] = os.path.join(
                self._data_dir, "instances", f"{instance_id}.json"
            )
            env["WORKSWARM_STATUS_TOKEN"] = status_token
            env["WORKSWARM_PYTHON"] = sys.executable
            env["WORKSWARM_STATIC_SERVER"] = str(Path(__file__).with_name("static_server.py").resolve())

            self._storage.save_instance(instance)
            if manifest.status_mode == "push":
                env["WORKSWARM_STATUS_REPORT_URL"] = (
                    f"http://127.0.0.1:{self._market_port}/apps/status"
                )

            # 获取平台对应脚本
            pk = platform_key()
            script = manifest.start_script.get(pk, "")
            if not script:
                script = manifest.start_script.get("default", "")
            if not script:
                raise FileNotFoundError(f"No start script for {app_id} ({pk})")

            # 启动进程
            pid = await self._supervisor.start(
                script=script,
                interpreter=manifest.interpreter,
                args=manifest.args,
                env=env,
                work_dir=manifest.work_dir,
                port=port,
            )

            instance.pid = pid
            self._status_manager.update_state(instance_id, "starting")
            self._storage.save_instance(instance)

            # 等待 running 状态（超时 30 秒）
            running = await self._wait_for_running(instance, manifest, timeout=30)

            if running:
                instance.state = "running"
                self._status_manager.update_state(instance_id, "running")
                self._logger.info(
                    "应用已启动: %s instance=%s port=%d", app_id, instance_id, port
                )
            else:
                # 启动失败，清理资源
                await self._cleanup_instance(instance, manifest)
                instance.state = "stopped" if not instance.pid or not self._supervisor.is_alive(instance.pid) else "stopping"
                instance.detail = "启动超时或状态未确认"
                self._status_manager.update_state(
                    instance_id, instance.state, instance.detail
                )
                self._logger.warning(
                    "应用启动失败: %s instance=%s", app_id, instance_id
                )

            self._storage.save_instance(instance)
            return instance

        except Exception as e:
            self._logger.error("启动应用失败: %s", e)
            if instance is not None:
                await self._cleanup_instance(instance, manifest)
                instance.state = "stopped" if not instance.pid or not self._supervisor.is_alive(instance.pid) else "stopping"
                instance.detail = str(e)
                self._status_manager.update_state(instance.instance_id, instance.state, instance.detail)
                self._storage.save_instance(instance)
            return None

    async def _wait_for_running(
        self, instance: AppInstance, manifest: AppManifest, timeout: float = 30
    ) -> bool:
        """等待应用进入 running 状态。

        push 模式：检查 StatusManager 中的状态。
        pull 模式：定期查询应用 /_workswarm/status。
        """
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while loop.time() - start_time < timeout:
            # 检查 push 模式状态（已被 handle_push 更新）
            state = self._status_manager.get_state(instance.instance_id)
            if state == "running":
                return True

            # 检查进程存活
            if instance.pid and not self._supervisor.is_alive(instance.pid):
                self._logger.warning("进程已退出: pid=%s", instance.pid)
                return False

            # pull 模式：查询应用状态
            if manifest.status_mode == "pull":
                status = await self._status_manager.poll_pull(
                    instance.port, instance.status_token
                )
                if (status and status.state == "running"
                        and status.instance_id == instance.instance_id
                        and status.app_id == instance.app_id):
                    self._status_manager.handle_push(
                        instance.instance_id, instance.status_token, status
                    )
                    return True

            await asyncio.sleep(1)

        return False

    # ── 应用停止 ──────────────────────────────────────────

    async def stop_app(self, instance_id: str) -> bool:
        """停止应用实例并回收资源。

        流程：优雅停止请求 → 停止脚本/强制回收 → 释放端口 → 更新状态。
        应用自行清理的宽限 8 秒，强制回收超时 10 秒。
        """
        try:
            instance = self._storage.load_instance(instance_id)
            if not instance:
                self._logger.error("实例不存在: %s", instance_id)
                return False

            manifest = self._storage.load_manifest(instance.app_id)

            # 更新状态为 stopping
            instance.state = "stopping"
            self._status_manager.update_state(instance_id, "stopping")
            self._storage.save_instance(instance)

            # 先请求应用自行清理并退出；Windows 上没有 SIGTERM 等价物，
            # 直接强杀会让应用的退出清理（如删除其角色会话）永远没有机会执行。
            if instance.pid:
                await self._graceful_shutdown(instance)

            # 停止进程
            if instance.pid:
                stop_script = None
                if manifest:
                    stop_script = self._stop_script(manifest)
                stopped = await self._supervisor.stop(
                    pid=instance.pid,
                    stop_script=stop_script or None,
                    interpreter=manifest.interpreter if manifest else None,
                    args=manifest.args if manifest else None,
                    env=self._instance_env(instance),
                    work_dir=instance.work_dir,
                    timeout=10,
                )

                if not stopped:
                    instance.detail = "进程尚未退出"
                    self._storage.save_instance(instance)
                    return False

            # 释放端口
            self._port_manager.release(instance.port)

            instance.sessions = []
            # 更新状态为 stopped
            instance.state = "stopped"
            instance.detail = ""
            self._status_manager.update_state(instance_id, "stopped")
            self._storage.save_instance(instance)

            self._logger.info("实例已停止: %s", instance_id)
            return True

        except Exception as e:
            self._logger.error("停止实例失败: %s", e)
            return False

    async def _graceful_shutdown(self, instance: AppInstance) -> None:
        """请求应用通过 /_workswarm/shutdown 自行清理并退出。

        应用未实现该端点或未在宽限期内退出时不做任何处理，
        由调用方继续走停止脚本/强制终止路径。
        """
        try:
            if not self._supervisor.is_alive(instance.pid):
                return
            self._supervisor.track_children(instance.pid)
            if not await self._status_manager.request_shutdown(instance.port, instance.status_token):
                return
            self._logger.info(
                "已请求应用自行停止: instance=%s port=%d", instance.instance_id, instance.port
            )
            deadline = asyncio.get_event_loop().time() + 8
            while asyncio.get_event_loop().time() < deadline:
                self._supervisor.track_children(instance.pid)
                if not self._supervisor.is_alive(instance.pid):
                    self._logger.info("应用已自行退出: instance=%s", instance.instance_id)
                    return
                await asyncio.sleep(0.25)
            self._logger.warning(
                "应用未在宽限期内自行退出，回退强制终止: instance=%s", instance.instance_id
            )
        except Exception as e:
            self._logger.debug("优雅停止请求失败: %s", e)

    async def _cleanup_instance(
        self, instance: AppInstance, manifest: AppManifest | None
    ) -> None:
        """清理失败的实例资源"""
        try:
            if instance.pid:
                stop_script = None
                if manifest:
                    stop_script = self._stop_script(manifest)
                stopped = await self._supervisor.stop(
                    pid=instance.pid,
                    stop_script=stop_script or None,
                    interpreter=manifest.interpreter if manifest else None,
                    args=manifest.args if manifest else None,
                    env=self._instance_env(instance),
                    work_dir=instance.work_dir,
                    timeout=5,
                )
                if not stopped:
                    return
            self._port_manager.release(instance.port)
        except Exception as e:
            self._logger.error("清理实例资源失败: %s", e)

    def _stop_script(self, manifest: AppManifest) -> str | None:
        pk = platform_key()
        script = manifest.stop_script.get(pk) or manifest.stop_script.get("default")
        if script and manifest.run_type == "static":
            path = Path(manifest.work_dir) / script
            generated = (self._skill_adapter._generate_windows_stop() if pk == "windows"
                         else self._skill_adapter._generate_unix_stop())
            try:
                original = path.read_text(encoding="utf-8") if path.is_file() else ""
            except UnicodeError:
                return script
            if original == generated:
                # The original template kills by port; stop the owned process tree instead.
                return None
        return script

    def _instance_env(self, instance: AppInstance) -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "WORKSWARM_GATEWAY_HOST", "WORKSWARM_GATEWAY_PORT",
            "WORKSWARM_GATEWAY_URL", "WORKSWARM_GATEWAY_WEB_PORT",
            "WORKSWARM_GATEWAY_API_URL",
        ):
            env.pop(key, None)
        env.update(self._gateway_env())
        manifest = self._storage.load_manifest(instance.app_id)
        env["WORKSWARM_SOURCE_SESSION"] = (manifest.source_session or "") if manifest else ""
        env["WORKSWARM_APP_HOST"] = self._app_host
        env["WORKSWARM_APP_PORT"] = str(instance.port)
        env["PORT"] = str(instance.port)
        env["WORKSWARM_APP_URL"] = f"http://{self._app_host}:{instance.port}"
        env["WORKSWARM_INSTANCE_CONTEXT"] = os.path.join(self._data_dir, "instances", f"{instance.instance_id}.json")
        env["WORKSWARM_STATUS_TOKEN"] = instance.status_token
        return env

    # ── 应用删除 ──────────────────────────────────────────

    async def delete_app(self, app_id: str) -> bool:
        """删除应用：先停止所有实例再删除记录"""
        try:
            for instance in self._storage.list_instances(app_id):
                if instance.state in ("starting", "running", "stopping"):
                    if not await self.stop_app(instance.instance_id):
                        return False
            self._storage.delete_app(app_id)
            self._logger.info("应用已删除: %s", app_id)
            return True
        except Exception as e:
            self._logger.error("删除应用失败: %s", e)
            return False

    # ── 查询接口 ──────────────────────────────────────────

    def list_apps(self) -> list[AppManifest]:
        """列出所有已上架应用"""
        return self._storage.list_apps()

    def get_app(self, app_id: str) -> AppManifest | None:
        """获取应用详情"""
        return self._storage.load_manifest(app_id)

    def list_instances(self, app_id: str | None = None) -> list[AppInstance]:
        """列出实例，可按应用过滤"""
        return self._storage.list_instances(app_id)

    def get_instance(self, instance_id: str) -> AppInstance | None:
        """获取实例详情"""
        return self._storage.load_instance(instance_id)

    def get_instance_state(self, instance_id: str) -> str:
        """获取实例当前状态"""
        return self._status_manager.get_state(instance_id)

    # ── 状态协议 ──────────────────────────────────────────

    def handle_status_push(
        self, instance_id: str, token: str, status: InstanceStatus
    ) -> bool:
        """处理应用主动推送的状态"""
        return self._status_manager.handle_push(instance_id, token, status)

    async def poll_instance_status(self, instance: AppInstance) -> None:
        """轮询实例状态（pull 模式，默认每 5 秒）"""
        try:
            status = await self._status_manager.poll_pull(
                instance.port, instance.status_token
            )
            if status:
                self._status_manager.handle_push(
                    instance.instance_id, instance.status_token, status
                )
                instance.seq = status.seq
                if status.state == "stopped" and instance.state == "running":
                    instance.state = "stopping"
                    instance.detail = status.detail
                    self._storage.save_instance(instance)
        except Exception as e:
            self._logger.debug("轮询实例状态失败: %s", e)

    # ── 关闭 ──────────────────────────────────────────────

    async def shutdown(self) -> None:
        """关闭时停止所有运行中的实例"""
        try:
            for instance in self._storage.list_instances():
                if instance.state in ("starting", "running", "stopping"):
                    await self.stop_app(instance.instance_id)
        except Exception as e:
            self._logger.error("关闭清理失败: %s", e)
