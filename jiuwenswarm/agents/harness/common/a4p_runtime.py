# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A4P runtime bridge for JiuwenSwarm tool permissions."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from a4p import A4PServer
from a4p.errors import A4PProtocolError
from a4p.types import (
    IntentAuthorizationResponse,
    VerificationResult,
    to_payload,
)
from jiuwenswarm.agents.harness.common.a4p_authorizer import (
    WebAuthorizationRequest,
    WebAuthorizerBroker,
)
from jiuwenswarm.agents.harness.common.a4p_cron_token_store import (
    CRON_INTENT_TOKENS_DB_FILENAME,
    CRON_INTENT_TOKENS_VERSION,
    SQLiteCronIntentTokenStore,
)
from jiuwenswarm.agents.harness.common.a4p_display import (
    INTENT_DISPLAY_CONTEXT,
    IntentDisplayContext,
    format_intent_display_text,
    preferred_display_language,
)
from jiuwenswarm.agents.harness.common.a4p_execution_context import AuthorizerRoute
from jiuwenswarm.agents.harness.common.a4p_token_expiry import token_expired
from jiuwenswarm.agents.harness.common.a4p_webauthn import (
    A4PWebAuthnAdapter,
    WEBAUTHN_CREDENTIAL_STORE_FILENAME,
    WEBAUTHN_EXPECTED_ORIGIN,
    WEBAUTHN_RP_ID,
    WEBAUTHN_RP_NAME,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_config_dir, logger


INTERNAL_A4P_USER_ID = "jiuwenswarm-local-user"
DEFAULT_INTENT_VALIDITY_SECONDS = 3600
DEFAULT_CRON_INTENT_VALIDITY_SECONDS = 30 * 24 * 60 * 60
DEFAULT_AUTHORIZATION_TIMEOUT_SECONDS = 300

_QUOTED_SHELL_VALUE_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)")
_SHELL_COMMAND_PARAM_KEYS = {
    "bash": "command",
    "mcp_exec_command": "command",
    "create_terminal": "cmd",
}


@dataclass(frozen=True)
class A4PIdentity:
    agent_id: str


def get_a4p_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else get_config()
    a4p_cfg = cfg.get("a4p") if isinstance(cfg, dict) else None
    return dict(a4p_cfg) if isinstance(a4p_cfg, dict) else {}


def is_a4p_enabled(config: dict[str, Any] | None = None) -> bool:
    return bool(get_a4p_config(config).get("enabled", False))


def resolve_a4p_identity(
    *,
    metadata: dict[str, Any] | None = None,
    agent_name: str | None = None,
) -> A4PIdentity:
    meta = metadata if isinstance(metadata, dict) else {}
    agent_id = (
        str(meta.get("agent_id") or "").strip()
        or str(agent_name or "").strip()
        or "jiuwenswarm"
    )
    return A4PIdentity(agent_id=agent_id)


def _agent_subject_id(agent_id: str) -> str:
    value = str(agent_id or "").strip()
    if value.startswith("agent:"):
        return value
    return f"agent:{value or 'jiuwenswarm'}"


def _normalize_shell_command_scope(command: str) -> str:
    """Remove quotes only when they wrap a shell-safe absolute path literal."""

    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        if not value.startswith("/"):
            return match.group(0)
        if all(char.isalnum() or char in "/._:@%+=,-" for char in value):
            return value
        return match.group(0)

    return _QUOTED_SHELL_VALUE_RE.sub(replace, command)


