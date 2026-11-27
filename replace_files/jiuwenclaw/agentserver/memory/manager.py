# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory Index Manager - Core memory management for JiuWenClaw."""

import logging
import os
import json
import sqlite3
import struct
import asyncio
import datetime
from typing import List, Optional, Dict, Any, Set
from dataclasses import dataclass

from .types import (
    MemorySearchResult, MemoryFileEntry, MemoryChunk, MemorySource
)
from .internal import (
    ensure_dir, list_memory_files, build_file_entry, chunk_markdown,
    hash_text, build_fts_query, bm25_rank_to_score, is_memory_path
)
from .embeddings import EmbeddingProvider, create_embedding_provider
from .config import MemorySettings

logger = logging.getLogger(__name__)

META_KEY = "memory_index_meta_v1"
SNIPPET_MAX_CHARS = 700
VECTOR_TABLE = "chunks_vec"
FTS_TABLE = "chunks_fts"
EMBEDDING_CACHE_TABLE = "embedding_cache"
SESSION_DIRTY_DEBOUNCE_MS = 5000

INDEX_CACHE: Dict[str, 'MemoryIndexManager'] = {}


@dataclass
class SessionDeltaState:
    """Tracks incremental changes to a session file."""
    last_size: int = 0
    pending_bytes: int = 0
    pending_messages: int = 0


