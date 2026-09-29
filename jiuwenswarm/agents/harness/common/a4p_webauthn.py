# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm adapter around the A4P SDK WebAuthn primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from a4p import (
    JsonFileCredentialStore,
    StaticA4PServerTrustStore,
    UserAuthorizationRequest,
    approve_user_mandate,
    sign_user_mandate_with_signer,
    verify_local_user_authorization_request,
)
from a4p.user_signature.webauthn import (
    WebAuthnSignatureMethod,
    WebAuthnUserSigner,
)


WEBAUTHN_CREDENTIAL_STORE_FILENAME = "webauthn_credentials.json"
WEBAUTHN_RP_ID = "localhost"
WEBAUTHN_RP_NAME = "JiuwenSwarm"
WEBAUTHN_EXPECTED_ORIGIN = "http://localhost:5173"


class A4PWebAuthnAdapter:
    """Own WebAuthn credentials and translate browser assertions for A4P."""

    def __init__(
        self,
        credential_store_path: str | Path,
        *,
        require_user_signature: bool,
    ) -> None:
        self.credential_store_path = Path(credential_store_path)
        self.require_user_signature = bool(require_user_signature)
        self.credential_store = JsonFileCredentialStore(self.credential_store_path)
        self.signature_method = WebAuthnSignatureMethod(
            self.credential_store,
            rp_id=WEBAUTHN_RP_ID,
            rp_name=WEBAUTHN_RP_NAME,
            expected_origin=WEBAUTHN_EXPECTED_ORIGIN,
        )
        self.user_signer = WebAuthnUserSigner()
        self._trust_store: StaticA4PServerTrustStore | None = None

    def configure_server_trust(self, trust_config: dict[str, Any]) -> None:
        self._trust_store = StaticA4PServerTrustStore(trust_config)

    def prepare_authorization_request(
        self,
        *,
        mandate: dict[str, Any],
        signing_options: dict[str, Any],
    ) -> dict[str, Any]:
        if self._trust_store is None:
            raise RuntimeError("A4P Server trust is not configured")
        return verify_local_user_authorization_request(
            UserAuthorizationRequest(
                mandate=mandate,
                signingOptions=signing_options,
            ),
            trust_store=self._trust_store,
            expected_signature_method=(
                self.user_signer.signature_method
                if self.require_user_signature
                else None
            ),
        )

    def approve_mandate(
        self,
        mandate: dict[str, Any],
        assertion: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not self.require_user_signature:
            return approve_user_mandate(mandate)
        if not isinstance(assertion, dict):
            raise ValueError("WebAuthn assertion missing")
        return sign_user_mandate_with_signer(
            mandate,
            user_signer=self.user_signer,
            signing_input={"assertion": assertion},
        )

    def credential_summaries(self, user_id: str) -> list[dict[str, Any]]:
        records = self.credential_store.list_for_user(user_id)
        summaries: list[dict[str, Any]] = []
        for record in records:
            details = record.details if isinstance(record.details, dict) else {}
            summaries.append(
                {
                    "credentialId": record.credentialId,
                    "createdAt": record.createdAt,
                    "signatureMethod": record.signatureMethod,
                    "publicKeyFormat": str(record.publicKey.get("format") or ""),
                    "signCount": int(details.get("signCount") or 0),
                    "transports": list(details.get("transports") or []),
                    "credentialDeviceType": str(
                        details.get("credentialDeviceType") or ""
                    ),
                    "credentialBackedUp": bool(
                        details.get("credentialBackedUp", False)
                    ),
                }
            )
        return summaries

    def registered_credential_summary(
        self,
        credential_id: str,
    ) -> dict[str, Any] | None:
        record = self.credential_store.get(credential_id)
        if record is None:
            return None
        return next(
            (
                item
                for item in self.credential_summaries(record.userId)
                if item["credentialId"] == credential_id
            ),
            None,
        )


__all__ = [
    "A4PWebAuthnAdapter",
    "WEBAUTHN_CREDENTIAL_STORE_FILENAME",
    "WEBAUTHN_EXPECTED_ORIGIN",
    "WEBAUTHN_RP_ID",
    "WEBAUTHN_RP_NAME",
]