def normalize_a4p_action_params(action: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Canonicalize integration-owned action params before A4P matching."""
    normalized = dict(params or {})
    command_key = _SHELL_COMMAND_PARAM_KEYS.get(str(action or "").strip())
    command = normalized.get(command_key) if command_key else None
    if command_key and isinstance(command, str):
        normalized[command_key] = _normalize_shell_command_scope(command)
    return normalized


class A4PRuntime:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = get_a4p_config(config)
        self._session_tokens: dict[str, list[dict[str, Any]]] = {}
        self._webauthn_registration_routes: dict[
            str,
            tuple[str, str, str, str],
        ] = {}
        self._cron_tokens_path = (
            get_config_dir() / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME
        )
        self._webauthn_credential_store_path = (
            get_config_dir() / "a4p" / WEBAUTHN_CREDENTIAL_STORE_FILENAME
        )
        self.require_user_signature = bool(
            self.config.get("require_user_signature", False)
        )
        self.cron_tokens = SQLiteCronIntentTokenStore(self._cron_tokens_path)
        self.cron_tokens.prune_expired()
        self.webauthn = A4PWebAuthnAdapter(
            self._webauthn_credential_store_path,
            require_user_signature=self.require_user_signature,
        )
        self.server = A4PServer(
            server_id=str(self.config.get("server_id") or "local://jiuwenswarm-a4p"),
            user_signature_method=self.webauthn.signature_method,
            intent_display_text_renderer=format_intent_display_text,
            require_user_signature=self.require_user_signature,
        )
        self.webauthn.configure_server_trust(self.server.server_trust_config())
        self.authorizer = WebAuthorizerBroker(
            timeout_seconds=DEFAULT_AUTHORIZATION_TIMEOUT_SECONDS,
            approve_mandate=self.webauthn.approve_mandate,
        )

    async def request_intent_authorization(
        self,
        *,
        session_id: str,
        channel_id: str,
        identity: A4PIdentity,
        actions: list[dict[str, Any]],
        cron_job_id: str | None = None,
        cron_authorization_target: dict[str, Any] | None = None,
        validity_seconds: int | None = None,
        reason: str | None = None,
        authorizer_route: AuthorizerRoute,
    ) -> dict[str, Any]:
        intent: dict[str, Any] = {"actions": actions}
        cron_id = str(cron_job_id or "").strip()
        validity = validity_seconds if validity_seconds is not None else (
            DEFAULT_CRON_INTENT_VALIDITY_SECONDS
            if cron_id
            else DEFAULT_INTENT_VALIDITY_SECONDS
        )
        metadata: dict[str, Any] = {
            "sessionId": session_id,
            "channelId": channel_id,
            "reason": reason or "",
        }
        if cron_id:
            metadata["cronJobId"] = cron_id
            metadata["authorizationTarget"] = dict(cron_authorization_target or {})

        display_context_token = INTENT_DISPLAY_CONTEXT.set(
            IntentDisplayContext(
                language=preferred_display_language(),
                cron_target=deepcopy(cron_authorization_target or {}) if cron_id else None,
                reason=str(reason or "").strip(),
            )
        )
        request_payload = {
            "agentId": identity.agent_id,
            "userId": INTERNAL_A4P_USER_ID,
            "intent": intent,
            "validitySeconds": validity,
            "metadata": metadata,
        }
        try:
            try:
                prepared = await self.server.prepare_intent_authorization(
                    request_payload
                )
            except A4PProtocolError as exc:
                reason = str(exc)
                if exc.code == "USER_CREDENTIAL_NOT_REGISTERED":
                    reason = (
                        "No Passkey is registered for A4P authorization. "
                        "Register one in the Web A4P security settings."
                    )
                return {
                    "approved": False,
                    "rejectReason": reason,
                    "verificationResult": {
                        "valid": False,
                        "reason": reason,
                        "code": exc.code,
                    },
                }
        finally:
            INTENT_DISPLAY_CONTEXT.reset(display_context_token)

        if prepared.mandate is None:
            return to_payload(prepared)

        request_id = str(prepared.mandate.get("mandateId") or "").strip()
        if not request_id:
            reason = "A4P prepared mandateId missing"
            return to_payload(
                IntentAuthorizationResponse(
                    mandate=prepared.mandate,
                    approved=False,
                    rejectReason=reason,
                    verificationResult=VerificationResult.fail(
                        reason,
                        "MANDATE_INVALID",
                    ),
                )
            )
        try:
            signing_options = self.webauthn.prepare_authorization_request(
                mandate=prepared.mandate,
                signing_options=dict(prepared.signingOptions or {}),
            )
        except ValueError as exc:
            reason = str(exc)
            return to_payload(
                IntentAuthorizationResponse(
                    mandate=prepared.mandate,
                    approved=False,
                    rejectReason=reason,
                    verificationResult=VerificationResult.fail(
                        reason,
                        getattr(exc, "code", "AUTHORIZATION_INVALID"),
                    ),
                )
            )
        authorization = await self.authorizer.request_authorization(
            WebAuthorizationRequest(
                request_id=request_id,
                mandate=prepared.mandate,
                signing_options=signing_options,
                ui_context={
                    "kind": "intent",
                    "userId": INTERNAL_A4P_USER_ID,
                    "agentId": identity.agent_id,
                    **metadata,
                },
                route=authorizer_route,
            )
        )
        signed_mandate = (
            authorization.signedMandate
            if isinstance(authorization.signedMandate, dict)
            else None
        )
        if not authorization.approved or signed_mandate is None:
            reason = authorization.rejectReason or "Authorization rejected"
            response = IntentAuthorizationResponse(
                mandate=prepared.mandate,
                approved=False,
                rejectReason=reason,
                verificationResult=VerificationResult.fail(
                    reason,
                    "AUTHORIZATION_REJECTED",
                ),
            )
        else:
            response = await self.server.complete_intent_authorization(
                {
                    "signedMandate": signed_mandate,
                }
            )
        payload = to_payload(response)
        payload["requestId"] = request_id
        token = payload.get("intentToken") if isinstance(payload, dict) else None
        if response.approved and isinstance(token, dict):
            if cron_id:
                self.store_cron_intent_token(cron_id, token)
            else:
                self.store_intent_token(session_id, token)
        return payload

    def store_intent_token(self, session_id: str, token: dict[str, Any]) -> None:
        if not session_id or not isinstance(token, dict) or token_expired(token):
            return
        tokens = self._session_tokens.setdefault(session_id, [])
        token_id = str(token.get("tokenId") or "")
        self._session_tokens[session_id] = [
            item for item in tokens if str(item.get("tokenId") or "") != token_id
        ] + [dict(token)]

    def _prune_session_tokens(self, session_id: str) -> None:
        tokens = [
            token
            for token in self._session_tokens.get(session_id, [])
            if isinstance(token, dict) and not token_expired(token)
        ]
        if tokens:
            self._session_tokens[session_id] = tokens
        else:
            self._session_tokens.pop(session_id, None)

    def export_session_tokens(self) -> dict[str, list[dict[str, Any]]]:
        for session_id in list(self._session_tokens):
            self._prune_session_tokens(session_id)
        return deepcopy(self._session_tokens)

    def import_session_tokens(
        self,
        tokens: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._session_tokens = deepcopy(tokens)
        for session_id in list(self._session_tokens):
            self._prune_session_tokens(session_id)

    def webauthn_registration_options(
        self,
        route: AuthorizerRoute,
    ) -> dict[str, Any]:
        result = self.server.webauthn_registration_options(
            {
                "userId": INTERNAL_A4P_USER_ID,
                "userName": "jiuwenswarm-owner",
                "userDisplayName": "JiuwenSwarm Owner",
            }
        )
        registration_request_id = str(
            result.get("registrationRequestId") or ""
        ).strip()
        if registration_request_id:
            self._webauthn_registration_routes[registration_request_id] = (
                route.logical_key
            )
        return {
            "registrationRequestId": registration_request_id,
            "options": result.get("options"),
        }

    def verify_webauthn_registration(
        self,
        *,
        registration_request_id: str,
        credential: dict[str, Any],
        route: AuthorizerRoute,
    ) -> dict[str, Any]:
        expected_route = self._webauthn_registration_routes.get(
            registration_request_id
        )
        if expected_route is None:
            raise A4PProtocolError(
                f"No WebAuthn registration request: {registration_request_id}",
                code="A4P_WEBAUTHN_REGISTRATION_NOT_PENDING",
            )
        if expected_route != route.logical_key:
            raise A4PProtocolError(
                "WebAuthn registration route mismatch",
                code="A4P_WEBAUTHN_REGISTRATION_ROUTE_MISMATCH",
            )
        self._webauthn_registration_routes.pop(registration_request_id, None)
        result = self.server.verify_webauthn_registration(
            {
                "registrationRequestId": registration_request_id,
                "userId": INTERNAL_A4P_USER_ID,
                "credential": credential,
            }
        )
        registered = (
            result.get("credential")
            if isinstance(result.get("credential"), dict)
            else {}
        )
        credential_id = str(registered.get("credentialId") or "").strip()
        return {
            "registered": bool(result.get("registered")),
            "created": bool(result.get("created")),
            "credential": self.webauthn.registered_credential_summary(
                credential_id
            ),
        }

    def webauthn_credential_status(self) -> dict[str, Any]:
        credentials = self.webauthn.credential_summaries(INTERNAL_A4P_USER_ID)
        return {
            "enabled": bool(self.config.get("enabled", False)),
            "requireUserSignature": self.require_user_signature,
            "signatureMethod": "webauthn",
            "rpId": WEBAUTHN_RP_ID,
            "rpName": WEBAUTHN_RP_NAME,
            "expectedOrigin": WEBAUTHN_EXPECTED_ORIGIN,
            "credentialStorePath": str(self._webauthn_credential_store_path),
            "credentials": credentials,
        }

    async def find_valid_intent_token(
        self,
        *,
        session_id: str,
        identity: A4PIdentity,
        action: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        self._prune_session_tokens(session_id)
        for token in list(self._session_tokens.get(session_id, [])):
            if await self._verify_intent_token(
                token=token,
                identity=identity,
                action=action,
                params=params,
            ):
                return token
        return None

    def store_cron_intent_token(self, cron_job_id: str, token: dict[str, Any]) -> None:
        self.cron_tokens.put(cron_job_id, token)

    async def find_valid_cron_intent_token(
        self,
        *,
        cron_job_id: str,
        identity: A4PIdentity,
        action: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        job_id = str(cron_job_id or "").strip()
        token = self.cron_tokens.get(job_id)
        if token is None:
            return None
        if await self._verify_intent_token(
            token=token,
            identity=identity,
            action=action,
            params=params,
        ):
            return token
        return None

    async def _verify_intent_token(
        self,
        *,
        token: dict[str, Any],
        identity: A4PIdentity,
        action: str,
        params: dict[str, Any] | None,
    ) -> bool:
        result = await self.server.verify_intent_token(
            {
                "token": token,
                "expected": {
                    "action": action,
                    "params": normalize_a4p_action_params(action, params),
                    "agentId": _agent_subject_id(identity.agent_id),
                    "userId": INTERNAL_A4P_USER_ID,
                },
            }
        )
        return bool(result.valid)

    def cron_intent_authorization_summary(
        self,
        cron_job_id: str,
    ) -> dict[str, Any] | None:
        """Return a non-secret authorization summary for one cron job."""
        job_id = str(cron_job_id or "").strip()
        token = self.cron_tokens.get(job_id)
        if token is None:
            return None

        intent = token.get("intent") if isinstance(token.get("intent"), dict) else {}
        return {
            "cronJobId": job_id,
            "tokenId": str(token.get("tokenId") or "")[:12],
            "actions": deepcopy(intent.get("actions") or []),
            "expireAt": token.get("expireAt"),
        }


_runtime: A4PRuntime | None = None


def get_a4p_runtime(config: dict[str, Any] | None = None) -> A4PRuntime:
    global _runtime
    if _runtime is None:
        _runtime = A4PRuntime(config=config)
        logger.info("[A4P] intent authorization runtime initialized")
    return _runtime


def reset_a4p_runtime_for_tests() -> None:
    global _runtime
    _runtime = None


async def disable_a4p_runtime() -> int:
    """Cancel in-flight approvals and discard volatile A4P runtime state."""
    global _runtime
    runtime = _runtime
    if runtime is None:
        return 0
    _runtime = None
    cancelled = await runtime.authorizer.cancel_all(
        reason="A4P was disabled while authorization was pending",
        code="A4P_DISABLED",
    )
    if cancelled:
        logger.info("[A4P] cancelled %d pending authorizations on disable", cancelled)
    return cancelled


async def reconfigure_a4p_runtime(config: dict[str, Any] | None = None) -> int:
    """Apply A4P configuration changes while preserving issued session tokens."""
    global _runtime
    runtime = _runtime
    next_config = get_a4p_config(config)
    if runtime is None:
        return 0
    if runtime.config == next_config:
        return 0
    if not bool(next_config.get("enabled", False)):
        return await disable_a4p_runtime()

    session_tokens = runtime.export_session_tokens()
    cancelled = await runtime.authorizer.cancel_all(
        reason="A4P configuration changed while authorization was pending",
        code="A4P_CONFIG_CHANGED",
    )
    replacement = A4PRuntime(config={"a4p": next_config})
    replacement.import_session_tokens(session_tokens)
    _runtime = replacement
    logger.info(
        "[A4P] runtime reconfigured require_user_signature=%s",
        replacement.require_user_signature,
    )
    return cancelled


__all__ = [
    "A4PIdentity",
    "A4PRuntime",
    "CRON_INTENT_TOKENS_DB_FILENAME",
    "CRON_INTENT_TOKENS_VERSION",
    "DEFAULT_CRON_INTENT_VALIDITY_SECONDS",
    "INTERNAL_A4P_USER_ID",
    "WEBAUTHN_CREDENTIAL_STORE_FILENAME",
    "get_a4p_config",
    "get_a4p_runtime",
    "is_a4p_enabled",
    "disable_a4p_runtime",
    "reconfigure_a4p_runtime",
    "reset_a4p_runtime_for_tests",
    "resolve_a4p_identity",
]
