# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared URL review helpers for read-only network and browser actions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, unquote_to_bytes, urlparse

from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    UserIntentSource,
    has_valid_ask_user_clarification,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import ToolDecisionFacts
from jiuwenswarm.agents.harness.common.rails.permissions.network_scope import (
    has_secret_query,
    host_matches_allowed_domain,
    network_host_rejection_reason,
    normalize_network_host,
)

URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
DOMAIN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}'\""
ABSOLUTE_URI_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]*"
)
UPLOAD_KEYS = frozenset(
    {"attachment", "attachments", "file", "file_path", "files", "upload"}
)
SAFE_URL_SCHEMES = frozenset({"https"})
INTERNAL_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".lan",
    ".home.arpa",
)
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata",
        "metadata.google.internal",
    }
)
HARD_BLOCK_REASONS = frozenset(
    {
        "network_host_missing",
        "network_host_not_public",
        "network_internal_hostname",
        "network_metadata_host",
        "network_public_suffix_host",
        "network_scheme_not_https",
        "network_secret_query",
        "network_single_label_host",
        "network_url_invalid_port",
        "network_url_userinfo",
    }
)


@dataclass(frozen=True, slots=True)
class AbsoluteUriFacts:
    """Bounded URI facts owned by URL safety."""

    uri: str
    scheme: str
    host: str
    invalid_port: bool
    userinfo_present: bool
    secret_query_present: bool
    network_like: bool
    parse_failure_reason: str = ""
    file_local: bool = False
    file_path: str = ""
    file_resolution_reason: str = ""


@dataclass(frozen=True, slots=True)
class NetworkScopeFacts:
    """Only network values consumed by a current hard guard or view."""

    absolute_uris: tuple[AbsoluteUriFacts, ...]
    urls: tuple[str, ...]
    hosts: tuple[str, ...]
    schemes: tuple[str, ...]
    upload_like: bool


def inspect_network_scope(subject: ToolDecisionFacts) -> NetworkScopeFacts:
    """Inspect bounded URL facts without adding them to the action carrier."""

    uris = inspect_absolute_uris(subject.untrusted_args, subject.workspace_root)
    return NetworkScopeFacts(
        absolute_uris=uris,
        urls=tuple(dict.fromkeys(uri.uri for uri in uris if uri.scheme in {"http", "https"})),
        hosts=tuple(dict.fromkeys(uri.host for uri in uris if uri.host)),
        schemes=tuple(dict.fromkeys(uri.scheme for uri in uris if uri.scheme)),
        upload_like=_contains_upload(subject.untrusted_args),
    )


def inspect_absolute_uris(
    value: Any,
    workspace_root: Path | str | None = None,
) -> tuple[AbsoluteUriFacts, ...]:
    """Return each distinct absolute URI embedded in the current arguments."""

    root = _safe_root(workspace_root)
    found: list[AbsoluteUriFacts] = []
    seen: set[str] = set()
    for text in _string_values(value):
        for match in ABSOLUTE_URI_PATTERN.finditer(text):
            uri = match.group(0)
            if uri in seen:
                continue
            seen.add(uri)
            found.append(_inspect_uri(uri, root))
    return tuple(found)


