from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from .models import AppInstance, AppManifest

logger = logging.getLogger(__name__)


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

    def delete_app(self, app_id: str) -> None:
        """删除应用，同时级联删除其实例记录"""
        try:
            # 删除应用目录
            app_dir = self._apps_dir / app_id
            if app_dir.exists():
                self._remove_tree(app_dir)
            # 级联删除该应用的所有实例记录
            for instance in self.list_instances(app_id):
                self.delete_instance(instance.instance_id)
        except Exception as e:
            logger.error("删除应用失败 app_id=%s: %s", app_id, e)

    # ── 实例记录 ──────────────────────────────────────────

    def save_instance(self, instance: AppInstance) -> None:
        """保存实例记录到 {data_dir}/instances/{instance_id}.json"""
        try:
            self._instances_dir.mkdir(parents=True, exist_ok=True)
            instance_path = self._instances_dir / f"{instance.instance_id}.json"
            with open(instance_path, "w", encoding="utf-8") as f:
                json.dump(asdict(instance), f, ensure_ascii=False, indent=2)
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

    # ── 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def _remove_tree(path: Path) -> None:
        """递归删除目录树"""
        import shutil

        shutil.rmtree(path, ignore_errors=True)
