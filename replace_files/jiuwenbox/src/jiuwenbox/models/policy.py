"""Security policy data models (static only)."""

from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _expand_path(value: str) -> str:
    """Expand shell-style path markers without requiring the path to exist."""
    return str(Path(os.path.expandvars(value)).expanduser())


class BindMount(BaseModel):
    host_path: str
    sandbox_path: str
    mode: Literal["ro", "rw"] = "ro"

    @field_validator("host_path", "sandbox_path", mode="before")
    @classmethod
    def expand_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value


class DirectoryMount(BaseModel):
    path: str
    permissions: str | int | None = None

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return _expand_path(value)
        return value

    @field_validator("permissions", mode="before")
    @classmethod
    def permissions_must_be_octal(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, int):
            if 0 <= value <= 0o777:
                text = format(value, "o")
            else:
                text = str(value)
        else:
            text = str(value)
        if not text:
            raise ValueError("directory permissions cannot be empty")
        if not all(char in "01234567" for char in text):
            raise ValueError("directory permissions must be an octal value")
        if len(text) > 4:
            raise ValueError("directory permissions must be at most four octal digits")
        return text.zfill(4)


class FilesystemPolicy(BaseModel):
    directories: list[str | DirectoryMount] = Field(default_factory=list)
    read_only: list[str] = Field(default_factory=list)
    read_write: list[str] = Field(default_factory=list)
    bind_mounts: list[BindMount] = Field(default_factory=list)

    @field_validator("directories", mode="before")
    @classmethod
    def expand_directory_paths(cls, value: object) -> object:
        if isinstance(value, list):
            return [_expand_path(item) if isinstance(item, str) else item for item in value]
        return value

    @field_validator("read_only", "read_write", mode="before")
    @classmethod
    def expand_path_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return [_expand_path(item) if isinstance(item, str) else item for item in value]
        return value


class ProcessPolicy(BaseModel):
    run_as_user: str = "sandbox"
    run_as_group: str = "sandbox"


class NamespacePolicy(BaseModel):
    user: bool = True
    pid: bool = True
    ipc: bool = True
    cgroup: bool = True
    uts: bool = True


class CapabilityPolicy(BaseModel):
    add: list[str] = Field(default_factory=list)
    drop: list[str] = Field(default_factory=list)


class LandlockPolicy(BaseModel):
    compatibility: Literal["disabled", "best_effort", "hard_requirement"] = "best_effort"


class SyscallPolicy(BaseModel):
    blocked: list[str] = Field(default_factory=list)


class NetworkMode(str, enum.Enum):
    ISOLATED = "isolated"
    HOST = "host"


class NetworkRulePolicy(BaseModel):
    default: Literal["deny", "allow"] = "deny"
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=list)
    blocked_ips: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    blocked_ports: list[int] = Field(default_factory=list)


class NetworkPolicy(BaseModel):
    mode: NetworkMode = NetworkMode.ISOLATED
    egress: NetworkRulePolicy = Field(default_factory=NetworkRulePolicy)
    ingress: NetworkRulePolicy = Field(default_factory=NetworkRulePolicy)


class SecurityPolicy(BaseModel):
    """Complete static security policy for a sandbox."""

    version: int = 1
    name: str = "default"
    sandbox_workspace: str = "/sandbox"
    filesystem_policy: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    process: ProcessPolicy = Field(default_factory=ProcessPolicy)
    namespace: NamespacePolicy = Field(default_factory=NamespacePolicy)
    capabilities: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    landlock: LandlockPolicy = Field(default_factory=LandlockPolicy)
    syscall: SyscallPolicy = Field(default_factory=SyscallPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)

    @field_validator("sandbox_workspace")
    @classmethod
    def sandbox_workspace_must_be_absolute(cls, value: str) -> str:
        workspace = Path(_expand_path(value))
        if not workspace.is_absolute():
            raise ValueError("sandbox_workspace must be an absolute host path")
        return str(workspace)
