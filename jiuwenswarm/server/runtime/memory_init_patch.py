# coding: utf-8
"""记忆库初始化：空库拷模板、建库离开事件循环、第一句回复不等建库。

事件循环可以理解成「服务员一次只能干一件事」。原来每个新会话都在这个服务员
手里现场建 sqlite 库（建表、试全文索引、加载向量插件、全量扫文件），20 个会话
会把第一句回复堵在后面。

本补丁做三件事：

1. 进程里先备好一份空的 memory.db（表和元数据都写好），新会话缺文件时拷贝，
   不再现场 CREATE。
2. 拷贝、打开、建表、加载向量插件都放进线程，不占用事件循环。
3. MemoryRail（对话记忆）在第一句回复前不再建库。模型真正调用记忆搜索时，
   工具自己会初始化（ensure_manager）。今日/昨日日记仍按原逻辑直接读文件，
   不依赖这个库。

代码模式（CodingMemoryRail）不推迟建库，避免自动召回整会话失效。
补丁是进程级的：装上后对本进程所有 MemoryRail 生效，直到 remove。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from typing import Any, Callable, Optional

from openjiuwen.core.common.logging import logger
from openjiuwen.core.memory.lite.config import MemorySettings
from openjiuwen.core.memory.lite.manager import MemoryIndexManager

__all__ = [
    "apply_memory_init_patch",
    "remove_memory_init_patch",
]

_PATCHED = False
_META_KEY = "memory_index_meta_v1"

_original_initialize: Optional[Callable] = None
_original_open_database: Optional[Callable] = None
_original_ensure_schema: Optional[Callable] = None
_original_load_vector: Optional[Callable] = None
_original_needs_rebuild: Optional[Callable] = None
_original_init_manager: Optional[Callable] = None

_template_lock = threading.Lock()
_conn_lock = threading.Lock()
_cached_template: Optional[str] = None
_ready_conns: dict[str, Any] = {}
_skip_schema_paths: set[str] = set()
_skip_vector_paths: set[str] = set()
_build_guard = threading.local()


class _EmptyMemoryWorkspace:
    """只给模板库用的空工作区，里面没有记忆文件。"""

    def __init__(self, memory_dir: str) -> None:
        self.root_path = os.path.dirname(memory_dir)
        self.memory_dir = memory_dir
        self.daily_rel: Optional[str] = None

    def get_node_path(self, node_name: str) -> Optional[str]:
        if node_name == "memory":
            return self.memory_dir
        return None

    def get_directory(self, name: str) -> Optional[str]:
        if name == "daily_memory":
            return self.daily_rel
        return None


def _db_file_missing(path: str) -> bool:
    if not os.path.isfile(path):
        return True
    try:
        size = os.path.getsize(path)
    except OSError:
        return True
    return size == 0


def _remove_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if not os.path.isfile(side):
            continue
        try:
            os.remove(side)
        except OSError as exc:
            logger.debug("memory db sidecar remove failed: %s", exc)


def _remove_path_quiet(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        logger.debug("memory db temp remove failed: %s", exc)


def _copy_sqlite_file(src: str, dest: str) -> None:
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = dest + ".copying"
    try:
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dest)
    except Exception:
        _remove_path_quiet(tmp_path)
        raise
    _remove_sidecars(dest)


def _template_stable_path() -> str:
    """进程私有模板路径，避免多进程共用 /tmp 固定文件名互相踩。"""
    return os.path.join(
        tempfile.gettempdir(),
        f"jws_empty_memory_{os.getpid()}.db",
    )


def _build_template_db() -> str:
    """用正式初始化跑一遍空目录，得到可拷贝的空库文件。"""
    root = tempfile.mkdtemp(prefix="jws_mem_tpl_")
    memory_dir = os.path.join(root, "memory")
    os.makedirs(memory_dir, exist_ok=True)
    settings = MemorySettings()
    settings.sync = {
        **settings.sync,
        "watch": False,
        "intervalMinutes": 0,
    }
    manager = MemoryIndexManager(
        "template",
        _EmptyMemoryWorkspace(memory_dir),
        settings,
        "memory",
    )
    _build_guard.active = True
    stable = _template_stable_path()
    staging_fd, staging = tempfile.mkstemp(
        prefix=f"jws_empty_memory_{os.getpid()}_",
        suffix=".db",
    )
    os.close(staging_fd)
    try:
        asyncio.run(_original_initialize(manager))
        db = manager.db
        if db is not None:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src = manager.db_path
        asyncio.run(manager.close())
        _copy_sqlite_file(src, staging)
        os.replace(staging, stable)
    except Exception:
        _remove_path_quiet(staging)
        raise
    finally:
        _build_guard.active = False
        shutil.rmtree(root, ignore_errors=True)

    logger.info("[MemoryInit] empty memory.db template ready: %s", stable)
    return stable


def _ensure_template_db() -> str:
    global _cached_template
    with _template_lock:
        cached = _cached_template
        if cached and os.path.isfile(cached):
            return cached
        built = _build_template_db()
        _cached_template = built
        return built


def _seed_empty_db(manager: MemoryIndexManager, db_path: str) -> None:
    """在线程里拷模板、打开连接、补表结构、加载向量插件。"""
    _copy_sqlite_file(_ensure_template_db(), db_path)
    manager.db_path = db_path
    manager.db = _original_open_database(db_path)
    try:
        _original_ensure_schema(manager)
        asyncio.run(_original_load_vector(manager))
    except Exception:
        db = manager.db
        manager.db = None
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                logger.debug("seed memory db close failed: %s", exc)
        raise
    with _conn_lock:
        _ready_conns[db_path] = manager.db
    _skip_schema_paths.add(db_path)
    _skip_vector_paths.add(db_path)
    logger.info("[MemoryInit] copied empty memory.db template to %s", db_path)


def _open_database_reuse(db_path: str) -> Any:
    with _conn_lock:
        ready = _ready_conns.pop(db_path, None)
    if ready is not None:
        return ready
    return _original_open_database(db_path)


def _ensure_schema_once(self: MemoryIndexManager) -> None:
    if self.db_path in _skip_schema_paths:
        return
    _original_ensure_schema(self)


async def _load_vector_off_loop(self: MemoryIndexManager) -> None:
    if self.db_path in _skip_vector_paths:
        return

    def _run() -> None:
        asyncio.run(_original_load_vector(self))

    await asyncio.to_thread(_run)


async def _needs_rebuild_none_safe(self: MemoryIndexManager) -> bool:
    """没配向量模型时，上游会读 provider.id 然后报错。这里改成按「没有模型」比较。"""
    if self.provider is not None:
        return await _original_needs_rebuild(self)
    try:
        cursor = self.db.execute(
            "SELECT value FROM meta WHERE key = ?",
            (_META_KEY,),
        )
        row = cursor.fetchone()
        if not row:
            return False
        meta = json.loads(row["value"])
    except Exception as exc:
        logger.warning("Failed to check meta for rebuild: %s", exc)
        return False
    provider_changed = meta.get("provider") is not None
    model_changed = meta.get("model") is not None
    if provider_changed or model_changed:
        return True
    key_changed = meta.get("providerKey") != self.provider_key
    chunk_changed = meta.get("chunkTokens") != self.settings.chunking.get("tokens")
    if key_changed or chunk_changed:
        return True
    return False


def _drop_leftover_conn(manager: MemoryIndexManager, db_path: str) -> None:
    with _conn_lock:
        leftover = _ready_conns.pop(db_path, None)
    if leftover is None:
        return
    if leftover is manager.db:
        return
    try:
        leftover.close()
    except Exception as exc:
        logger.debug("leftover memory db close failed: %s", exc)


async def _initialize_from_template(self: MemoryIndexManager) -> None:
    if getattr(_build_guard, "active", False):
        await _original_initialize(self)
        return
    resolve = getattr(self, "_resolve_db_path")
    db_path = resolve()
    self.db_path = db_path
    if _db_file_missing(db_path):
        await asyncio.to_thread(_seed_empty_db, self, db_path)
    try:
        await _original_initialize(self)
    finally:
        _skip_schema_paths.discard(db_path)
        _skip_vector_paths.discard(db_path)
        _drop_leftover_conn(self, db_path)


async def _defer_memory_index(self: Any, ctx: Any) -> None:
    """第一句回复前不建库。记忆工具被调用时才会初始化。"""
    _ = (self, ctx)
    logger.info("[MemoryInit] defer index build until a memory tool runs")


def apply_memory_init_patch() -> None:
    """安装记忆初始化补丁。重复调用无效果。

    只推迟对话 MemoryRail 建库；CodingMemoryRail 保持原路径，避免自动召回失效。
    """
    global _PATCHED
    global _original_initialize, _original_open_database
    global _original_ensure_schema, _original_load_vector
    global _original_needs_rebuild, _original_init_manager
    if _PATCHED:
        return

    import openjiuwen.core.memory.lite.manager as memory_manager_mod
    from openjiuwen.harness.rails.memory.memory_rail import MemoryRail

    _original_initialize = MemoryIndexManager.initialize
    _original_open_database = getattr(memory_manager_mod, "_open_database")
    _original_ensure_schema = getattr(MemoryIndexManager, "_ensure_schema")
    _original_load_vector = getattr(MemoryIndexManager, "_load_vector_extension")
    _original_needs_rebuild = getattr(
        MemoryIndexManager, "_needs_rebuild_on_config_change"
    )
    _original_init_manager = getattr(MemoryRail, "_init_memory_manager")

    setattr(memory_manager_mod, "_open_database", _open_database_reuse)
    setattr(MemoryIndexManager, "initialize", _initialize_from_template)
    setattr(MemoryIndexManager, "_ensure_schema", _ensure_schema_once)
    setattr(MemoryIndexManager, "_load_vector_extension", _load_vector_off_loop)
    setattr(
        MemoryIndexManager,
        "_needs_rebuild_on_config_change",
        _needs_rebuild_none_safe,
    )
    setattr(MemoryRail, "_init_memory_manager", _defer_memory_index)
    _PATCHED = True
    logger.info(
        "[MemoryInit] patch applied "
        "(template copy, off-loop open, defer MemoryRail until memory tool; "
        "CodingMemoryRail unchanged)"
    )


def remove_memory_init_patch() -> None:
    """卸下补丁。只给单测用。"""
    global _PATCHED
    global _original_initialize, _original_open_database
    global _original_ensure_schema, _original_load_vector
    global _original_needs_rebuild, _original_init_manager
    global _cached_template
    if not _PATCHED:
        return

    import openjiuwen.core.memory.lite.manager as memory_manager_mod
    from openjiuwen.harness.rails.memory.memory_rail import MemoryRail

    setattr(memory_manager_mod, "_open_database", _original_open_database)
    setattr(MemoryIndexManager, "initialize", _original_initialize)
    setattr(MemoryIndexManager, "_ensure_schema", _original_ensure_schema)
    setattr(MemoryIndexManager, "_load_vector_extension", _original_load_vector)
    setattr(
        MemoryIndexManager,
        "_needs_rebuild_on_config_change",
        _original_needs_rebuild,
    )
    setattr(MemoryRail, "_init_memory_manager", _original_init_manager)
    _original_initialize = None
    _original_open_database = None
    _original_ensure_schema = None
    _original_load_vector = None
    _original_needs_rebuild = None
    _original_init_manager = None
    _cached_template = None
    _skip_schema_paths.clear()
    _skip_vector_paths.clear()
    with _conn_lock:
        _ready_conns.clear()
    _PATCHED = False