@dataclass(frozen=True)
class RecentUrlSource:
    """Bounded, low-trust URL provenance from prior discovery tool results."""

    url: str
    host: str
    source_tool: str
    source_kind: str = "recent_search_result"
    trusted: bool = False

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for reviewer evidence."""
        return {
            "host": self.host,
            "source_kind": self.source_kind,
            "source_tool": self.source_tool,
            "trusted": self.trusted,
            "url": self.url,
        }


@dataclass(frozen=True)
class ReviewableUrlDecision:
    """Decision from shared URL reviewability checks."""

    accepted: bool
    kind: str
    reason: str
    evidence_summary: dict[str, object] = field(default_factory=dict)
    hard_block: bool = False
    semantic_review_required: bool = False

    def __post_init__(self) -> None:
        if self.semantic_review_required and (not self.accepted or self.hard_block):
            raise ValueError("semantic review cannot override URL safety rejection")


def evaluate_reviewable_url_scope(
    subject: ToolDecisionFacts,
    *,
    evidence: OriginalUserIntentEvidence | None,
    recent_url_sources: tuple[RecentUrlSource, ...] = (),
) -> ReviewableUrlDecision:
    """Return whether a single public HTTPS URL is safe to send to AutoReviewer."""
    side_effects, urls, _hosts, _schemes, upload_like = _network_facts(subject)
    summary = _base_summary(subject)
    if "external_send" in side_effects:
        return _reject("network_external_send", summary)
    if upload_like:
        return _reject("network_upload_like", summary)
    if len(urls) != 1:
        reason = (
            "network_url_missing"
            if not urls
            else "network_url_count_not_one"
        )
        return _reject(reason, summary)
    url = urls[0]
    parsed = urlparse(str(url or "").strip())
    unsafe_reason = unsafe_public_https_url_reason(parsed)
    if unsafe_reason is not None:
        return _reject(
            unsafe_reason, summary, hard_block=unsafe_reason in HARD_BLOCK_REASONS
        )
    normalized_url = normalize_url_for_match(url)
    host = normalize_network_host(parsed.hostname)
    if not _has_trusted_host_user_intent(evidence):
        return _reject(
            "original_user_intent_missing",
            {
                **summary,
                "host": host,
                "registrable_domain": registrable_domain(host),
            },
        )
    source_kind = _resolve_url_source_kind(
        normalized_url=normalized_url,
        host=host,
        evidence=evidence,
        recent_url_sources=recent_url_sources,
    )
    enriched_summary = {
        **summary,
        "host": host,
        "query_safety": "no_secret_query",
        "registrable_domain": registrable_domain(host),
        "source_kind": source_kind,
    }
    if has_valid_ask_user_clarification(evidence) and (
        source_kind != "recent_search_result"
    ):
        return _semantic_unresolved(enriched_summary)
    if source_kind:
        return ReviewableUrlDecision(
            accepted=True,
            kind="readonly_network_fetch",
            reason="readonly_network_url_reviewable",
            evidence_summary=enriched_summary,
        )
    return _semantic_unresolved(enriched_summary)


def unsafe_public_https_url_reason(parsed: ParseResult) -> str | None:
    """Return why a parsed URL must not be auto-reviewed, or None if eligible."""
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES:
        return "network_scheme_not_https"
    if _has_invalid_port(parsed):
        return "network_url_invalid_port"
    if parsed.username or parsed.password:
        return "network_url_userinfo"
    if has_secret_query(parsed.query):
        return "network_secret_query"
    host = normalize_network_host(parsed.hostname)
    if host in METADATA_HOSTS:
        return "network_metadata_host"
    if host.endswith(INTERNAL_HOST_SUFFIXES):
        return "network_internal_hostname"
    return network_host_rejection_reason(host)


def absolute_uri_hard_block_reason(uri: AbsoluteUriFacts) -> str | None:
    """Return non-overridable safety evidence from canonical URI facts."""
    if uri.invalid_port:
        return "network_url_invalid_port"
    if uri.userinfo_present:
        return "network_url_userinfo"
    if uri.scheme == "https" and uri.secret_query_present:
        return "network_secret_query"
    if not uri.network_like:
        return None
    host = uri.host
    if host in METADATA_HOSTS:
        return "network_metadata_host"
    if host.endswith(INTERNAL_HOST_SUFFIXES):
        return "network_internal_hostname"
    return network_host_rejection_reason(host)


def extract_urls_from_text(text: str) -> tuple[str, ...]:
    """Return normalized HTTP(S) URLs found in text."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(str(text or "")):
        url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        normalized = normalize_url_for_match(url)
        if not normalized or normalized in seen:
            continue
        urls.append(normalized)
        seen.add(normalized)
    return tuple(urls)


def normalize_url_for_match(url: str) -> str:
    """Return a stable URL form for exact provenance matching."""
    parsed = urlparse(str(url or "").strip().rstrip(TRAILING_URL_PUNCTUATION))
    scheme = parsed.scheme.lower()
    host = normalize_network_host(parsed.hostname)
    if not scheme or not host:
        return ""
    port = _safe_port_suffix(parsed)
    if port is None:
        return ""
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}{port}{path}{query}"


