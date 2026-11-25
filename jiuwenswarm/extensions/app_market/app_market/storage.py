from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from .models import AppInstance, AppManifest

logger = logging.getLogger(__name__)

# Keep a short diagnostic tail per application instead of one record per start.
MAX_STOPPED_INSTANCES_PER_APP = 5
APP_MARKET_DIRNAME = "app_market"


class MarketStorage:
    """应用市场存储层 — 使用 JSON 文件持久化应用清单和实例记录"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self._apps_dir = self.data_dir / "apps"
        self._instances_dir = self.data_dir / "instances"
        # 确保目录存在
        self._apps_dir.mkdir(parents=True, exist_ok=True)
        self._instances_dir.mkdir(parents=True, exist_ok=True)

    # ── 应用清单 ──────────────────────────────────────────

    def save_manifest(self, app_id: str, manifest: AppManifest) -> None:
        """保存应用清单到 {data_dir}/apps/{app_id}/manifest.json"""
        app_dir = self._apps_dir / app_id
        app_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = app_dir / "manifest.json"
        temporary = app_dir / f"manifest-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as f:
                json.dump(asdict(manifest), f, ensure_ascii=False, indent=2)
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def load_manifest(self, app_id: str) -> AppManifest | None:
        """从文件加载应用清单"""
        try:
            manifest_path = self._apps_dir / app_id / "manifest.json"
            if not manifest_path.exists():
                return None
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return AppManifest(**data)
        except Exception as e:
            logger.error("加载应用清单失败 app_id=%s: %s", app_id, e)
            return None

    def list_apps(self) -> list[AppManifest]:
        """列出所有已注册的应用清单"""
        result: list[AppManifest] = []
        try:
            if not self._apps_dir.exists():
                return result
            for app_dir in self._apps_dir.iterdir():
                if not app_dir.is_dir():
                    continue
                manifest_path = app_dir / "manifest.json"
                if not manifest_path.exists():
                    continue
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    result.append(AppManifest(**data))
                except Exception as e:
                    logger.warning("跳过损坏的应用清单 %s: %s", app_dir.name, e)
        except Exception as e:
            logger.error("列出应用清单失败: %s", e)
        return result

    def contains_hub_install(self, path: Path) -> bool:
        """Identify paths owned by an existing market installation."""
        path = path.resolve()
        return any(
            path.is_relative_to(Path(app.work_dir).resolve())
            for app in self.list_apps()
            if app.source == "hub" and app.work_dir
        )

    def find_local_app(self, entry: Path, owner_id: str) -> AppManifest | None:
        for app in self.list_apps():
            if app.source == "local" and app.owner_id == owner_id and app.work_dir:
                if (Path(app.work_dir) / (app.page_entry_path or "index.html")).resolve() == entry:
                    return app
        return None

    def contains_local_app(self, work_dir: Path) -> bool:
        """Check for a legacy local registration inside the installed work tree."""
        work_dir = work_dir.resolve()
        return any(
            Path(app.work_dir).resolve().is_relative_to(work_dir)
            for app in self.list_apps()
            if app.source == "local" and app.work_dir
        )

    def has_overlapping_work_dir(self, work_dir: Path) -> bool:
        """Check whether cleanup could affect any remaining registration.

        Both sides are compared as canonical paths, and an unresolvable path
        counts as a possible overlap so cleanup keeps files it cannot verify.
        """
        try:
            target = Path(work_dir).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return True
        for app in self.list_apps():
            if not app.work_dir:
                continue
            try:
                other_dir = Path(app.work_dir).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                return True
            if other_dir.is_relative_to(target) or target.is_relative_to(other_dir):
                return True
        return False

    def delete_app(self, app_id: str) -> None:
        """删除应用，同时级联删除其实例记录"""
        app_dir = self._apps_dir / app_id
        if app_dir.exists():
            shutil.rmtree(app_dir)
        for instance in self.list_instances(app_id):
            self.delete_instance(instance.instance_id)

    # ── 安装凭据（Hub 来源） ──────────────────────────────

    def install_record_path(self, app_id: str) -> Path:
        return self._apps_dir / app_id / "install.json"

    def save_install_record(self, app_id: str, record: dict) -> None:
        """将 Hub 安装凭据与注册清单放在同一目录，原子写入。"""
        target = self.install_record_path(app_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f"install-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def load_install_record(self, app_id: str) -> dict | None:
        path = self.install_record_path(app_id)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("读取安装凭据失败 app_id=%s: %s", app_id, e)
            return None
        return value if isinstance(value, dict) else None

    def delete_install_record(self, app_id: str) -> None:
        self.install_record_path(app_id).unlink(missing_ok=True)

    # ── 实例记录 ──────────────────────────────────────────

    def save_instance(self, instance: AppInstance) -> None:
        """保存实例记录到 {data_dir}/instances/{instance_id}.json

        The record is replaced atomically so an interrupted write cannot
        truncate the last known identity; ``stop_app`` needs that identity to
        reclaim a process that outlived a crash.
        """
        try:
            self._instances_dir.mkdir(parents=True, exist_ok=True)
            instance_path = self._instances_dir / f"{instance.instance_id}.json"
            temporary = self._instances_dir / f"{instance.instance_id}-{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("w", encoding="utf-8") as f:
                    json.dump(asdict(instance), f, ensure_ascii=False, indent=2)
                os.replace(temporary, instance_path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as e:
            logger.error("保存实例记录失败 instance_id=%s: %s", instance.instance_id, e)

    def load_instance(self, instance_id: str) -> AppInstance | None:
        """从文件加载实例记录"""
        try:
            instance_path = self._instances_dir / f"{instance_id}.json"
            if not instance_path.exists():
                return None
            with open(instance_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return AppInstance(**data)
        except Exception as e:
            logger.error("加载实例记录失败 instance_id=%s: %s", instance_id, e)
            return None

    def list_instances(self, app_id: str | None = None) -> list[AppInstance]:
        """列出实例记录，可按 app_id 过滤"""
        result: list[AppInstance] = []
        try:
            if not self._instances_dir.exists():
                return result
            for instance_file in self._instances_dir.glob("*.json"):
                try:
                    with open(instance_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    instance = AppInstance(**data)
                    if app_id is None or instance.app_id == app_id:
                        result.append(instance)
                except Exception as e:
                    logger.warning("跳过损坏的实例记录 %s: %s", instance_file.name, e)
        except Exception as e:
            logger.error("列出实例记录失败: %s", e)
        return result

    def delete_instance(self, instance_id: str) -> None:
        """删除单个实例记录"""
        try:
            instance_path = self._instances_dir / f"{instance_id}.json"
            if instance_path.exists():
                instance_path.unlink()
        except Exception as e:
            logger.error("删除实例记录失败 instance_id=%s: %s", instance_id, e)

    def prune_instances(
        self,
        app_id: str,
        *,
        keep_stopped: int = MAX_STOPPED_INSTANCES_PER_APP,
    ) -> None:
        """Delete obsolete stopped records, keeping the newest few.

        Active records are never removed: they still own a process, port or
        cleanup task. ``AppMarketManager.start_error`` reads the latest record,
        so the retained tail keeps the most recent failure reason available.
        """
        try:
            instances = self.list_instances(app_id)
        except Exception as e:
            logger.error("清理实例记录失败 app_id=%s: %s", app_id, e)
            return
        self._prune_records(instances, keep_stopped)

    def prune_all_instances(
        self,
        *,
        keep_stopped: int = MAX_STOPPED_INSTANCES_PER_APP,
    ) -> None:
        """Bound records left by earlier runs, including pre-upgrade history."""
        try:
            grouped: dict[str, list[AppInstance]] = {}
            for instance in self.list_instances():
                grouped.setdefault(instance.app_id, []).append(instance)
        except Exception as e:
            logger.error("扫描实例记录失败: %s", e)
            return
        for instances in grouped.values():
            self._prune_records(instances, keep_stopped)

    def _prune_records(
        self,
        instances: list[AppInstance],
        keep_stopped: int,
    ) -> None:
        stopped = sorted(
            (item for item in instances if item.state == "stopped"),
            key=lambda item: item.created_at or "",
        )
        excess = stopped if keep_stopped <= 0 else stopped[:-keep_stopped]
        for instance in excess:
            self.delete_instance(instance.instance_id)
