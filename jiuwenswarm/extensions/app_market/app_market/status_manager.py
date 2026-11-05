from __future__ import annotations

import asyncio
import json
import logging

from .models import InstanceStatus

logger = logging.getLogger(__name__)

# 可选依赖：httpx
try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


class StatusManager:
    """状态协议处理器 — 支持 push 和 pull 两种状态接入方式"""

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(__name__)
        # {instance_id: (state, seq, detail)}
        self._states: dict[str, tuple[str, int, str]] = {}

    def handle_push(self, instance_id: str, token: str, status: InstanceStatus) -> bool:
        """处理应用主动推送的状态更新"""
        try:
            # 校验 instance_id 非空
            if not instance_id or not instance_id.strip():
                self._logger.warning("push 状态更新被拒绝：instance_id 为空")
                return False
            # 校验 token 非空
            if not token or not token.strip():
                self._logger.warning("push 状态更新被拒绝：token 为空")
                return False

            current = self._states.get(instance_id)
            if current is not None:
                current_state, current_seq, _ = current
                # 校验 seq：旧消息不覆盖新状态
                if status.seq <= current_seq:
                    self._logger.debug(
                        "忽略旧状态推送 instance_id=%s, seq=%d <= current=%d",
                        instance_id,
                        status.seq,
                        current_seq,
                    )
                    return False
                # 停止开始后不接受迟到的 "running"
                if current_state == "stopping" and status.state == "running":
                    self._logger.debug(
                        "忽略迟到的 running 状态 instance_id=%s", instance_id
                    )
                    return False

            # 更新状态
            self._states[instance_id] = (status.state, status.seq, status.detail)
            self._logger.info(
                "状态已更新 instance_id=%s, state=%s, seq=%d",
                instance_id,
                status.state,
                status.seq,
            )
            return True

        except Exception as e:
            self._logger.error("处理 push 状态失败 instance_id=%s: %s", instance_id, e)
            return False

    async def poll_pull(self, port: int, token: str) -> InstanceStatus | None:
        """通过 HTTP 拉取应用状态（pull 模式）"""
        url = f"http://127.0.0.1:{port}/_workswarm/status"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            if _HAS_HTTPX:
                # 优先使用 httpx
                async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code != 200:
                        self._logger.warning(
                            "拉取状态失败 port=%d, status=%d", port, resp.status_code
                        )
                        return None
                    data = resp.json()
                    return InstanceStatus(**data)
            else:
                # 退回到 urllib（同步调用，包装在线程中）
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, self._pull_with_urllib, url, headers
                )
                return result

        except Exception as e:
            self._logger.debug("拉取状态失败 port=%d: %s", port, e)
            return None

    async def request_shutdown(self, port: int, token: str) -> bool:
        """请求应用通过 /_workswarm/shutdown 自行清理并退出。

        返回 True 仅表示应用接受了停止请求；是否退出仍需由调用方
        继续观察进程存活。应用未实现该端点（404/超时/连接失败）时
        返回 False，调用方应回退到强制终止。
        """
        url = f"http://127.0.0.1:{port}/_workswarm/shutdown"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            if _HAS_HTTPX:
                async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                    resp = await client.post(url, headers=headers)
                    return resp.status_code == 200
            # 退回到 urllib（同步调用，包装在线程中）
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._shutdown_with_urllib, url, headers)
        except Exception as e:
            self._logger.debug("请求应用停止失败 port=%d: %s", port, e)
            return False

    @staticmethod
    def _shutdown_with_urllib(
        url: str, headers: dict[str, str]
    ) -> bool:
        """使用 urllib 同步请求应用停止"""
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(url, headers=headers, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _pull_with_urllib(
        url: str, headers: dict[str, str]
    ) -> InstanceStatus | None:
        """使用 urllib 同步拉取状态"""
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return InstanceStatus(**data)
        except Exception:
            return None

    def get_state(self, instance_id: str) -> str:
        """获取实例当前状态，不存在返回空字符串"""
        entry = self._states.get(instance_id)
        return entry[0] if entry else ""

    def update_state(self, instance_id: str, state: str, detail: str = "") -> None:
        """更新实例状态，state 变化时 seq +1"""
        try:
            current = self._states.get(instance_id)
            if current is not None:
                old_state, old_seq, _ = current
                if state != old_state:
                    # 状态变化，seq 递增
                    new_seq = old_seq + 1
                else:
                    new_seq = old_seq
                self._states[instance_id] = (state, new_seq, detail)
            else:
                # 新实例
                self._states[instance_id] = (state, 0, detail)
            self._logger.debug(
                "更新状态 instance_id=%s, state=%s", instance_id, state
            )
        except Exception as e:
            self._logger.error("更新状态失败 instance_id=%s: %s", instance_id, e)

    def get_seq(self, instance_id: str) -> int:
        """获取实例当前 seq，不存在返回 0"""
        entry = self._states.get(instance_id)
        return entry[1] if entry else 0

    def validate_seq(self, instance_id: str, seq: int) -> bool:
        """检查 seq 是否大于当前记录的 seq"""
        current_seq = self.get_seq(instance_id)
        return seq > current_seq