def registrable_domain(host: str) -> str:
    """Return a best-effort registrable domain without external dependencies."""
    normalized = normalize_network_host(host)
    labels = [label for label in normalized.split(".") if label]
    if len(labels) < 2:
        return normalized
    if len(labels) >= 3 and labels[-2] in {"co", "com", "edu", "gov", "net", "org"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _resolve_url_source_kind(
    *,
    normalized_url: str,
    host: str,
    evidence: OriginalUserIntentEvidence | None,
    recent_url_sources: tuple[RecentUrlSource, ...],
) -> str:
    if _matches_host_user_intent(
        normalized_url=normalized_url, host=host, evidence=evidence
    ):
        return "explicit_user_intent"
    for source in recent_url_sources:
        if source.trusted and source.url == normalized_url:
            return source.source_kind
    return ""


def _has_trusted_host_user_intent(
    evidence: OriginalUserIntentEvidence | None,
) -> bool:
    if evidence is None or evidence.source != UserIntentSource.HOST_USER_MESSAGE:
        return False
    if str(evidence.text or "").strip():
        return True
    context = evidence.context
    if context is None:
        return False
    return any(
        str(turn.text or "").strip() or bool(turn.clarifications)
        for turn in context.trusted_turns
    )


def _matches_host_user_intent(
    *,
    normalized_url: str,
    host: str,
    evidence: OriginalUserIntentEvidence | None,
) -> bool:
    if evidence is None or evidence.source != UserIntentSource.HOST_USER_MESSAGE:
        return False
    evidence_urls, evidence_hosts = _extract_network_intent_tokens(evidence.text)
    if normalized_url in evidence_urls:
        return True
    return any(
        host_matches_allowed_domain(host, evidence_host)
        for evidence_host in evidence_hosts
    )


def _extract_network_intent_tokens(text: str) -> tuple[set[str], set[str]]:
    urls = {normalize_url_for_match(url) for url in extract_urls_from_text(text)}
    hosts = {
        normalize_network_host(match.group(0))
        for match in DOMAIN_PATTERN.finditer(str(text or ""))
    }
    return {url for url in urls if url}, {host for host in hosts if host}


def _has_invalid_port(parsed: ParseResult) -> bool:
    try:
        _ = parsed.port
    except ValueError:
        return True
    return False


def _safe_port_suffix(parsed: ParseResult) -> str | None:
    try:
        port = parsed.port
    except ValueError:
        return None
    return f":{port}" if port is not None else ""


def _network_facts(
    subject: ToolDecisionFacts,
) -> tuple[frozenset[str], tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    network = inspect_network_scope(subject)
    return (
        subject.capability.static_side_effects,
        network.urls,
        network.hosts,
        network.schemes,
        network.upload_like,
    )


def _base_summary(subject: ToolDecisionFacts) -> dict[str, object]:
    _side_effects, urls, hosts, schemes, _upload_like = _network_facts(subject)
    return {
        "host_count": len(hosts),
        "hosts": list(hosts[:5]),
        "registrable_domains": [
            registrable_domain(host) for host in hosts[:5]
        ],
        "schemes": list(schemes[:5]),
        "url_count": len(urls),
        "urls": [normalize_url_for_match(url) for url in urls[:5]],
    }


def _reject(
    reason: str,
    summary: dict[str, object],
    *,
    hard_block: bool = False,
) -> ReviewableUrlDecision:
    return ReviewableUrlDecision(
        accepted=False,
        kind="readonly_network_fetch",
        reason=reason,
        evidence_summary=summary,
        hard_block=hard_block,
        semantic_review_required=False,
    )


def _semantic_unresolved(summary: dict[str, object]) -> ReviewableUrlDecision:
    return ReviewableUrlDecision(
        accepted=True,
        kind="readonly_network_fetch",
        reason="network_url_semantic_unresolved",
        evidence_summary=summary,
        hard_block=False,
        semantic_review_required=True,
    )


def _label(value: object) -> str:
    return str(value or "").strip().lower()


def _inspect_uri(uri: str, root: Path | None) -> AbsoluteUriFacts:
    try:
        parsed = urlparse(uri)
        scheme = parsed.scheme.lower()
        host = normalize_network_host(parsed.hostname)
        invalid_port = _has_invalid_port(parsed)
        userinfo = parsed.username is not None or parsed.password is not None
    except ValueError:
        return AbsoluteUriFacts(uri, "", "", False, False, False, False, "uri_parse_invalid")
    parse_failure = ""
    if "%" in uri:
        try:
            decoded = unquote_to_bytes(uri).decode("utf-8")
            if "\x00" in decoded:
                parse_failure = "uri_nul"
        except UnicodeDecodeError:
            parse_failure = "uri_utf8_invalid"
    if scheme != "file" and not host:
        parse_failure = parse_failure or "uri_host_missing"
    common = {
        "uri": uri,
        "scheme": scheme,
        "host": host,
        "invalid_port": invalid_port,
        "userinfo_present": userinfo,
        "secret_query_present": has_secret_query(parsed.query),
        "network_like": bool(scheme != "file" and host and not parse_failure),
        "parse_failure_reason": parse_failure,
    }
    if scheme != "file" or parse_failure:
        return AbsoluteUriFacts(**common)
    if host and host != "localhost":
        return AbsoluteUriFacts(**common, file_resolution_reason="file_uri_nonlocal_host")
    if parsed.query or parsed.fragment:
        return AbsoluteUriFacts(
            **common,
            file_local=True,
            file_resolution_reason="file_uri_query_or_fragment",
        )
    try:
        path_text = unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError:
        return AbsoluteUriFacts(
            **common,
            file_local=True,
            file_resolution_reason="file_uri_utf8_invalid",
        )
    if not path_text.startswith("/") or "\x00" in path_text:
        return AbsoluteUriFacts(
            **common,
            file_local=True,
            file_resolution_reason="file_uri_path_invalid",
        )
    try:
        path = Path(path_text).expanduser()
        if not path.is_absolute() and root is not None:
            path = root / path
        resolved = path.resolve(strict=False).as_posix()
    except (OSError, RuntimeError, ValueError):
        return AbsoluteUriFacts(
            **common,
            file_local=True,
            file_resolution_reason="file_uri_path_unresolvable",
        )
    return AbsoluteUriFacts(**common, file_local=True, file_path=resolved)


def _safe_root(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _string_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        values: list[str] = []
        for nested in value.values():
            values.extend(_string_values(nested))
        return tuple(values)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        values = []
        for nested in value:
            values.extend(_string_values(nested))
        return tuple(values)
    return ()


def _contains_upload(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (str(key).strip().lower() in UPLOAD_KEYS and bool(nested))
            or _contains_upload(nested)
            for key, nested in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_contains_upload(item) for item in value)
    return False
