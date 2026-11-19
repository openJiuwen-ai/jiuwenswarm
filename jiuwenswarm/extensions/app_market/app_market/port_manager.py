from __future__ import annotations

import logging
import socket
import threading

logger = logging.getLogger(__name__)


class PortManager:
    """端口分配与冲突检测管理器"""

    def __init__(self, start_port: int = 8080, end_port: int = 9099):
        self._start_port = start_port
        self._end_port = end_port
        self._allocated: dict[int, str] = {}  # {port: instance_id}
        self._lock = threading.Lock()

    def allocate(self, instance_id: str, default_port: int = 0) -> int:
        """为实例分配端口，优先使用 default_port（如果可用）"""
        with self._lock:
            # 优先尝试 default_port
            if default_port > 0 and default_port not in self._allocated and self.is_available(default_port):
                self._allocated[default_port] = instance_id
                logger.info("为实例 %s 分配默认端口 %d", instance_id, default_port)
                return default_port

            # 在范围内查找可用端口
            for port in range(self._start_port, self._end_port + 1):
                if port not in self._allocated and self.is_available(port):
                    self._allocated[port] = instance_id
                    logger.info("为实例 %s 分配端口 %d", instance_id, port)
                    return port

            logger.error("没有可用端口可分配给实例 %s", instance_id)
            return 0

    def release(self, port: int, instance_id: str) -> None:
        """只释放属于该实例的端口"""
        with self._lock:
            if self._allocated.get(port) == instance_id:
                del self._allocated[port]
                logger.info("释放端口 %d", port)

    @staticmethod
    def is_available(port: int) -> bool:
        """检测端口是否可用（通过 socket bind 探测）"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    # POSIX: 对齐 ThreadingHTTPServer 的 allow_reuse_address。
                    # 状态轮询由服务端关闭的连接会进入 TIME_WAIT，不带
                    # SO_REUSEADDR 的探测会比真实服务器更严格，导致刚释放
                    # 的默认端口在重试时被跳过。
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def is_allocated(self, port: int) -> bool:
        """检查端口是否已被本管理器分配"""
        with self._lock:
            return port in self._allocated
