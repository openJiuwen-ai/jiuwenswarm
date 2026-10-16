"""SecretStore-backed credential references for registered outbound agents."""

from __future__ import annotations

import re
from typing import Protocol

from jiuwenswarm.common.secrets import SecretStore


_CREDENTIAL_PREFIX = "a2a/outbound/"
_CREDENTIAL_REF_RE = re.compile(r"^a2a/outbound/([^/\\\r\n]+)\.api_key$")


class SecretStoreLike(Protocol):
    def get(self, key: str) -> str:
        raise NotImplementedError

    def set(
        self, key: str, value: str, *, algorithm: str | None = None
    ) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class A2AOutboundCredentialStore:
    """Own credential values while domain records retain only ``credential_ref``."""

    def __init__(self, secret_store: SecretStoreLike | None = None) -> None:
        self._store = secret_store or SecretStore.get_instance()

    @staticmethod
    def reference_for(agent_id: str) -> str:
        normalized = str(agent_id or "").strip()
        if not normalized or any(char in normalized for char in "\\/\r\n"):
            raise ValueError("invalid A2A outbound agent_id")
        return f"{_CREDENTIAL_PREFIX}{normalized}.api_key"

    @staticmethod
    def validate_reference(credential_ref: str) -> str:
        normalized = str(credential_ref or "").strip()
        if _CREDENTIAL_REF_RE.fullmatch(normalized) is None:
            raise ValueError("invalid A2A outbound credential_ref")
        return normalized

    @classmethod
    def validate_for_agent(cls, agent_id: str, credential_ref: str) -> str:
        normalized = cls.validate_reference(credential_ref)
        if normalized != cls.reference_for(agent_id):
            raise ValueError("credential_ref does not belong to A2A outbound agent")
        return normalized

    def set_for_agent(
        self,
        agent_id: str,
        secret: str,
        *,
        algorithm: str | None = None,
    ) -> str:
        credential_ref = self.reference_for(agent_id)
        self._store.set(credential_ref, str(secret), algorithm=algorithm)
        return credential_ref

    def get(self, credential_ref: str | None) -> str:
        if not credential_ref:
            return ""
        return self._store.get(self.validate_reference(credential_ref))

    def delete(self, credential_ref: str | None) -> None:
        if credential_ref:
            self._store.delete(self.validate_reference(credential_ref))


__all__ = ["A2AOutboundCredentialStore", "SecretStoreLike"]
