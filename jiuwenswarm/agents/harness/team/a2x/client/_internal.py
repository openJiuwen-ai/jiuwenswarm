"""Shared helpers used by the sync and async client classes.

Keeping these as pure module-level functions lets ``A2XRegistryClient`` and
``AsyncA2XRegistryClient`` stay symmetric without sharing a base class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import httpx

from .errors import UnexpectedServiceTypeError
from .models import AgentDetail

# ── Sentinels / constants ────────────────────────────────────────────────────

UNSET: Final[Any] = object()
"""Sentinel distinguishing "argument omitted" from "explicitly None"."""

DEFAULT_FORMATS: Final[dict[str, str]] = {"a2a": "v0.0"}
"""SDK default ``formats`` for Agent Team use cases."""

DEFAULT_EMBEDDING_MODEL: Final[str] = "all-MiniLM-L6-v2"
"""SDK default embedding model used by ``create_dataset``. Must stay in
sync with the backend's ``vector.utils.embedding.DEFAULT_EMBEDDING_MODEL``
— duplicated by design (the client package ships independently and
cannot import from the rest of the project)."""

DEFAULT_OWNERSHIP_FILE: Final[Path] = Path.home() / ".a2x_registry_client" / "owned.json"

STATUS_FIELD: Final[str] = "status"
"""Service-state field on the AgentCard. Eureka-style intent — values
``online`` / ``busy`` / ``offline``. Default is ``online``: any service
registered without an explicit ``status`` field is treated as online by
filter matching (see ``status=online`` carve-out in backend's match logic)."""

STATUS_ONLINE: Final[str] = "online"
STATUS_BUSY: Final[str] = "busy"
STATUS_OFFLINE: Final[str] = "offline"
_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_ONLINE, STATUS_BUSY, STATUS_OFFLINE}
)

DEFAULT_RESERVATION_TTL: Final[int] = 30
"""Default lease lifetime for ``reserve_blank_agents`` (seconds). Generous
enough for typical P2P negotiation including network + LLM round-trips."""

BLANK_AGENT_NAME_PREFIX: Final[str] = "_BlankAgent_"
"""Name prefix used when constructing a blank card. Kept distinct so two
blank agents with different endpoints get different ``name``s (and thus
different sids via ``generate_service_id``); no longer used for discovery."""

BLANK_DESCRIPTION_SENTINEL: Final[str] = "__BLANK__"
"""Description sentinel identifying idle-pool agents. Matched exactly by
the backend filter against the **raw** agent_card.description (pre-
build_description transform). Changing it breaks cross-SDK interop."""

ENDPOINT_FIELD: Final[str] = "endpoint"
"""Custom AgentCard field holding the agent's endpoint URL. AgentCard uses
``extra="allow"`` (see ``src/register/models.py``), so the backend stores it
verbatim. Callers of ``replace_agent_card`` must preserve this field so
``restore_to_blank`` can recover the endpoint after process restart."""

_CONTENT_TYPE_JSON: Final[str] = "application/json"


# ── URL construction ─────────────────────────────────────────────────────────

def _encode(segment: str) -> str:
    return quote(segment, safe="")


# Paths are relative (no leading "/") so ``httpx.Client(base_url=...)`` joins
# them under any mount point (e.g. ``http://host/a2x/``). Callers must ensure
# ``base_url`` ends with ``/`` — ``normalize_base_url`` takes care of that.

DATASETS_ROOT = "api/datasets"


def dataset_path(dataset: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}"


def services_path(dataset: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}/services"


def service_path(dataset: str, service_id: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}/services/{_encode(service_id)}"


def a2a_register_path(dataset: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}/services/a2a"


def reservations_path(dataset: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}/reservations"


def reservation_holder_path(dataset: str, holder_id: str) -> str:
    return f"{DATASETS_ROOT}/{_encode(dataset)}/reservations/{_encode(holder_id)}"


def reservation_holder_sid_path(
    dataset: str, holder_id: str, service_id: str,
) -> str:
    return (
        f"{DATASETS_ROOT}/{_encode(dataset)}/reservations/"
        f"{_encode(holder_id)}/{_encode(service_id)}"
    )


def reservation_extend_path(dataset: str, holder_id: str) -> str:
    return (
        f"{DATASETS_ROOT}/{_encode(dataset)}/reservations/"
        f"{_encode(holder_id)}/extend"
    )


def service_lease_path(dataset: str, service_id: str) -> str:
    return (
        f"{DATASETS_ROOT}/{_encode(dataset)}/services/{_encode(service_id)}/lease"
    )


def normalize_base_url(base_url: str) -> str:
    """Ensure trailing ``/`` so relative paths append under the mount point."""
    return base_url if base_url.endswith("/") else base_url + "/"


# ── Body construction ────────────────────────────────────────────────────────

def build_create_dataset_body(
    name: str,
    embedding_model: str,
    formats: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "embedding_model": embedding_model}
    if formats is UNSET:
        body["formats"] = dict(DEFAULT_FORMATS)
    elif formats is not None:
        body["formats"] = formats
    return body


