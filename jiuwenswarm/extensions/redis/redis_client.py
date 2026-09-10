# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway 分布式模式：Redis 异步客户端封装（设计文档 §3.3.3）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import ConnectionPool, Redis

logger = logging.getLogger(__name__)


def _require_redis_async() -> Any:
    """延迟导入：默认安装不包含 redis 包时仍可 import 本模块。"""
    try:
        import redis.asyncio as redis_async  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "分布式网关需要 Redis 异步客户端。请安装可选依赖：pip install 'jiuwenswarm[redis]' "
            "或 pip install 'redis>=5.0.0'"
        ) from e
    return redis_async


def _coerce_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _coerce_float(val: Any, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@dataclass
class RedisConfig:
    """与 config 中 ``redis:`` 段及 §3.2.1 对齐。"""

    mode: str = "standalone"                                  # standalone | cluster
    startup_nodes: list[dict] = field(default_factory=list)   # [{host, port}, ...]; cluster 启动节点
    host: str = "localhost"
    port: int = 6379
    password: str | None = None
    db: int = 0
    key_prefix: str = "jiuwenswarm:"
    pool_size: int = 10
    connect_timeout: float = 5.0
    operation_timeout: float = 10.0
    health_check_interval: int = 30

    @staticmethod
    def _normalize_password(value: Any) -> str | None:
        """处理 YAML 解析后的密码值（false/0 等应视为无密码）。"""
        if value is None or value is False:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return str(value)

    @staticmethod
    def _parse_startup_nodes(value: Any, default_port: int = 6379) -> list[dict]:
        """解析 'host[:port]' 或逗号分隔列表；未带端口用 default_port。"""
        nodes: list[dict] = []
        for item in str(value or "").split(","):
            host, _, port = item.partition(":")
            host = host.strip()
            if not host:
                continue
            nodes.append({"host": host, "port": _coerce_int(port.strip() or str(default_port), default_port)})
        return nodes

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> RedisConfig:
        m = data or {}
        kp = str(m.get("key_prefix") if m.get("key_prefix") is not None else "jiuwenswarm:")
        if kp and not kp.endswith(":"):
            kp = f"{kp}:"
        pw = cls._normalize_password(m.get("password"))
        mode = str(m.get("mode") or "standalone").strip().lower()
        if mode not in ("standalone", "cluster"):
            mode = "standalone"
        # host 字段承载地址：单机 host/host:port 或集群逗号列表；未带端口用 port(REDIS_PORT)
        fallback_port = _coerce_int(m.get("port"), 6379)
        node_list = cls._parse_startup_nodes(m.get("host"), default_port=fallback_port)
        if node_list:
            host = node_list[0]["host"]
            port = node_list[0]["port"]
            startup_nodes = node_list if mode == "cluster" else []
        else:
            host = "localhost"
            port = fallback_port
            startup_nodes = []
        return cls(
            mode=mode,
            startup_nodes=startup_nodes,
            host=host,
            port=port,
            password=pw,
            db=_coerce_int(m.get("db"), 0),
            key_prefix=kp,
            pool_size=max(1, _coerce_int(m.get("pool_size"), 10)),
            connect_timeout=_coerce_float(m.get("connect_timeout"), 5.0),
            operation_timeout=_coerce_float(m.get("operation_timeout"), 10.0),
            health_check_interval=max(1, _coerce_int(m.get("health_check_interval"), 30)),
        )

    def effective_key(self, key: str) -> str:
        """为 key / 频道名添加 ``key_prefix``（已含前缀则不再添加）。"""
        k = key or ""
        p = self.key_prefix
        if not p:
            return k
        if k.startswith(p):
            return k
        return f"{p}{k}"


class RedisClient:
    """§3.3.3 接口：连接池 + KV/Hash + Pub/Sub + ping + close。

    ``mode="cluster"`` 时 ``open()`` 用 ``redis.asyncio.RedisCluster`` 替代单机连接池，
    其余接口不变；``mget()`` 在 CROSSSLOT 异常时自动回退逐 key get。
    """

    def __init__(self, cfg: RedisConfig) -> None:
        self._cfg = cfg
        self._pool: ConnectionPool | None = None
        self._redis: Redis | None = None

    @property
    def config(self) -> RedisConfig:
        return self._cfg

    async def open(self) -> None:
        if self._redis is not None:
            return
        redis = _require_redis_async()
        if self._cfg.mode == "cluster":
            from redis.asyncio.cluster import RedisCluster
            from redis.cluster import ClusterNode
            if not self._cfg.startup_nodes:
                logger.warning(
                    "[RedisClient] cluster 模式未配置 startup_nodes,回退单节点 %s:%s"
                    "(若该节点非集群成员将连接失败)",
                    self._cfg.host, self._cfg.port,
                )
            nodes = [ClusterNode(n["host"], int(n["port"])) for n in self._cfg.startup_nodes] \
                or [ClusterNode(self._cfg.host, self._cfg.port)]
            self._redis = RedisCluster(
                startup_nodes=nodes,
                password=self._cfg.password,
                decode_responses=True,
                socket_connect_timeout=self._cfg.connect_timeout,
                socket_timeout=self._cfg.operation_timeout,
                health_check_interval=self._cfg.health_check_interval,
                max_connections=self._cfg.pool_size,
            )
            return
        self._pool = redis.ConnectionPool(
            host=self._cfg.host,
            port=self._cfg.port,
            username=None,
            password=self._cfg.password,
            db=self._cfg.db,
            decode_responses=True,
            max_connections=self._cfg.pool_size,
            socket_connect_timeout=self._cfg.connect_timeout,
            socket_timeout=self._cfg.operation_timeout,
        )
        self._redis = redis.Redis(connection_pool=self._pool)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RedisClient] aclose: %s", exc)
            self._redis = None
        if self._pool is not None:
            try:
                await self._pool.disconnect(inuse_connections=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RedisClient] pool disconnect: %s", exc)
            self._pool = None

    def _connection(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("RedisClient is not open")
        return self._redis

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RedisClient] ping failed: %s", exc)
            return False

    async def get(self, key: str) -> str | None:
        r = self._connection()
        return await r.get(self._cfg.effective_key(key))

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> bool:
        r = self._connection()
        v = value if isinstance(value, str) else str(value)
        if ttl_seconds is not None and ttl_seconds > 0:
            return bool(await r.set(self._cfg.effective_key(key), v, ex=int(ttl_seconds)))
        return bool(await r.set(self._cfg.effective_key(key), v))

    async def set_nx(self, key: str, value: Any, ttl_seconds: int | None = None) -> bool:
        """SET key NX [EX ttl]；成功返回 True。"""
        r = self._connection()
        v = value if isinstance(value, str) else str(value)
        kwargs: dict[str, Any] = {"nx": True}
        if ttl_seconds is not None and ttl_seconds > 0:
            kwargs["ex"] = int(ttl_seconds)
        return bool(await r.set(self._cfg.effective_key(key), v, **kwargs))

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        r = self._connection()
        return bool(await r.expire(self._cfg.effective_key(key), int(ttl_seconds)))

    async def delete(self, key: str) -> bool:
        r = self._connection()
        n = int(await r.delete(self._cfg.effective_key(key)))
        return n > 0

    async def mget(self, keys: list[str]) -> list[str | None]:
        r = self._connection()
        if not keys:
            return []
        full = [self._cfg.effective_key(k) for k in keys]
        try:
            vals = await r.mget(*full)
        except Exception as exc:
            # Cluster 跨 slot 抛 CROSSSLOT -> 回退逐个 get；其余异常（连接断开等）直接抛出
            if "CROSSSLOT" not in str(exc).upper():
                raise
            logger.warning("[RedisClient] mget CROSSSLOT fallback to per-key get: %s", exc)
            return await asyncio.gather(*(r.get(k) for k in full))
        out: list[str | None] = []
        for v in vals or []:
            if v is None:
                out.append(None)
            elif isinstance(v, str):
                out.append(v)
            else:
                out.append(str(v))
        return out

    async def scan_iter(self, match: str) -> list[str]:
        """扫描匹配相对 key 模式的键，返回去掉 ``key_prefix`` 后的相对名列表。"""
        r = self._connection()
        pattern = self._cfg.effective_key(match)
        prefix = self._cfg.key_prefix or ""
        keys: list[str] = []
        async for raw in r.scan_iter(match=pattern):
            k = raw if isinstance(raw, str) else str(raw)
            if prefix and k.startswith(prefix):
                keys.append(k[len(prefix):])
            else:
                keys.append(k)
        return keys

    async def scan_keys(self, pattern: str) -> list[str]:
        """``scan_iter`` 的别名（SessionMap / LeaderElection 故障切换用）。"""
        return await self.scan_iter(pattern)

    async def hget(self, key: str, hash_field: str) -> str | None:
        r = self._connection()
        return await r.hget(self._cfg.effective_key(key), hash_field)

    async def hset(self, key: str, hash_field: str, value: Any) -> bool:
        r = self._connection()
        v = value if isinstance(value, str) else str(value)
        await r.hset(self._cfg.effective_key(key), hash_field, v)
        return True

    async def hgetall(self, key: str) -> dict[str, Any]:
        r = self._connection()
        raw = await r.hgetall(self._cfg.effective_key(key))
        return dict(raw) if raw else {}

    async def hdel(self, key: str, hash_field: str) -> bool:
        r = self._connection()
        n = int(await r.hdel(self._cfg.effective_key(key), hash_field))
        return n > 0

    def _ensure_pubsub_capable(self, r: Any) -> None:
        """cluster 模式下较低版本 redis-py 的异步 RedisCluster 没有 publish/pubsub。

        当前仓库内尚无调用方，属前向防御：提前给出明确报错而不是深埋一个
        AttributeError，同时提示升级 redis-py。
        """
        if getattr(self._cfg, "mode", "standalone") == "cluster" and not (
            hasattr(r, "publish") and hasattr(r, "pubsub")
        ):
            raise RuntimeError(
                "redis cluster 模式要求支持 pubsub 的 redis-py 版本（>=8.0）；"
                "当前异步 RedisCluster 客户端缺少 publish/pubsub 能力"
            )

    async def publish(self, channel: str, message: str) -> int:
        r = self._connection()
        self._ensure_pubsub_capable(r)
        return int(await r.publish(self._cfg.effective_key(channel), message))

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        """订阅频道，仅 yield ``type==message`` 的字符串载荷。"""
        r = self._connection()
        self._ensure_pubsub_capable(r)
        pubsub = r.pubsub()
        ch = self._cfg.effective_key(channel)
        await pubsub.subscribe(ch)
        try:
            async for msg in pubsub.listen():
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                yield data if isinstance(data, str) else str(data)
        finally:
            try:
                await pubsub.unsubscribe(ch)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RedisClient] unsubscribe: %s", exc)
            try:
                await pubsub.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RedisClient] pubsub aclose: %s", exc)
