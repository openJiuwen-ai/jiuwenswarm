from typing import Protocol, runtime_checkable, Optional


@runtime_checkable
class CryptoProvider(Protocol):
    def encrypt(self, plaintext: str, **kwargs) -> str:
        pass

    def decrypt(self, ciphertext: str, **kwargs) -> str:
        pass


_default_provider: CryptoProvider = None


def set_crypto_provider(provider: CryptoProvider) -> None:
    global _default_provider
    _default_provider = provider


def get_crypto_provider() -> Optional[CryptoProvider]:
    return _default_provider