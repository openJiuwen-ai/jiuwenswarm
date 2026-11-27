from abc import abstractmethod

from jiuwenclaw.extensions.sdk.base import BaseExtension
from jiuwenclaw.security.base_crypto import CryptoProvider


class CryptoUtility(BaseExtension):
    """扩展入口：持有真正的加解密实现，通过 `get_crypto()` 暴露。"""

    @abstractmethod
    def get_crypto(self) -> CryptoProvider:
        """返回实际执行 encrypt/decrypt 的实例。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭"""
        pass
