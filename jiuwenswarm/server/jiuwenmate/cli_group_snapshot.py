"""Host-private CLI approval snapshots; never serialized as model tool arguments."""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.persistent_agent.schema.protocol import Identifier, ProtocolModel


class CliAttachment(ProtocolModel):
    group_id: Identifier
    group_name: str
    artifact_id: Identifier
    artifact_kind: Literal["file"] = "file"
    title: str
    file_name: Annotated[str, Field(min_length=1, max_length=200)]
    file_size: Annotated[int, Field(ge=0, le=20 * 1024 * 1024)]
    file_digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    cache_name: Annotated[str, Field(pattern=r"^[a-f0-9]{24}\.bin$")]
    message_id: Identifier
    file_key: Annotated[str, Field(pattern=r"^(?:file|img)_[A-Za-z0-9_-]+$")]
    resource_type: Literal["file", "image"]
    created_at: AwareDatetime
    url: None = None
    snippet: str | None = None

    @model_validator(mode="after")
    def filename_only(self):
        if self.file_name in {".", ".."} or any(char in self.file_name for char in '/\\\x00:<>"|?*'):
            raise ValueError("invalid CLI attachment filename")
        return self


class CliReadSnapshot(core.InvocationRef):
    kind: Literal["cli_group_read"] = "cli_group_read"
    command_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    plan: dict[str, Any]
    source_authorizations: dict[str, dict[str, str]] = Field(default_factory=dict)
    content_types: tuple[Literal["messages", "files"], ...]
    hits: Annotated[tuple[core.GroupMessageSearchHit, ...], Field(max_length=500)] = ()
    artifacts: Annotated[tuple[CliAttachment, ...], Field(max_length=50)] = ()
    has_more: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def exact_content(self):
        if not self.content_types or len(set(self.content_types)) != len(self.content_types):
            raise ValueError("invalid CLI snapshot content types")
        if self.artifacts and "files" not in self.content_types:
            raise ValueError("file snapshots require file consent")
        known = {(hit.group_id, hit.message.message_id) for hit in self.hits}
        if any((item.group_id, item.message_id) not in known for item in self.artifacts):
            raise ValueError("attachment has no source message")
        return self
