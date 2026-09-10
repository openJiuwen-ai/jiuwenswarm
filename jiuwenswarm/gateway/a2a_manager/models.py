"""Typed configuration for the Gateway A2A ingress service."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any

from jiuwenswarm.gateway.channel_manager.protocol.a2a.security import (
    hash_credential,
    validate_security,
)


class A2AIngressState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class A2AIngressError(RuntimeError):
    """Stable error exposed by the ingress management API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class A2AIngressConfig:
    """Configuration for the ``A2A_SERVER_*`` settings."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 19100
    rpc_path: str = "/a2a"
    protocol_version: str = "1.0.0"
    card_path: str = "/.well-known/agent-card.json"
    extended_card_path: str = "/agent/authenticatedExtendedCard"
    app_name: str = "JiuwenSwarm Gateway A2A Server"
    app_description: str = "A2A ingress for JiuwenSwarm Gateway"
    app_version: str = "0.1.0"
    expose_reasoning: bool = True
    auth_type: str = "none"
    api_key_header: str = "X-API-Key"
    card_auth_required: bool = False
    credential: str = field(default="", repr=False)

    @property
    def credential_hash(self) -> str:
        return hash_credential(self.credential) if self.credential else ""

    def validate(self) -> "A2AIngressConfig":
        try:
            validate_security(self)
        except ValueError as exc:
            raise A2AIngressError("A2A_CONFIG_INVALID", str(exc)) from exc
        if not self.host.strip():
            raise A2AIngressError(
                "A2A_CONFIG_INVALID", "A2A_SERVER_HOST cannot be empty"
            )
        if not 1 <= self.port <= 65535:
            raise A2AIngressError(
                "A2A_CONFIG_INVALID", "A2A_SERVER_PORT must be between 1 and 65535"
            )
        for name in ("rpc_path", "card_path", "extended_card_path"):
            if not getattr(self, name).startswith("/"):
                raise A2AIngressError("A2A_CONFIG_INVALID", f"{name} must start with /")
        paths = {self.rpc_path, self.card_path, self.extended_card_path}
        if len(paths) != 3:
            raise A2AIngressError(
                "A2A_CONFIG_INVALID",
                "rpc_path, card_path, and extended_card_path must be distinct",
            )
        for name in ("protocol_version", "app_name", "app_version"):
            if not getattr(self, name).strip():
                raise A2AIngressError("A2A_CONFIG_INVALID", f"{name} cannot be empty")
        return self

    def with_patch(self, patch: dict[str, Any]) -> "A2AIngressConfig":
        allowed = set(asdict(self)) | {"clear_credential"}
        unknown = set(patch) - allowed
        if unknown:
            raise A2AIngressError(
                "A2A_CONFIG_INVALID",
                f"Unsupported fields: {', '.join(sorted(unknown))}",
            )
        values = asdict(self)
        clear = patch.get("clear_credential", False)
        if not isinstance(clear, bool):
            raise A2AIngressError(
                "A2A_CONFIG_INVALID", "clear_credential must be a boolean"
            )
        credential = patch.get("credential", "")
        if not isinstance(credential, str):
            raise A2AIngressError("A2A_CONFIG_INVALID", "credential must be a string")
        if clear and credential:
            raise A2AIngressError(
                "A2A_CONFIG_INVALID", "Cannot replace and clear a credential together"
            )
        if clear:
            values["credential"] = ""
        elif credential:
            values["credential"] = credential
        for name, value in patch.items():
            if name in {"credential", "clear_credential"}:
                continue
            if name == "port":
                try:
                    values[name] = int(value)
                except (TypeError, ValueError) as exc:
                    raise A2AIngressError(
                        "A2A_CONFIG_INVALID", "port must be an integer"
                    ) from exc
            elif name in {"enabled", "expose_reasoning", "card_auth_required"}:
                if not isinstance(value, bool):
                    raise A2AIngressError(
                        "A2A_CONFIG_INVALID", f"{name} must be a boolean"
                    )
                values[name] = value
            elif value is None:
                raise A2AIngressError("A2A_CONFIG_INVALID", f"{name} cannot be null")
            else:
                values[name] = str(value).strip()
        return replace(self, **values).validate()

    def to_channel_config(self):
        """Create the protocol-adapter configuration."""
        from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
            A2AChannelConfig,
        )

        return A2AChannelConfig(
            enabled=self.enabled,
            host=self.host,
            port=self.port,
            rpc_path=self.rpc_path,
            protocol_version=self.protocol_version,
            card_path=self.card_path,
            extended_card_path=self.extended_card_path,
            app_name=self.app_name,
            app_description=self.app_description,
            app_version=self.app_version,
            expose_reasoning=self.expose_reasoning,
            auth_type=self.auth_type,
            api_key_header=self.api_key_header,
            card_auth_required=self.card_auth_required,
            credential_hash=self.credential_hash,
        )


@dataclass(frozen=True)
class A2AIngressSnapshot:
    enabled: bool
    state: A2AIngressState
    desired_host: str
    desired_port: int
    desired_rpc_path: str
    desired_card_path: str
    desired_extended_card_path: str
    desired_protocol_version: str
    desired_app_name: str
    desired_app_description: str
    desired_app_version: str
    desired_expose_reasoning: bool
    desired_rpc_url: str
    desired_card_url: str
    desired_extended_card_url: str
    effective_host: str | None
    effective_port: int | None
    effective_rpc_path: str | None
    effective_card_path: str | None
    effective_extended_card_path: str | None
    effective_rpc_url: str | None
    effective_card_url: str | None
    effective_extended_card_url: str | None
    exposure_warning: str | None
    started_at: float | None
    last_error: str | None
    config_revision: int
    desired_auth_type: str = "none"
    desired_api_key_header: str = "X-API-Key"
    desired_card_auth_required: bool = False
    credential_configured: bool = False
    effective_auth_type: str | None = None
    effective_card_auth_required: bool | None = None
    security_pending_apply: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result