def vector_to_blob(embedding: List[float]) -> bytes:
    """Convert vector to binary blob."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def blob_to_vector(blob: bytes) -> List[float]:
    """Convert binary blob to vector."""
    count = len(blob) // 4
    return list(struct.unpack(f'{count}f', blob))


class MemoryIndexManager:
    """Manages memory indexing and search."""

    def __init__(
            self,
            agent_id: str,
            workspace_dir: str,
            settings: MemorySettings
    ):
        self.agent_id = agent_id
        self.workspace_dir = workspace_dir
        self.settings = settings

        self.db: Optional[sqlite3.Connection] = None
        self.db_path: str = ""

        self.provider: Optional[EmbeddingProvider] = None
        self.provider_key: str = ""

        self.dirty = True
        self.sessions_dirty = False
        self.sessions_dirty_files: Set[str] = set()
        self.session_warm: Set[str] = set()
        self.closed = False

        self.fts_enabled = settings.store.get("fts", {}).get("enabled", True)
        self.vector_enabled = settings.store.get("vector", {}).get("enabled", True)
        self.cache_enabled = settings.cache.get("enabled", True)

        self.fts_available = False
        self.fts_error: Optional[str] = None
        self.vector_available = False
        self.vector_error: Optional[str] = None
        self.vector_dims: Optional[int] = None

        self._interval_timer: Optional[asyncio.Task] = None
        self._watch_timer: Optional[asyncio.Task] = None
        self._session_timer: Optional[asyncio.Task] = None
        self._session_pending_files: Set[str] = set()
        self._session_deltas: Dict[str, SessionDeltaState] = {}

        self._file_observer: Optional[Any] = None
        self._watcher_paths: Set[str] = set()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._watcher_initialized: bool = False
        self._file_stability_tracker: Dict[str, float] = {}

    @classmethod
    async def get(
            cls,
            agent_id: str,
            workspace_dir: str,
            settings: Optional[MemorySettings] = None
    ) -> Optional['MemoryIndexManager']:
        """Get or create memory index manager."""
        cache_key = f"{agent_id}:{workspace_dir}"

        if cache_key in INDEX_CACHE:
            manager = INDEX_CACHE[cache_key]
            if not manager.closed:
                return manager

        settings = settings or MemorySettings()
        manager = cls(agent_id, workspace_dir, settings)

        try:
            await manager._initialize()
            INDEX_CACHE[cache_key] = manager
            return manager
        except Exception as e:
            logger.error(f"Failed to initialize memory manager: {e}")
            return None

    async def _initialize(self) -> None:
        """Initialize the memory manager."""
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

        self.db_path = self._resolve_db_path()
        self.db = self._open_database(self.db_path)
        self._ensure_schema()
        await self._initialize_provider()
        await self._load_vector_extension()

        await self.sync(reason="initial")

        if self.settings.sync.get("watch", True):
            self._setup_file_watcher()

        self._ensure_interval_sync()

        logger.info(f"Memory manager initialized for agent: {self.agent_id}")

    def _resolve_db_path(self) -> str:
        """Resolve database path.

        确保向量数据库索引文件存放在与 MEMORY.md 同目录 (workspace_dir/memory/)
        """
        store_path = self.settings.store.get("path", "memory.db")
        if os.path.isabs(store_path):
            return store_path

        # 如果 store_path 已经包含 workspace_dir 的部分路径，避免重复拼接
        workspace_name = os.path.basename(self.workspace_dir)
        if store_path.startswith(f"{workspace_name}/") or store_path.startswith(f"{workspace_name}\\"):
            # 去除重复的 workspace 目录前缀
            store_path = store_path[len(workspace_name) + 1:]

        # 确保使用 memory 子目录，与 MEMORY.md 同目录
        if not store_path.startswith("memory/") and not store_path.startswith("memory\\"):
            store_path = os.path.join("memory", store_path)

        return os.path.join(self.workspace_dir, store_path)

    def _open_database(self, db_path: str) -> sqlite3.Connection:
        """Open SQLite database."""
        ensure_dir(os.path.dirname(db_path) or ".")

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        return conn

    def _ensure_schema(self) -> None:
        """Ensure database schema exists."""
        if not self.db:
            raise RuntimeError("Database not initialized")

        self.db.execute("""
                        CREATE TABLE IF NOT EXISTS meta
                        (
                            key
                            TEXT
                            PRIMARY
                            KEY,
                            value
                            TEXT
                        )
                        """)

        self.db.execute("""
                        CREATE TABLE IF NOT EXISTS files
                        (
                            path
                            TEXT
                            PRIMARY
                            KEY,
                            source
                            TEXT,
                            hash
                            TEXT,
                            mtime
                            INTEGER,
                            size
                            INTEGER
                        )
                        """)

        self.db.execute("""
                        CREATE TABLE IF NOT EXISTS chunks
                        (
                            id
                            TEXT
                            PRIMARY
                            KEY,
                            path
                            TEXT,
                            source
                            TEXT,
                            start_line
                            INTEGER,
                            end_line
                            INTEGER,
                            hash
                            TEXT,
                            model
                            TEXT,
                            text
                            TEXT,
                            embedding
                            BLOB,
                            updated_at
                            INTEGER
                        )
                        """)

        self.db.execute(f"""
            CREATE TABLE IF NOT EXISTS {EMBEDDING_CACHE_TABLE} (
                provider TEXT,
                model TEXT,
                provider_key TEXT,
                hash TEXT PRIMARY KEY,
                embedding BLOB,
                dims INTEGER,
                updated_at INTEGER
            )
        """)

        if self.fts_enabled:
            try:
                self.db.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
                        id UNINDEXED,
                        path UNINDEXED,
                        source UNINDEXED,
                        text,
                        content='',
                        contentless_delete=1
                    )
                """)
                self.fts_available = True
            except Exception as e:
                self.fts_available = False
                self.fts_error = str(e)
                logger.warning(f"Failed to create FTS5 table: {e}")

        self.db.commit()

    async def _initialize_provider(self) -> None:
        """Initialize embedding provider."""
        try:
            self.provider = await create_embedding_provider(
                provider=self.settings.provider,
                model=self.settings.model,
                fallback=self.settings.fallback
            )
            self.provider_key = f"{self.provider.id}:{self.provider.model}"
            logger.info(f"Embedding provider: {self.provider.id} / {self.provider.model}")
        except Exception as e:
            logger.error(f"Failed to initialize embedding provider: {e}")
            raise

    async def _load_vector_extension(self) -> None:
        """Load sqlite-vec extension."""
        if not self.vector_enabled or not self.db:
            return

        try:
            import sqlite_vec
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
            self.vector_available = True
            logger.info("sqlite-vec extension loaded successfully")
        except Exception as e:
            self.vector_available = False
            self.vector_error = str(e)
            logger.warning(f"Failed to load sqlite-vec extension: {e}")

    def _ensure_vector_table(self, dims: int) -> bool:
        """Ensure vector virtual table exists with correct dimensions."""
        if not self.db or not self.vector_available:
            return False

        try:
            if self.vector_dims == dims:
                return True

            if self.vector_dims is not None and self.vector_dims != dims:
                try:
                    self.db.execute(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
                except:
                    pass

            self.db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0(
                    embedding float[{dims}]
                )
            """)
            self.vector_dims = dims
            logger.info(f"Vector table created with dims={dims}")
            return True

        except Exception as e:
            logger.warning(f"Failed to create vector table: {e}")
            self.vector_available = False
            self.vector_error = str(e)
            return False

    def _setup_file_watcher(self) -> None:
        """Setup file system watcher for memory files."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class MemoryFileHandler(FileSystemEventHandler):
                def __init__(self, manager: 'MemoryIndexManager'):
                    self.manager = manager

                def on_modified(self, event):
                    if not event.is_directory and self.manager._watcher_initialized:
                        self._handle_change(event.src_path, "modified")

                def on_created(self, event):
                    if not event.is_directory and self.manager._watcher_initialized:
                        self._handle_change(event.src_path, "created")

                def on_deleted(self, event):
                    if not event.is_directory and self.manager._watcher_initialized:
                        self._handle_change(event.src_path, "deleted")

                def _handle_change(self, path: str, event_type: str):
                    rel_path = os.path.relpath(path, self.manager.workspace_dir)
                    if is_memory_path(rel_path):
                        self.manager._schedule_watch_sync(path, event_type)

            watch_paths = set()

            for filename in ["MEMORY.md", "memory.md"]:
                filepath = os.path.join(self.workspace_dir, filename)
                if os.path.exists(filepath) and not os.path.islink(filepath):
                    watch_paths.add(filepath)

            memory_dir = os.path.join(self.workspace_dir, "memory")
            if os.path.exists(memory_dir) and not os.path.islink(memory_dir):
                watch_paths.add(memory_dir)

            for extra_path in self.settings.extraPaths:
                full_path = os.path.join(self.workspace_dir, extra_path)
                if os.path.exists(full_path) and not os.path.islink(full_path):
                    watch_paths.add(full_path)

            if not watch_paths:
                logger.debug("No memory paths to watch")
                return

            self._file_observer = Observer()
            handler = MemoryFileHandler(self)

            for watch_path in watch_paths:
                if os.path.isdir(watch_path):
                    self._file_observer.schedule(handler, watch_path, recursive=True)
                else:
                    parent_dir = os.path.dirname(watch_path)
                    if parent_dir:
                        self._file_observer.schedule(handler, parent_dir, recursive=False)
                self._watcher_paths.add(watch_path)

            self._file_observer.start()

            self._watcher_initialized = False
            if self._event_loop:
                self._event_loop.call_later(1.0, self._set_watcher_initialized)

            logger.info(f"File watcher started for {len(watch_paths)} path(s)")

        except ImportError:
            logger.warning("watchdog not installed, file watching disabled")
        except Exception as e:
            logger.error(f"Failed to setup file watcher: {e}")

    def _set_watcher_initialized(self) -> None:
        """Mark watcher as initialized after initial scan period."""
        self._watcher_initialized = True
        logger.debug("File watcher initialized")

    def _schedule_watch_sync(self, path: Optional[str] = None, event_type: Optional[str] = None) -> None:
        """Schedule a sync after file change (debounced)."""
        self.dirty = True

        if not self._event_loop:
            return

        debounce_ms = self.settings.sync.get("watchDebounceMs", 2000)

        def schedule_sync():
            if self.closed:
                return

            if self._watch_timer:
                self._watch_timer.cancel()

            async def do_watch_sync():
                await asyncio.sleep(debounce_ms / 1000)
                self._watch_timer = None

                if not self.closed:
                    try:
                        await self.sync(reason="watch")
                    except Exception as e:
                        logger.warning(f"Memory sync failed (watch): {e}")

            self._watch_timer = asyncio.create_task(do_watch_sync())

        try:
            self._event_loop.call_soon_threadsafe(schedule_sync)
        except Exception as e:
            logger.debug(f"Failed to schedule sync: {e}")

    def _ensure_interval_sync(self) -> None:
        """Setup interval-based sync if configured."""
        minutes = self.settings.sync.get("intervalMinutes", 0)
        if not minutes or minutes <= 0:
            return

        if self._interval_timer:
            return

        async def interval_sync():
            while not self.closed:
                await asyncio.sleep(minutes * 60)
                if not self.closed:
                    try:
                        await self.sync(reason="interval")
                    except Exception as e:
                        logger.warning(f"Memory sync failed (interval): {e}")

        self._interval_timer = asyncio.create_task(interval_sync())
        logger.info(f"Interval sync enabled: every {minutes} minutes")

    async def sync(
            self,
            reason: Optional[str] = None,
            force: bool = False
    ) -> None:
        """Synchronize memory index."""
        if self.closed:
            return

        needs_full_reindex = force or await self._should_full_reindex()

        if needs_full_reindex:
            logger.info(f"Running full reindex (reason: {reason or 'unknown'})...")
            await self._run_reindex()
            return

        logger.debug(f"Memory sync (reason: {reason or 'unknown'})...")

        if "memory" in self.settings.sources and self.dirty:
            await self._sync_memory_files()
            self.dirty = False

        if "sessions" in self.settings.sources:
            await self._sync_session_files()

    async def _should_full_reindex(self) -> bool:
        """Check if full reindex is needed."""
        try:
            cursor = self.db.execute(
                "SELECT value FROM meta WHERE key = ?",
                (META_KEY,)
            )
            row = cursor.fetchone()

            if not row:
                return True

            meta = json.loads(row["value"])

            if meta.get("provider") != self.provider.id:
                return True

            if meta.get("model") != self.provider.model:
                return True

            if meta.get("chunkTokens") != self.settings.chunking.get("tokens"):
                return True

            return False

        except Exception as e:
            logger.warning(f"Failed to check meta: {e}")
            return True

    async def _run_reindex(self) -> None:
        """Run full reindex."""
        if "memory" in self.settings.sources:
            await self._sync_memory_files()
            self.dirty = False

        if "sessions" in self.settings.sources:
            await self._sync_session_files()

        meta = {
            "provider": self.provider.id,
            "model": self.provider.model,
            "providerKey": self.provider_key,
            "chunkTokens": self.settings.chunking.get("tokens"),
            "chunkOverlap": self.settings.chunking.get("overlap"),
        }
        if self.vector_available and self.vector_dims:
            meta["vectorDims"] = self.vector_dims

        self._write_meta(meta)

    def _is_recent_session_file(self, filename: str) -> bool:
        """Check if the file is today's or yesterday's session record.

        Session files are named as YYYY-MM-DD.md in the memory/ directory.

        Args:
            filename: The filename to check

        Returns:
            True if the file is from today or yesterday
        """
        import re

        match = re.match(r'^(\d{4}-\d{2}-\d{2})\.md$', filename)
        if not match:
            return False

        date_str = match.group(1)

        try:
            file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return False

        today = datetime.datetime.now().date()
        yesterday = today - datetime.timedelta(days=1)

        return file_date in (today, yesterday)

    async def _sync_memory_files(self) -> None:
        """Sync memory files.

        All session files (YYYY-MM-DD.md) are indexed for search.
        Recent session files (today + yesterday) are also loaded for context.
        """
        files = list_memory_files(self.workspace_dir, self.settings.extraPaths)

        logger.debug(f"Syncing {len(files)} memory files")

        active_paths = set()

        for filepath in files:
            entry = await build_file_entry(filepath, self.workspace_dir)
            active_paths.add(entry["path"])

            cursor = self.db.execute(
                "SELECT hash FROM files WHERE path = ? AND source = ?",
                (entry["path"], "memory")
            )
            row = cursor.fetchone()

            if row and row["hash"] == entry["hash"]:
                continue

            await self._index_file(entry, "memory")

        cursor = self.db.execute("SELECT path FROM files WHERE source = ?", ("memory",))
        for row in cursor.fetchall():
            if row["path"] not in active_paths:
                self._remove_file_from_index(row["path"])

    async def _sync_session_files(self) -> None:
        """Sync session transcript files."""
        sessions_dir = os.path.join(self.workspace_dir, "sessions")
        if not os.path.exists(sessions_dir):
            return

        session_files = []
        for root, _, files in os.walk(sessions_dir):
            for f in files:
                if f.endswith(".jsonl"):
                    session_files.append(os.path.join(root, f))

        logger.debug(f"Syncing {len(session_files)} session files")

        active_paths = set()
        for session_file in session_files:
            entry = await build_file_entry(session_file, self.workspace_dir)
            active_paths.add(entry["path"])

            cursor = self.db.execute(
                "SELECT hash FROM files WHERE path = ? AND source = ?",
                (entry["path"], "sessions")
            )
            row = cursor.fetchone()

            if row and row["hash"] == entry["hash"]:
                continue

            await self._index_file(entry, "sessions")

        cursor = self.db.execute("SELECT path FROM files WHERE source = ?", ("sessions",))
        for row in cursor.fetchall():
            if row["path"] not in active_paths:
                self._remove_file_from_index(row["path"])

    async def _index_file(self, entry: Dict[str, Any], source: str) -> None:
        """Index a single file."""
        try:
            with open(entry["absPath"], "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            chunks = chunk_markdown(content, self.settings.chunking)

            self.db.execute("DELETE FROM chunks WHERE path = ?", (entry["path"],))
            if self.fts_available:
                try:
                    self.db.execute(f"DELETE FROM {FTS_TABLE} WHERE path = ?", (entry["path"],))
                except:
                    pass

            for chunk in chunks:
                await self._index_chunk(entry["path"], source, chunk)

            self.db.execute("""
                INSERT OR REPLACE INTO files (path, source, hash, mtime, size)
                VALUES (?, ?, ?, ?, ?)
            """, (entry["path"], source, entry["hash"], entry["mtimeMs"], entry["size"]))

            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to index file {entry['path']}: {e}")
            self.db.rollback()

    async def _index_chunk(
            self,
            file_path: str,
            source: str,
            chunk: MemoryChunk
    ) -> None:
        """Index a single chunk."""
        chunk_id = f"{file_path}:{chunk.startLine}:{chunk.endLine}"
        chunk_hash = hash_text(chunk.text)

        embedding = await self._get_embedding(chunk.text)

        cursor = self.db.execute("""
            INSERT OR REPLACE INTO chunks
            (id, path, source, start_line, end_line, hash, model, text, embedding, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING rowid
        """, (
            chunk_id, file_path, source, chunk.startLine, chunk.endLine,
            chunk_hash, self.provider.model, chunk.text,
            vector_to_blob(embedding) if embedding else None,
            int(asyncio.get_event_loop().time()) if self._event_loop else 0
        ))
        row = cursor.fetchone()
        chunk_rowid = row["rowid"] if row else None

        if self.fts_available and chunk_rowid:
            try:
                self.db.execute(f"""
                    INSERT OR REPLACE INTO {FTS_TABLE} (rowid, id, path, source, text)
                    VALUES (?, ?, ?, ?, ?)
                """, (chunk_rowid, chunk_id, file_path, source, chunk.text))
            except Exception as e:
                logger.debug(f"Failed to insert into FTS: {e}")

        if self.vector_available and embedding and chunk_rowid:
            try:
                if self._ensure_vector_table(len(embedding)):
                    self.db.execute(f"""
                        INSERT OR REPLACE INTO {VECTOR_TABLE} (rowid, embedding)
                        VALUES (?, vec_f32(?))
                    """, (chunk_rowid, vector_to_blob(embedding)))
            except Exception as e:
                logger.debug(f"Failed to insert into vector table: {e}")

    def _remove_file_from_index(self, file_path: str) -> None:
        """Remove file from index."""
        try:
            if self.vector_available:
                cursor = self.db.execute(
                    "SELECT rowid FROM chunks WHERE path = ?", (file_path,)
                )
                for row in cursor.fetchall():
                    try:
                        self.db.execute(f"DELETE FROM {VECTOR_TABLE} WHERE rowid = ?", (row["rowid"],))
                    except:
                        pass

            if self.fts_available:
                cursor = self.db.execute("SELECT rowid FROM chunks WHERE path = ?", (file_path,))
                for row in cursor.fetchall():
                    try:
                        self.db.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid = ?", (row["rowid"],))
                    except:
                        pass

            self.db.execute("DELETE FROM chunks WHERE path = ?", (file_path,))
            self.db.execute("DELETE FROM files WHERE path = ?", (file_path,))
            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to remove file from index: {e}")
            self.db.rollback()

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding for text (with caching)."""
        if not self.provider:
            return None

        text_hash = hash_text(text)

        if self.cache_enabled:
            cursor = self.db.execute(f"""
                SELECT embedding FROM {EMBEDDING_CACHE_TABLE}
                WHERE provider = ? AND model = ? AND provider_key = ? AND hash = ?
            """, (self.provider.id, self.provider.model, self.provider_key, text_hash))
            row = cursor.fetchone()
            if row:
                return blob_to_vector(row["embedding"])

        try:
            embedding = await self.provider.embed_query(text)

            if self.cache_enabled:
                self.db.execute(f"""
                    INSERT OR REPLACE INTO {EMBEDDING_CACHE_TABLE}
                    (provider, model, provider_key, hash, embedding, dims, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.provider.id, self.provider.model, self.provider_key,
                    text_hash, vector_to_blob(embedding), len(embedding),
                    int(asyncio.get_event_loop().time()) if self._event_loop else 0
                ))
                self.db.commit()

            return embedding

        except Exception as e:
            logger.error(f"Failed to get embedding: {e}")
            return None

    async def search(
            self,
            query: str,
            opts: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Search memory for relevant content.

        Note: Excludes MEMORY.md and recent (today/yesterday) memory files
        as they are already loaded in the system prompt.
        """
        opts = opts or {}

        if self.settings.sync.get("onSearch", True) and self.dirty:
            try:
                await self.sync(reason="search")
            except Exception as e:
                logger.warning(f"Memory sync failed (search): {e}")

        cleaned = query.strip()
        if not cleaned:
            return []

        min_score = opts.get("minScore") if opts and "minScore" in opts else self.settings.query.get("minScore", 0.7)
        max_results = opts.get("maxResults") if opts and "maxResults" in opts else self.settings.query.get("maxResults",
                                                                                                           10)
        hybrid = self.settings.query.get("hybrid") or {}

        candidates = min(200, max(1, int(max_results * (hybrid.get("candidateMultiplier") or 2.0))))

        keyword_results = []
        if hybrid.get("enabled", True) and self.fts_available:
            try:
                keyword_results = await self._search_keyword(cleaned, candidates)
            except Exception as e:
                logger.debug(f"Keyword search failed: {e}")

        query_vec = await self._embed_query_with_timeout(cleaned)
        has_vector = any(v != 0 for v in query_vec)

        vector_results = []
        if has_vector:
            try:
                vector_results = await self._search_vector(query_vec, candidates)
            except Exception as e:
                logger.debug(f"Vector search failed: {e}")

        if not hybrid.get("enabled", True):
            return [
                r for r in vector_results
                if r["score"] >= min_score
            ][:max_results]

        merged = self._merge_hybrid_results(
            vector_results,
            keyword_results,
            hybrid.get("vectorWeight", 0.7),
            hybrid.get("textWeight", 0.3)
        )

        return [r for r in merged if r["score"] >= min_score][:max_results]

    async def _search_vector(
            self,
            query_vec: List[float],
            limit: int
    ) -> List[Dict[str, Any]]:
        """Search using vector similarity."""
        if not self.vector_enabled:
            return await self._search_vector_fallback(query_vec, limit)

        if not self.vector_dims:
            sample = await self.provider.embed_query("sample")
            self._ensure_vector_table(len(sample))
        else:
            self._ensure_vector_table(self.vector_dims)

        if not self.vector_available:
            return await self._search_vector_fallback(query_vec, limit)

        try:
            import math

            query_blob = vector_to_blob(query_vec)

            source_filter = self._build_source_filter()

            cursor = self.db.execute(f"""
                SELECT rowid, id, path, source, start_line, end_line, text
                FROM chunks
                WHERE {source_filter}
            """)

            chunk_map = {}
            for row in cursor.fetchall():
                chunk_map[row["rowid"]] = {
                    "id": str(row["id"]),
                    "path": str(row["path"]),
                    "source": str(row["source"]),
                    "startLine": int(row["start_line"]),
                    "endLine": int(row["end_line"]),
                    "snippet": str(row["text"][:SNIPPET_MAX_CHARS])
                }

            if not chunk_map:
                return []

            rows = self.db.execute(f"""
                SELECT 
                    rowid,
                    vec_distance_cosine(embedding, vec_f32(?)) as distance
                FROM {VECTOR_TABLE}
                WHERE rowid IN ({','.join('?' * len(chunk_map))})
                ORDER BY distance
                LIMIT ?
            """, (query_blob, *chunk_map.keys(), limit))

            results = []
            for row in rows:
                rowid = row["rowid"]
                if rowid in chunk_map:
                    distance = row["distance"]
                    score = max(0, 1 - distance / 2)

                    result = chunk_map[rowid].copy()
                    result["score"] = score
                    results.append(result)

            return results

        except Exception as e:
            logger.debug(f"Vector search with sqlite-vec failed: {e}")
            return await self._search_vector_fallback(query_vec, limit)

    async def _search_vector_fallback(
            self,
            query_vec: List[float],
            limit: int
    ) -> List[Dict[str, Any]]:
        """Fallback vector search using in-memory cosine similarity."""
        import math

        query_norm = math.sqrt(sum(x * x for x in query_vec))
        if query_norm < 1e-10:
            return []
        query_vec = [x / query_norm for x in query_vec]

        source_filter = self._build_source_filter()

        cursor = self.db.execute(f"""
            SELECT id, path, source, start_line, end_line, text, embedding
            FROM chunks
            WHERE {source_filter} AND embedding IS NOT NULL
        """)

        results = []
        for row in cursor.fetchall():
            if not row["embedding"]:
                continue

            vec = blob_to_vector(row["embedding"])
            if len(vec) != len(query_vec):
                continue

            dot = sum(a * b for a, b in zip(vec, query_vec))
            vec_norm = math.sqrt(sum(x * x for x in vec))
            if vec_norm < 1e-10:
                continue

            similarity = dot / vec_norm

            results.append({
                "id": str(row["id"]),
                "path": str(row["path"]),
                "source": str(row["source"]),
                "startLine": int(row["start_line"]),
                "endLine": int(row["end_line"]),
                "snippet": str(row["text"][:SNIPPET_MAX_CHARS]),
                "score": float(max(0, similarity))
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    async def _search_keyword(
            self,
            query: str,
            limit: int
    ) -> List[Dict[str, Any]]:
        """Search using keyword matching (FTS5)."""
        if not self.fts_available:
            return []

        try:
            fts_query = build_fts_query(query)
            if not fts_query:
                return []

            source_filter = self._build_source_filter()

            cursor = self.db.execute(f"""
                SELECT rowid, id, path, source, start_line, end_line, text
                FROM chunks
                WHERE {source_filter}
            """)

            chunk_map = {}
            for row in cursor.fetchall():
                chunk_map[row["rowid"]] = {
                    "id": str(row["id"]),
                    "path": str(row["path"]),
                    "source": str(row["source"]),
                    "startLine": int(row["start_line"]),
                    "endLine": int(row["end_line"]),
                    "snippet": str(row["text"][:SNIPPET_MAX_CHARS])
                }

            if not chunk_map:
                return []

            rows = self.db.execute(f"""
                SELECT 
                    rowid,
                    rank
                FROM {FTS_TABLE}
                WHERE {FTS_TABLE} MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit))

            results = []
            for row in rows:
                rowid = row["rowid"]
                if rowid in chunk_map:
                    score = bm25_rank_to_score(float(row["rank"]))

                    result = chunk_map[rowid].copy()
                    result["score"] = float(score)
                    results.append(result)

            return results

        except Exception as e:
            logger.debug(f"Keyword search failed: {e}")
            return []

    def _build_source_filter(self) -> str:
        """Build SQL filter for enabled sources."""
        sources = self.settings.sources
        if not sources:
            return "1=0"

        if len(sources) == 1:
            return f"source = '{sources[0]}'"

        return f"source IN ({', '.join(repr(s) for s in sources)})"

    def _merge_hybrid_results(
            self,
            vector_results: List[Dict[str, Any]],
            keyword_results: List[Dict[str, Any]],
            vector_weight: float,
            text_weight: float
    ) -> List[Dict[str, Any]]:
        """Merge and rerank hybrid search results."""
        by_id: Dict[str, Dict[str, Any]] = {}

        for r in vector_results:
            r["_vector_score"] = r["score"]
            r["_text_score"] = 0.0
            by_id[r["id"]] = r

        for r in keyword_results:
            if r["id"] in by_id:
                by_id[r["id"]]["_text_score"] = r["score"]
            else:
                r["_vector_score"] = 0.0
                r["_text_score"] = r["score"]
                by_id[r["id"]] = r

        for r in by_id.values():
            r["score"] = vector_weight * r["_vector_score"] + text_weight * r["_text_score"]
            del r["_vector_score"]
            del r["_text_score"]

        results = list(by_id.values())
        results.sort(key=lambda x: x["score"], reverse=True)

        return results

    async def _embed_query_with_timeout(self, query: str) -> List[float]:
        """Embed query with timeout."""
        try:
            timeout = 60.0
            return await asyncio.wait_for(
                self.provider.embed_query(query),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Embedding query timed out")
            return []
        except Exception as e:
            logger.error(f"Embedding query failed: {e}")
            return []

    async def read_file(
            self,
            rel_path: str,
            from_line: Optional[int] = None,
            lines: Optional[int] = None
    ) -> Dict[str, Any]:
        """Read file content."""
        full_path = os.path.join(self.workspace_dir, rel_path)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"File not found: {rel_path}")

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total_lines = len(all_lines)

        if from_line is not None:
            start = max(0, from_line - 1)
            end = total_lines
            if lines is not None:
                end = min(total_lines, start + lines)
            content_lines = all_lines[start:end]
        else:
            content_lines = all_lines

        return {
            "path": rel_path,
            "text": "".join(content_lines),
            "totalLines": total_lines,
            "fromLine": from_line or 1,
            "toLine": (from_line or 1) + len(content_lines) - 1
        }

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        """Write metadata to database."""
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (META_KEY, json.dumps(meta))
        )
        self.db.commit()

    def status(self) -> Dict[str, Any]:
        """Get memory system status."""
        if not self.db:
            return {"available": False}

        cursor = self.db.execute("SELECT COUNT(*) as count FROM files")
        file_count = cursor.fetchone()["count"]

        cursor = self.db.execute("SELECT COUNT(*) as count FROM chunks")
        chunk_count = cursor.fetchone()["count"]

        cursor = self.db.execute("""
                                 SELECT source, COUNT(*) as files
                                 FROM files
                                 GROUP BY source
                                 """)
        source_counts = [
            {"source": str(row["source"]), "files": int(row["files"])}
            for row in cursor.fetchall()
        ]

        return {
            "available": True,
            "provider": self.provider.id if self.provider else None,
            "model": self.provider.model if self.provider else None,
            "files": int(file_count),
            "chunks": int(chunk_count),
            "sourceCounts": source_counts,
            "dirty": self.dirty,
            "fts": {
                "enabled": self.fts_enabled,
                "available": self.fts_available,
                "error": self.fts_error
            },
            "vector": {
                "enabled": self.vector_enabled,
                "available": self.vector_available,
                "error": self.vector_error,
                "dims": self.vector_dims
            },
            "cache": {
                "enabled": self.cache_enabled,
                "entries": int(self._get_cache_entry_count())
            }
        }

    def _get_cache_entry_count(self) -> int:
        """Get number of cache entries."""
        try:
            cursor = self.db.execute(f"SELECT COUNT(*) as count FROM {EMBEDDING_CACHE_TABLE}")
            return cursor.fetchone()["count"]
        except:
            return 0

    async def close(self) -> None:
        """Close the memory manager."""
        if self.closed:
            return

        self.closed = True

        if self._interval_timer:
            self._interval_timer.cancel()
        if self._watch_timer:
            self._watch_timer.cancel()
        if self._session_timer:
            self._session_timer.cancel()

        if self._file_observer:
            try:
                self._file_observer.stop()
                self._file_observer.join()
            except:
                pass

        if self.db:
            self.db.close()

        cache_key = f"{self.agent_id}:{self.workspace_dir}"
        if cache_key in INDEX_CACHE:
            del INDEX_CACHE[cache_key]

        logger.info("Memory manager closed")


def clear_memory_manager_cache() -> None:
    """清除 memory manager 缓存，使下次 get_memory_manager 使用最新配置（如 embed_api_base 等）创建新实例。"""
    INDEX_CACHE.clear()


async def get_memory_manager(
        agent_id: str = "default",
        workspace_dir: str = ".",
        settings: Optional[MemorySettings] = None
) -> Optional[MemoryIndexManager]:
    """Get or create memory manager."""
    settings = settings or MemorySettings()
    return await MemoryIndexManager.get(agent_id, workspace_dir, settings)