def build_register_agent_body(
    agent_card: dict[str, Any],
    service_id: str | None,
    persistent: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_card": agent_card, "persistent": persistent}
    if service_id is not None:
        body["service_id"] = service_id
    return body


def build_status_body(status: str) -> dict[str, Any]:
    """Body for the ``set_status`` SDK convenience method (validates enum locally)."""
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}"
        )
    return {STATUS_FIELD: status}


def build_blank_agent_card(endpoint: str) -> dict[str, Any]:
    """Blank-agent AgentCard template.

    ``name`` encodes the endpoint so the deterministic
    ``generate_service_id("agent", name)`` on the backend keeps sid stable
    across re-registrations of the same endpoint.

    ``description`` carries the ``BLANK_DESCRIPTION_SENTINEL`` — this is
    what the backend filter matches to discover idle-pool agents.

    ``status`` is set to ``online`` so the agent is immediately visible
    to ``status=online`` filters.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(f"endpoint must be a non-empty string, got {endpoint!r}")
    return {
        "name": f"{BLANK_AGENT_NAME_PREFIX}{endpoint}",
        "description": BLANK_DESCRIPTION_SENTINEL,
        ENDPOINT_FIELD: endpoint,
        STATUS_FIELD: STATUS_ONLINE,
    }


def build_filter_params(filters: dict[str, Any]) -> dict[str, Any]:
    """Build query params for ``GET .../services?<filters>``.

    Every ``(k, v)`` becomes a query param with AND semantics. Values are
    coerced to strings (HTTP query params are strings; backend also
    string-coerces its comparison). Empty filters → backend returns every
    service. Reserved keys (``fields``, ``page``, ``size``) are off-limits.
    """
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise ValueError(f"filters must be a dict, got {filters!r}")
    reserved = {"fields", "page", "size"}
    params: dict[str, Any] = {}
    for k, v in filters.items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"filter keys must be non-empty strings, got {k!r}")
        if k in reserved:
            raise ValueError(
                f"filter key {k!r} collides with a reserved query param "
                f"({reserved}); backend would drop it before filtering"
            )
        if v is None:
            raise ValueError(f"filter value for {k!r} must not be None")
        params[k] = str(v)
    return params


def extract_endpoint(card: Any) -> str | None:
    if not isinstance(card, dict):
        return None
    value = card.get(ENDPOINT_FIELD)
    if isinstance(value, str) and value.strip():
        return value
    return None


# ── Ownership-file resolution ────────────────────────────────────────────────

def resolve_ownership_file(raw: Any) -> Path | None:
    """Return the effective ownership-file path, or ``None`` for memory-only mode.

    - ``None`` → default ``~/.a2x_registry_client/owned.json``
    - ``False`` → disable persistence
    - ``Path`` / ``str`` → use as-is
    """
    if raw is None:
        return DEFAULT_OWNERSHIP_FILE
    if raw is False:
        return None
    if isinstance(raw, Path):
        return raw
    if isinstance(raw, str):
        return Path(raw)
    raise TypeError(
        f"ownership_file must be None, False, Path, or str; got {type(raw).__name__}"
    )


def build_default_headers(api_key: str | None) -> dict[str, str] | None:
    if not api_key:
        return None
    return {"Authorization": f"Bearer {api_key}"}


# ── Response post-processing ─────────────────────────────────────────────────

def parse_agent_detail(resp: httpx.Response) -> AgentDetail:
    """Decode a ``GET /services/{sid}`` response or raise ``UnexpectedServiceTypeError``."""
    content_type = resp.headers.get("content-type", "")
    if _CONTENT_TYPE_JSON not in content_type.lower():
        raise UnexpectedServiceTypeError(
            f"expected application/json, got {content_type or '<unknown>'}",
            status_code=resp.status_code,
            payload=None,
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise UnexpectedServiceTypeError(
            f"expected JSON object for agent detail, got {type(data).__name__}",
            status_code=resp.status_code,
            payload=None,
        )
    return AgentDetail.from_dict(data)


def parse_agent_list(resp: httpx.Response) -> list[dict[str, Any]]:
    """Parse a ``GET /services?<filters>`` response into flat ``id + card`` dicts.

    Each wrapped backend entry ``{id, type, name, description, metadata}``
    is flattened: the wrapper stays, then ``metadata`` is popped and its
    keys merged up. Metadata keys win on conflict — for a2a that means
    the raw card ``description`` overrides the ``build_description``-
    transformed one in the wrapper, giving callers the exact string they
    originally registered.

    For generic/skill, ``metadata`` has no ``name``/``description``, so
    the wrapper's values survive. ``type`` is preserved at top level so
    callers can tell cross-type results apart.
    """
    data = resp.json()
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for wrapped in data:
        if not isinstance(wrapped, dict):
            continue
        flat = dict(wrapped)
        metadata = flat.pop("metadata", None)
        if isinstance(metadata, dict):
            flat.update(metadata)
        result.append(flat)
    return result
