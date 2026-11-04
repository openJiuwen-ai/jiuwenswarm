# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PA-Bridge and VerbatimRAG-adapted retrieval contracts for topic recommendation."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    provider_validation,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    LearnedComponentBinding,
    LearnedComponentReceipt,
)

PA_RETRIEVER_COMPONENT_ID = "pa_bridge_fixed_retriever"
PA_API_FALLBACK_PROVIDER_ID = "api-ranker-fallback"
PA_RETRIEVAL_CONTRACT_VERSION = "topic-starter-v6-pa-retrieval-v2"
PA_TRAINING_EXAMPLE_SCHEMA_VERSION = "topic-starter-v6-pa-training-example-v1"
PASSIVE_STARTER_EXAMPLE_SCHEMA_VERSION = (
    "topic-starter-v6-passive-starter-example-v1"
)
ACTIVE_EXPRESSION_OBSERVATION_SCHEMA_VERSION = (
    "topic-starter-v6-active-expression-observation-v1"
)
ACTIVE_EXPRESSION_EXAMPLE_SCHEMA_VERSION = (
    "topic-starter-v6-active-expression-example-v1"
)
EVIDENCE_COMPONENT_ID = "verbatim_web_evidence"
EVIDENCE_API_PROVIDER_ID = "ark-zero-shot-adapted"
EVIDENCE_CONTRACT_VERSION = "topic-starter-v6-verbatim-evidence-v2"
NEWS_SOURCE_KIND = "news_report"
NEWS_FRESHNESS_CLASS = "news_event"
NEWS_MAX_WINDOW_HOURS = 72
EVIDENCE_EXTRACTOR_VERSION = "topic-starter-v6-verbatim-web-v5"
LEGACY_PREFIX_CHARACTER_COUNT = 2_000
OFFSET_CATALOG_MAX_CHARS = 600
GLOBAL_FIXED_CATEGORIES = frozenset(
    {"adjacent_explanation", "broad_timely_practical"}
)
_IGNORED_PROVIDER_TEXT_FIELDS = frozenset({"title", "text", "prompt"})


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_scope(value: Any) -> bool:
    return value == "global_new_chat" or (
        isinstance(value, str)
        and value.startswith("project:")
        and bool(value.removeprefix("project:").strip())
        and value.removeprefix("project:")
        == value.removeprefix("project:").strip()
    )


class TerminalRetrievalContractError(ValueError):
    """A provider or host input violated the PA retrieval contract."""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        component_receipt: LearnedComponentReceipt | None = None,
    ) -> None:
        self.reason = reason
        self.component_receipt = component_receipt
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{reason}{suffix}")


class EvidenceContractError(ValueError):
    """A frozen-document, extractor, or admission contract failed closed."""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        component_receipt: LearnedComponentReceipt | None = None,
    ) -> None:
        self.reason = reason
        self.component_receipt = component_receipt
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{reason}{suffix}")


def _failed_component_receipt(
    *,
    binding: LearnedComponentBinding,
    input_snapshot_id: str,
    request_id: str,
    reason: str,
) -> LearnedComponentReceipt:
    return LearnedComponentReceipt(
        binding=binding,
        input_snapshot_id=input_snapshot_id,
        request_id=request_id,
        score_semantics="not_scored",
        output_status="failed",
        fallback_reason=reason,
    )


def _finite_timestamp(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


@dataclass(frozen=True, slots=True)
class PATrainingExample:
    """One active or passive PA training tuple without copied private text."""

    user_state_snapshot_id: str
    query_text_locator: str | None
    query_source_starter_id: str | None
    domain: str
    positive_label: bool
    event_time: float
    context_scope: str
    source_artifact_id: str
    schema_version: str = PA_TRAINING_EXAMPLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _rejected3 = (
            not _nonempty(self.user_state_snapshot_id)
            or self.domain not in {"active", "passive"}
            or not isinstance(self.positive_label, bool)
            or not _finite_timestamp(self.event_time)
            or not _valid_scope(self.context_scope)
            or not _nonempty(self.source_artifact_id)
            or self.schema_version != PA_TRAINING_EXAMPLE_SCHEMA_VERSION
        )
        if (
            _rejected3
        ):
            raise TerminalRetrievalContractError("invalid_pa_training_example")
        if self.domain == "active":
            valid_locator = _nonempty(self.query_text_locator)
            no_starter = self.query_source_starter_id is None
            if not valid_locator or not no_starter:
                raise TerminalRetrievalContractError("invalid_pa_training_example")
        elif (
            not _nonempty(self.query_source_starter_id)
            or self.query_text_locator is not None
        ):
            raise TerminalRetrievalContractError("invalid_pa_training_example")

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "query_text_locator": self.query_text_locator,
            "query_source_starter_id": self.query_source_starter_id,
            "domain": self.domain,
            "positive_label": self.positive_label,
            "event_time": self.event_time,
            "context_scope": self.context_scope,
            "source_artifact_id": self.source_artifact_id,
            "schema_version": self.schema_version,
        }

    @property
    def artifact_id(self) -> str:
        return f"pa-training:{self.domain}:{self.source_artifact_id}"

    @classmethod
    def from_payload(cls, payload: Any) -> PATrainingExample:
        if not isinstance(payload, dict) or set(payload) != {
            "user_state_snapshot_id",
            "query_text_locator",
            "query_source_starter_id",
            "domain",
            "positive_label",
            "event_time",
            "context_scope",
            "source_artifact_id",
            "schema_version",
        }:
            raise TerminalRetrievalContractError("invalid_pa_training_example")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class PassiveStarterExample:
    user_state_snapshot_id: str
    context_scope: str
    corpus_version: str
    starter_id: str
    exposure_id: str
    manifest_id: str
    card_revision_id: str
    exposed_at: float
    clicked_at: float | None
    not_interested_at: float | None
    feature_version_refs: Mapping[str, str]
    schema_version: str = PASSIVE_STARTER_EXAMPLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items2 = (
            self.user_state_snapshot_id,
            self.corpus_version,
            self.starter_id,
            self.exposure_id,
            self.manifest_id,
            self.card_revision_id,
        )
        if any(
            not _nonempty(value)
            for value in _value_items2
        ) or not _valid_scope(self.context_scope) or (
            self.schema_version != PASSIVE_STARTER_EXAMPLE_SCHEMA_VERSION
        ):
            raise TerminalRetrievalContractError("invalid_passive_starter_example")
        if not _finite_timestamp(self.exposed_at) or any(
            value is not None and not _finite_timestamp(value)
            for value in (self.clicked_at, self.not_interested_at)
        ):
            raise TerminalRetrievalContractError("invalid_passive_starter_example")
        if (
            not isinstance(self.feature_version_refs, Mapping)
            or not self.feature_version_refs
            or any(
                not _nonempty(key) or not _nonempty(value)
                for key, value in self.feature_version_refs.items()
            )
        ):
            raise TerminalRetrievalContractError("invalid_passive_starter_example")

    def to_training_example(
        self,
        *,
        positive_label: bool,
        event_time: float,
    ) -> PATrainingExample:
        return PATrainingExample(
            user_state_snapshot_id=self.user_state_snapshot_id,
            query_text_locator=None,
            query_source_starter_id=self.starter_id,
            domain="passive",
            positive_label=positive_label,
            event_time=event_time,
            context_scope=self.context_scope,
            source_artifact_id=self.artifact_id,
        )

    @property
    def artifact_id(self) -> str:
        return f"passive-starter:{self.exposure_id}:{self.card_revision_id}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "context_scope": self.context_scope,
            "corpus_version": self.corpus_version,
            "starter_id": self.starter_id,
            "exposure_id": self.exposure_id,
            "manifest_id": self.manifest_id,
            "card_revision_id": self.card_revision_id,
            "exposed_at": self.exposed_at,
            "clicked_at": self.clicked_at,
            "not_interested_at": self.not_interested_at,
            "feature_version_refs": dict(self.feature_version_refs),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> PassiveStarterExample:
        if not isinstance(payload, dict) or set(payload) != {
            "user_state_snapshot_id",
            "context_scope",
            "corpus_version",
            "starter_id",
            "exposure_id",
            "manifest_id",
            "card_revision_id",
            "exposed_at",
            "clicked_at",
            "not_interested_at",
            "feature_version_refs",
            "schema_version",
        } or not isinstance(payload.get("feature_version_refs"), dict):
            raise TerminalRetrievalContractError("invalid_passive_starter_example")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ActiveExpressionObservation:
    """Product outcome retained before the active-training exclusion gate."""

    user_state_snapshot_id: str
    source_locator: str
    observed_at: float
    input_origin: str
    assistance_origin: str
    context_scope: str
    schema_version: str = ACTIVE_EXPRESSION_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items3 = (
            self.user_state_snapshot_id,
            self.source_locator,
            self.input_origin,
            self.assistance_origin,
        )
        if any(
            not _nonempty(value)
            for value in _value_items3
        ) or not _valid_scope(self.context_scope) or (
            self.schema_version != ACTIVE_EXPRESSION_OBSERVATION_SCHEMA_VERSION
        ):
            raise TerminalRetrievalContractError("invalid_active_expression_observation")
        if not _finite_timestamp(self.observed_at):
            raise TerminalRetrievalContractError("invalid_active_expression_observation")

    @property
    def artifact_id(self) -> str:
        return (
            f"active-observation:{self.user_state_snapshot_id}:"
            f"{self.source_locator}"
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "source_locator": self.source_locator,
            "observed_at": self.observed_at,
            "input_origin": self.input_origin,
            "assistance_origin": self.assistance_origin,
            "context_scope": self.context_scope,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ActiveExpressionObservation:
        if not isinstance(payload, dict) or set(payload) != {
            "user_state_snapshot_id",
            "source_locator",
            "observed_at",
            "input_origin",
            "assistance_origin",
            "context_scope",
            "schema_version",
        }:
            raise TerminalRetrievalContractError(
                "invalid_active_expression_observation"
            )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ActiveExpressionExample:
    user_state_snapshot_id: str
    source_locator: str
    observed_at: float
    input_origin: str
    assistance_origin: str
    context_scope: str
    schema_version: str = ACTIVE_EXPRESSION_EXAMPLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.input_origin != "manual_free_text" or self.assistance_origin != "none":
            raise TerminalRetrievalContractError("invalid_active_expression_example")
        _rejected4 = (
            not _nonempty(self.user_state_snapshot_id)
            or not _nonempty(self.source_locator)
            or not _finite_timestamp(self.observed_at)
            or not _valid_scope(self.context_scope)
            or self.schema_version != ACTIVE_EXPRESSION_EXAMPLE_SCHEMA_VERSION
        )
        if (
            _rejected4
        ):
            raise TerminalRetrievalContractError("invalid_active_expression_example")

    def to_training_example(
        self,
        *,
        positive_label: bool,
        event_time: float,
    ) -> PATrainingExample:
        return PATrainingExample(
            user_state_snapshot_id=self.user_state_snapshot_id,
            query_text_locator=self.source_locator,
            query_source_starter_id=None,
            domain="active",
            positive_label=positive_label,
            event_time=event_time,
            context_scope=self.context_scope,
            source_artifact_id=self.artifact_id,
        )

    @property
    def artifact_id(self) -> str:
        return f"active-example:{self.user_state_snapshot_id}:{self.source_locator}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "source_locator": self.source_locator,
            "observed_at": self.observed_at,
            "input_origin": self.input_origin,
            "assistance_origin": self.assistance_origin,
            "context_scope": self.context_scope,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ActiveExpressionExample:
        if not isinstance(payload, dict) or set(payload) != {
            "user_state_snapshot_id",
            "source_locator",
            "observed_at",
            "input_origin",
            "assistance_origin",
            "context_scope",
            "schema_version",
        }:
            raise TerminalRetrievalContractError("invalid_active_expression_example")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ActiveExpressionAdmission:
    observation: ActiveExpressionObservation
    example: ActiveExpressionExample | None
    exclusion_reason: str | None


def admit_active_expression(
    observation: ActiveExpressionObservation,
) -> ActiveExpressionAdmission:
    """Keep the outcome, but only admit natural unassisted manual text."""

    if observation.input_origin != "manual_free_text":
        return ActiveExpressionAdmission(
            observation=observation,
            example=None,
            exclusion_reason="ineligible_input_origin",
        )
    if observation.assistance_origin != "none":
        return ActiveExpressionAdmission(
            observation=observation,
            example=None,
            exclusion_reason="assisted_input_excluded",
        )
    return ActiveExpressionAdmission(
        observation=observation,
        example=ActiveExpressionExample(
            user_state_snapshot_id=observation.user_state_snapshot_id,
            source_locator=observation.source_locator,
            observed_at=observation.observed_at,
            input_origin=observation.input_origin,
            assistance_origin=observation.assistance_origin,
            context_scope=observation.context_scope,
        ),
        exclusion_reason=None,
    )


def _parse_iso_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    if not _nonempty(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def versioned_document_identity(url: str, content: str = "") -> tuple[str, str] | None:
    """Recognize explicit arXiv versions; unversioned URLs cannot bind dates."""
    parsed = urlparse(url)
    if parsed.hostname not in {"arxiv.org", "www.arxiv.org"}:
        return None
    match = re.fullmatch(r"/(?:abs|html|pdf)/(\d{4}\.\d{4,5})v(\d+)(?:\.pdf)?/?", parsed.path)
    if match:
        return f"arxiv:{match[1]}", f"v{match[2]}"
    unversioned = re.fullmatch(r"/abs/(\d{4}\.\d{4,5})/?", parsed.path)
    if unversioned:
        cited = re.search(r"arXiv:" + re.escape(unversioned[1]) + r"v(\d+)\b", content, re.IGNORECASE)
        if cited:
            return f"arxiv:{unversioned[1]}", f"v{cited[1]}"
    return None


def valid_linked_date_page(value: Any, *, body_url: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "date_page_url", "body_url", "document_id", "version", "date_page_excerpt",
        "operation_id", "request_id", "http_status_code",
    }:
        return False
    identity = versioned_document_identity(body_url)
    return bool(
        identity and value["body_url"] == body_url
        and versioned_document_identity(str(value["date_page_url"]), str(value["date_page_excerpt"])) == identity
        and value["date_page_url"] != body_url
        and (value["document_id"], value["version"]) == identity
        and all(_nonempty(value[key]) for key in ("date_page_excerpt", "operation_id", "request_id"))
        and _plain_int(value["http_status_code"]) and 200 <= value["http_status_code"] < 300
    )


@dataclass(frozen=True, slots=True)
class PublicationDateProvenance:
    """Exact origin of one normalized publication timestamp."""

    source_type: str
    source_field: str
    raw_value: str
    search_provider_id: str | None = None
    search_receipt_id: str | None = None
    search_result_url: str | None = None
    linked_page: dict[str, Any] | None = None
    # When the page dates itself, the date the search listing gave the same URL,
    # kept so an old story newly listed can be told apart afterwards.
    listed_at: str | None = None
    # When the listing dates a page that gave itself a later date, the page's raw
    # date, kept so the listing's precedence can be reviewed afterwards.
    page_published_at: str | None = None
    # When the page's own metadata was read from its publisher's detail API instead
    # of the page (msn articles), the API URL the date came from.
    api_url: str | None = None

    def __post_init__(self) -> None:
        _rejected5 = (
            any(
                not _nonempty(value)
                for value in (self.source_type, self.source_field, self.raw_value)
            ) or (self.listed_at is not None and (
                self.source_type != "page_explicit_metadata" or not _nonempty(self.listed_at)
            )) or (self.page_published_at is not None and (
                self.source_type != "structured_search_result" or not _nonempty(self.page_published_at)
            )) or (self.api_url is not None and (
                self.source_type != "page_explicit_metadata" or not _nonempty(self.api_url)
            ))
        )
        if _rejected5:
            raise EvidenceContractError("invalid_publication_date_provenance")
        search_values = (
            self.search_provider_id,
            self.search_receipt_id,
            self.search_result_url,
        )
        if self.source_type == "structured_search_result":
            if any(not _nonempty(value) for value in search_values):
                raise EvidenceContractError("invalid_publication_date_provenance")
        elif self.source_type == "linked_document_metadata":
            if any(value is not None for value in search_values) or not valid_linked_date_page(
                self.linked_page,
                body_url=str((self.linked_page or {}).get("body_url", "")),
            ):
                raise EvidenceContractError("invalid_publication_date_provenance")
        elif self.source_type == "page_explicit_metadata":
            if any(value is not None for value in search_values):
                raise EvidenceContractError("invalid_publication_date_provenance")
        else:
            raise EvidenceContractError("invalid_publication_date_provenance")

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_field": self.source_field,
            "raw_value": self.raw_value,
            "search_provider_id": self.search_provider_id,
            "search_receipt_id": self.search_receipt_id,
            "search_result_url": self.search_result_url,
            **({"linked_page": dict(self.linked_page)} if self.linked_page is not None else {}),
            **({"listed_at": self.listed_at} if self.listed_at is not None else {}),
            **({"page_published_at": self.page_published_at} if self.page_published_at is not None else {}),
            **({"api_url": self.api_url} if self.api_url is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class FrozenDocumentRevision:
    """Fetched page content frozen once for one generation snapshot."""

    document_revision_id: str
    source_url: str
    publisher: str
    source_kind: str
    published_at: str | None
    fetched_at: float
    immutable_content: str
    snapshot_date: str
    snapshot_version: str
    publication_date_provenance: PublicationDateProvenance | None = None

    def __post_init__(self) -> None:
        _value_items4 = (
            self.document_revision_id,
            self.source_url,
            self.publisher,
            self.source_kind,
            self.immutable_content,
            self.snapshot_date,
            self.snapshot_version,
        )
        if any(
            not _nonempty(value)
            for value in _value_items4
        ) or not _finite_timestamp(self.fetched_at):
            raise EvidenceContractError("invalid_frozen_document")
        if self.published_at is not None and _parse_iso_timestamp(
            self.published_at
        ) is None:
            raise EvidenceContractError("invalid_frozen_document")
        if (
            self.published_at is None
            and self.publication_date_provenance is not None
        ):
            raise EvidenceContractError("invalid_frozen_document")
        if (
            self.publication_date_provenance is not None
            and self.publication_date_provenance.search_result_url is not None
            and self.publication_date_provenance.search_result_url != self.source_url
        ):
            raise EvidenceContractError("invalid_frozen_document")

    def as_payload(self, *, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "document_revision_id": self.document_revision_id,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "source_kind": self.source_kind,
            "published_at": self.published_at,
            "publication_date_provenance": (
                self.publication_date_provenance.as_payload()
                if self.publication_date_provenance is not None
                else None
            ),
            "fetched_at": self.fetched_at,
            "snapshot_date": self.snapshot_date,
            "snapshot_version": self.snapshot_version,
        }
        if include_content:
            payload["immutable_content"] = self.immutable_content
        return payload


@dataclass(frozen=True, slots=True)
class FrozenDocumentSnapshot:
    snapshot_date: str
    snapshot_version: str
    documents: tuple[FrozenDocumentRevision, ...]

    def __post_init__(self) -> None:
        if (
            not _nonempty(self.snapshot_date)
            or not _nonempty(self.snapshot_version)
            or not self.documents
        ):
            raise EvidenceContractError("invalid_document_snapshot")
        document_ids = [doc.document_revision_id for doc in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise EvidenceContractError("duplicate_document_revision")
        if any(
            doc.snapshot_date != self.snapshot_date
            or doc.snapshot_version != self.snapshot_version
            for doc in self.documents
        ):
            raise EvidenceContractError("mixed_document_snapshot")

    @property
    def by_id(self) -> dict[str, FrozenDocumentRevision]:
        return {doc.document_revision_id: doc for doc in self.documents}


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    candidate_id: str
    title: str
    primary_query: str
    freshness_class: str
    context_scope: str
    category: str

    def __post_init__(self) -> None:
        _value_items5 = (
            self.candidate_id,
            self.title,
            self.primary_query,
            self.freshness_class,
            self.category,
        )
        if any(
            not _nonempty(value)
            for value in _value_items5
        ) or not _valid_scope(self.context_scope):
            raise EvidenceContractError("invalid_evidence_candidate")
        if self.freshness_class not in {
            "current_event",
            "recent_development",
            "evergreen_corpus",
            NEWS_FRESHNESS_CLASS,
        }:
            raise EvidenceContractError("invalid_freshness_class")

    def as_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "primary_query": self.primary_query,
            "freshness_class": self.freshness_class,
            "context_scope": self.context_scope,
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class ExactEvidenceSpan:
    candidate_id: str
    document_revision_id: str
    source_url: str
    publisher: str
    source_kind: str
    published_at: str | None
    fetched_at: float
    snapshot_date: str
    snapshot_version: str
    start: int
    end: int
    exact_text: str
    retrieved_at: float
    publication_date_provenance: PublicationDateProvenance | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "document_revision_id": self.document_revision_id,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "source_kind": self.source_kind,
            "published_at": self.published_at,
            "publication_date_provenance": (
                self.publication_date_provenance.as_payload()
                if self.publication_date_provenance is not None
                else None
            ),
            "fetched_at": self.fetched_at,
            "snapshot_date": self.snapshot_date,
            "snapshot_version": self.snapshot_version,
            "start": self.start,
            "end": self.end,
            "exact_text": self.exact_text,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True, slots=True)
class ExtractedCandidateEvidence:
    candidate: EvidenceCandidate
    snapshot_date: str
    snapshot_version: str
    spans: tuple[ExactEvidenceSpan, ...]
    component_receipt: LearnedComponentReceipt
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    candidate_id: str
    context_scope: str
    category: str
    freshness_class: str
    snapshot_date: str
    snapshot_version: str
    spans: tuple[ExactEvidenceSpan, ...]
    source_gate_mode: str
    component_receipt: LearnedComponentReceipt
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "context_scope": self.context_scope,
            "category": self.category,
            "freshness_class": self.freshness_class,
            "snapshot_date": self.snapshot_date,
            "snapshot_version": self.snapshot_version,
            "spans": [span.as_payload() for span in self.spans],
            "source_gate_mode": self.source_gate_mode,
            "component_receipt": self.component_receipt.as_payload(),
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class EvidenceOffset:
    document_revision_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _rejected6 = (
            not _nonempty(self.document_revision_id)
            or not _plain_int(self.start)
            or not _plain_int(self.end)
            or self.start < 0
            or self.end <= self.start
        )
        if (
            _rejected6
        ):
            raise EvidenceContractError("invalid_heldout_offset")


@dataclass(frozen=True, slots=True)
class EvidenceHeldOutCase:
    """One manually exact-reviewed Chinese evidence evaluation case."""

    case_id: str
    fixture_version: str
    candidate: EvidenceCandidate
    snapshot: FrozenDocumentSnapshot
    gold_offsets: tuple[EvidenceOffset, ...]
    gold_should_admit: bool
    gold_review_status: str

    def __post_init__(self) -> None:
        if not _nonempty(self.case_id) or not _nonempty(self.fixture_version):
            raise EvidenceContractError("invalid_heldout_case")
        if self.gold_review_status != "human_exact_verified":
            raise EvidenceContractError("heldout_gold_not_human_verified")
        if not isinstance(self.gold_should_admit, bool):
            raise EvidenceContractError("invalid_heldout_case")
        offset_keys = [
            (offset.document_revision_id, offset.start, offset.end)
            for offset in self.gold_offsets
        ]
        if len(offset_keys) != len(set(offset_keys)):
            raise EvidenceContractError("invalid_heldout_case")
        documents = self.snapshot.by_id
        for offset in self.gold_offsets:
            document = documents.get(offset.document_revision_id)
            if (
                document is None
                or offset.end > len(document.immutable_content)
                or not document.immutable_content[offset.start: offset.end]
            ):
                raise EvidenceContractError("invalid_heldout_gold_offset")


@dataclass(frozen=True, slots=True)
class EvidenceHeldOutPrediction:
    case_id: str
    predicted_offsets: tuple[EvidenceOffset, ...]
    admitted: bool
    latency_ms: float
    provider_cost: float

    def __post_init__(self) -> None:
        _rejected7 = (
            not _nonempty(self.case_id)
            or not isinstance(self.admitted, bool)
            or type(self.latency_ms) not in (int, float)
            or not math.isfinite(float(self.latency_ms))
            or self.latency_ms < 0
            or type(self.provider_cost) not in (int, float)
            or not math.isfinite(float(self.provider_cost))
            or self.provider_cost < 0
        )
        if (
            _rejected7
        ):
            raise EvidenceContractError("invalid_heldout_prediction")
        keys = [
            (offset.document_revision_id, offset.start, offset.end)
            for offset in self.predicted_offsets
        ]
        if len(keys) != len(set(keys)):
            raise EvidenceContractError("invalid_heldout_prediction")


@dataclass(frozen=True, slots=True)
class EvidenceHeldOutMetrics:
    fixture_version: str
    case_count: int
    exact_offset_validity: float
    span_precision: float
    span_recall: float
    span_f1: float
    no_support_rejection: float
    admission_precision: float
    admission_coverage: float
    mean_latency_ms: float
    total_provider_cost: float


def _ratio(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    return numerator / denominator if denominator else empty



EvidenceExtractor = Callable[[dict[str, Any]], Awaitable[Any]]


def _host_exact_offset_catalog(content: str) -> list[dict[str, Any]]:
    """Expose copyable host-computed boundaries without selecting evidence."""

    catalog: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(content):
        while cursor < len(content) and content[cursor].isspace():
            cursor += 1
        if cursor >= len(content):
            break
        hard_end = min(cursor + OFFSET_CATALOG_MAX_CHARS, len(content))
        window = content[cursor:hard_end]
        boundaries = [
            window.find(marker) + len(marker)
            for marker in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ")
            if window.find(marker) >= 0
        ]
        end = cursor + min(boundaries) if boundaries else hard_end
        while end > cursor and content[end - 1].isspace():
            end -= 1
        if end <= cursor:
            end = hard_end
        catalog.append(
            {
                "start": cursor,
                "end": end,
                "exact_text": content[cursor:end],
            }
        )
        cursor = end
    return catalog


def _published_at_is_fresh(
    published_at: str | None,
    *,
    freshness_class: str,
    now: float,
) -> bool:
    if freshness_class == "evergreen_corpus":
        return True
    published_timestamp = _parse_iso_timestamp(published_at)
    if published_timestamp is None:
        return False
    age_seconds = now - published_timestamp
    if freshness_class == NEWS_FRESHNESS_CLASS:
        return 0 <= age_seconds <= NEWS_MAX_WINDOW_HOURS * 60 * 60
    max_days = 7 if freshness_class == "current_event" else 90
    return 0 <= age_seconds <= max_days * 24 * 60 * 60


def eligible_document_revision_ids(
    candidate: EvidenceCandidate,
    documents: Sequence[FrozenDocumentRevision],
    *,
    now: float,
) -> tuple[str, ...]:
    """Apply the source/freshness gate before spending an extractor call."""

    if not _finite_timestamp(now):
        raise EvidenceContractError("evidence_admission_input_failed")
    frozen = tuple(documents)
    if candidate.freshness_class == "evergreen_corpus":
        return tuple(
            document.document_revision_id
            for document in frozen
            if document.source_kind in {"authoritative", "original", "stable"}
        )
    fresh_documents: list[FrozenDocumentRevision] = []
    for document in frozen:
        if not _published_at_is_fresh(
            document.published_at,
            freshness_class=candidate.freshness_class,
            now=now,
        ):
            continue
        fresh_documents.append(document)
    fresh = tuple(fresh_documents)
    if candidate.freshness_class == NEWS_FRESHNESS_CLASS:
        return tuple(
            document.document_revision_id
            for document in fresh
            if document.source_kind == NEWS_SOURCE_KIND
        )
    primary = tuple(
        document
        for document in fresh
        if document.source_kind in {"authoritative", "original"}
    )
    if primary:
        return tuple(document.document_revision_id for document in primary)
    reports: list[str] = []
    publishers: set[str] = set()
    for document in fresh:
        if (
            document.source_kind != "reliable_report"
            or document.publisher in publishers
        ):
            continue
        reports.append(document.document_revision_id)
        publishers.add(document.publisher)
        if len(reports) == 2:
            return tuple(reports)
    return ()


def _required_source_gate_mode(
    candidate: EvidenceCandidate,
    documents: Sequence[FrozenDocumentRevision],
) -> str:
    if candidate.freshness_class == "evergreen_corpus":
        return "single_stable_source"
    if candidate.freshness_class == NEWS_FRESHNESS_CLASS:
        return "single_news_report"
    if any(
        document.source_kind in {"authoritative", "original"}
        for document in documents
    ):
        return "single_primary_source"
    return "two_independent_reports"


async def extract_candidate_evidence(
    *,
    frozen_candidate: EvidenceCandidate,
    frozen_documents: FrozenDocumentSnapshot,
    allowed_document_revision_ids: Sequence[str] | None = None,
    input_snapshot_id: str,
    request_id: str,
    provider_binding: LearnedComponentBinding,
    invoke_extractor: EvidenceExtractor,
    retrieved_at: float,
) -> ExtractedCandidateEvidence:
    """Select exact offsets for one candidate within one frozen snapshot."""

    _rejected = (
        not _nonempty(input_snapshot_id)
        or not _nonempty(request_id)
        or not _finite_timestamp(retrieved_at)
        or provider_binding.component_id != EVIDENCE_COMPONENT_ID
    )
    if (
        _rejected
    ):
        raise EvidenceContractError("evidence_input_contract_failed")
    if provider_binding.provider_id != EVIDENCE_API_PROVIDER_ID:
        raise EvidenceContractError("evidence_provider_contract_failed")

    snapshot_documents_by_id = frozen_documents.by_id
    allowed_ids = tuple(
        allowed_document_revision_ids
        if allowed_document_revision_ids is not None
        else snapshot_documents_by_id
    )
    if (
        not allowed_ids
        or len(allowed_ids) != len(set(allowed_ids))
        or any(document_id not in snapshot_documents_by_id for document_id in allowed_ids)
    ):
        raise EvidenceContractError("evidence_input_contract_failed")
    allowed_documents = tuple(
        snapshot_documents_by_id[document_id] for document_id in allowed_ids
    )

    def invoked_failure(reason: str) -> EvidenceContractError:
        return EvidenceContractError(
            reason,
            component_receipt=_failed_component_receipt(
                binding=provider_binding,
                input_snapshot_id=input_snapshot_id,
                request_id=request_id,
                reason=reason,
            ),
        )

    offset_catalog_by_id = {
        document.document_revision_id: _host_exact_offset_catalog(
            document.immutable_content
        )
        for document in allowed_documents
    }
    request_payload = {
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "request_id": request_id,
        "input_snapshot_id": input_snapshot_id,
        "candidate_id": frozen_candidate.candidate_id,
        "candidate": frozen_candidate.as_payload(),
        "snapshot_date": frozen_documents.snapshot_date,
        "snapshot_version": frozen_documents.snapshot_version,
        "snapshot_document_revision_ids": [
            document.document_revision_id for document in frozen_documents.documents
        ],
        "document_revision_ids": list(allowed_ids),
        "required_source_gate_mode": _required_source_gate_mode(
            frozen_candidate,
            allowed_documents,
        ),
        "documents": [
            {
                **document.as_payload(include_content=True),
                "offset_catalog": offset_catalog_by_id[
                    document.document_revision_id
                ],
            }
            for document in allowed_documents
        ],
        "provider_binding": provider_binding.as_provenance(),
    }
    try:
        response = await invoke_extractor(request_payload)
    except EvidenceContractError as exc:
        reason = (
            exc.reason
            if exc.reason == "evidence_output_contract_failed"
            else "evidence_provider_invocation_failed"
        )
        raise invoked_failure(reason) from exc
    except Exception as exc:  # noqa: BLE001 - provider failures fail closed
        raise invoked_failure("evidence_provider_invocation_failed") from exc
    if not isinstance(response, dict):
        raise invoked_failure("evidence_output_contract_failed")
    top_level_extras = set(response) - {"candidate_id", "spans"}
    if top_level_extras.intersection({"snippet", "title", "text", "span_text"}):
        raise invoked_failure("snippet_or_title_not_evidence")
    if set(response) != {"candidate_id", "spans"} or not isinstance(
        response.get("spans"), list
    ):
        raise invoked_failure("evidence_output_contract_failed")
    if response.get("candidate_id") != frozen_candidate.candidate_id:
        raise invoked_failure("candidate_mismatch")
    if not response["spans"]:
        raise invoked_failure("no_candidate_support")

    documents_by_id = {
        document.document_revision_id: document for document in allowed_documents
    }
    spans: list[ExactEvidenceSpan] = []
    seen_offsets: set[tuple[str, int, int]] = set()
    for raw in response["spans"]:
        if not isinstance(raw, dict):
            raise invoked_failure("evidence_output_contract_failed")
        extras = set(raw) - {"document_revision_id", "start", "end"}
        if extras.intersection(
            {"snippet", "title", "text", "span_text", "source"}
        ):
            raise invoked_failure("snippet_or_title_not_evidence")
        if set(raw) != {"document_revision_id", "start", "end"}:
            raise invoked_failure("evidence_output_contract_failed")
        document_id = raw.get("document_revision_id")
        start = raw.get("start")
        end = raw.get("end")
        if not _nonempty(document_id) or document_id not in documents_by_id:
            raise invoked_failure("unknown_document_revision")
        if not _plain_int(start) or not _plain_int(end):
            raise invoked_failure("invalid_evidence_offsets")
        if start == end:
            raise invoked_failure("empty_evidence_span")
        document = documents_by_id[document_id]
        _rejected8 = (
            start < 0 or end < 0 or start > end or end > len(
                document.immutable_content
            )
        )
        if _rejected8:
            raise invoked_failure("invalid_evidence_offsets")
        if start == 0 and end == LEGACY_PREFIX_CHARACTER_COUNT:
            raise invoked_failure("legacy_prefix_not_evidence")
        if (start, end) not in {
            (entry["start"], entry["end"])
            for entry in offset_catalog_by_id[document_id]
        }:
            raise invoked_failure("invalid_evidence_offsets")
        offset_key = (document_id, start, end)
        if offset_key in seen_offsets:
            raise invoked_failure("duplicate_evidence_span")
        seen_offsets.add(offset_key)
        exact_text = document.immutable_content[start:end]
        if not exact_text:
            raise invoked_failure("empty_evidence_span")
        spans.append(
            ExactEvidenceSpan(
                candidate_id=frozen_candidate.candidate_id,
                document_revision_id=document.document_revision_id,
                source_url=document.source_url,
                publisher=document.publisher,
                source_kind=document.source_kind,
                published_at=document.published_at,
                fetched_at=document.fetched_at,
                snapshot_date=document.snapshot_date,
                snapshot_version=document.snapshot_version,
                start=start,
                end=end,
                exact_text=exact_text,
                retrieved_at=retrieved_at,
                publication_date_provenance=(
                    document.publication_date_provenance
                ),
            )
        )

    receipt = LearnedComponentReceipt(
        binding=provider_binding,
        input_snapshot_id=input_snapshot_id,
        request_id=request_id,
        score_semantics="uncalibrated_api_proxy",
        output_status="success",
        fallback_reason=None,
    )
    return ExtractedCandidateEvidence(
        candidate=frozen_candidate,
        snapshot_date=frozen_documents.snapshot_date,
        snapshot_version=frozen_documents.snapshot_version,
        spans=tuple(spans),
        component_receipt=receipt,
    )


def _fresh_for_candidate(
    span: ExactEvidenceSpan,
    *,
    freshness_class: str,
    now: float,
) -> bool:
    return _published_at_is_fresh(
        span.published_at,
        freshness_class=freshness_class,
        now=now,
    )


def admit_candidate_evidence(
    extracted: ExtractedCandidateEvidence,
    *,
    now: float,
) -> EvidenceBundle:
    """Apply date and source gates independently after exact-span validation."""

    if not _finite_timestamp(now):
        raise EvidenceContractError("evidence_admission_input_failed")
    candidate = extracted.candidate
    fresh_span_items: list[ExactEvidenceSpan] = []
    for span in extracted.spans:
        if not _fresh_for_candidate(
            span,
            freshness_class=candidate.freshness_class,
            now=now,
        ):
            continue
        fresh_span_items.append(span)
    fresh_spans = tuple(fresh_span_items)
    if not fresh_spans:
        raise EvidenceContractError("freshness_gate_failed")

    selected: tuple[ExactEvidenceSpan, ...]
    source_gate_mode: str
    if candidate.freshness_class == "evergreen_corpus":
        stable = tuple(
            span
            for span in fresh_spans
            if span.source_kind in {"authoritative", "original", "stable"}
        )
        if not stable:
            raise EvidenceContractError("source_gate_failed")
        selected = (stable[0],)
        source_gate_mode = "single_stable_source"
    elif candidate.freshness_class == NEWS_FRESHNESS_CLASS:
        news = tuple(
            span for span in fresh_spans if span.source_kind == NEWS_SOURCE_KIND
        )
        if not news:
            raise EvidenceContractError("source_gate_failed")
        selected = (news[0],)
        source_gate_mode = "single_news_report"
    else:
        primary = tuple(
            span
            for span in fresh_spans
            if span.source_kind in {"authoritative", "original"}
        )
        if primary:
            selected = (primary[0],)
            source_gate_mode = "single_primary_source"
        else:
            reports: list[ExactEvidenceSpan] = []
            publishers: set[str] = set()
            documents: set[str] = set()
            for span in fresh_spans:
                if span.source_kind != "reliable_report":
                    continue
                if span.publisher in publishers:
                    continue
                if span.document_revision_id in documents:
                    continue
                reports.append(span)
                publishers.add(span.publisher)
                documents.add(span.document_revision_id)
                if len(reports) == 2:
                    break
            if len(reports) != 2:
                raise EvidenceContractError("source_gate_failed")
            selected = tuple(reports)
            source_gate_mode = "two_independent_reports"

    return EvidenceBundle(
        candidate_id=candidate.candidate_id,
        context_scope=candidate.context_scope,
        category=candidate.category,
        freshness_class=candidate.freshness_class,
        snapshot_date=extracted.snapshot_date,
        snapshot_version=extracted.snapshot_version,
        spans=selected,
        source_gate_mode=source_gate_mode,
        component_receipt=extracted.component_receipt,
    )


@dataclass(frozen=True, slots=True)
class FixedStarterRecord:
    """One immutable, versioned entry in the prebuilt starter corpus."""

    starter_id: str
    title: str
    text: str
    topic: str
    hook_type: str
    freshness_class: str
    origin: str
    category: str
    context_scopes: tuple[str, ...]
    corpus_version: str

    def __post_init__(self) -> None:
        _value_items = (
            self.starter_id,
            self.title,
            self.text,
            self.topic,
            self.hook_type,
            self.freshness_class,
            self.origin,
            self.category,
            self.corpus_version,
        )
        if any(
            not _nonempty(value)
            for value in _value_items
        ):
            raise TerminalRetrievalContractError(
                "invalid_corpus_record", "record fields must be non-empty"
            )
        if (
            not self.context_scopes
            or len(set(self.context_scopes)) != len(self.context_scopes)
            or any(not _valid_scope(scope) for scope in self.context_scopes)
        ):
            raise TerminalRetrievalContractError(
                "invalid_corpus_record", "context scopes must be unique and valid"
            )
        if (
            "global_new_chat" in self.context_scopes
            and self.category not in GLOBAL_FIXED_CATEGORIES
        ):
            raise TerminalRetrievalContractError(
                "wrong_category",
                "global fixed starters require one of the two v6 product categories",
            )


@dataclass(frozen=True, slots=True)
class FrozenStarterCorpus:
    """One immutable corpus version used for both ranking and materialization."""

    corpus_version: str
    starters: tuple[FixedStarterRecord, ...]

    def __post_init__(self) -> None:
        if not _nonempty(self.corpus_version) or not self.starters:
            raise TerminalRetrievalContractError("invalid_corpus")
        ids = [starter.starter_id for starter in self.starters]
        if len(ids) != len(set(ids)):
            raise TerminalRetrievalContractError("duplicate_corpus_id")
        if {starter.corpus_version for starter in self.starters} != {
            self.corpus_version
        }:
            raise TerminalRetrievalContractError("cross_version_corpus")

    @property
    def by_id(self) -> dict[str, FixedStarterRecord]:
        return {starter.starter_id: starter for starter in self.starters}


@dataclass(frozen=True, slots=True)
class RankedStarterInterest:
    interest_summary_id: str
    interest_summary: str
    rank: int

    def __post_init__(self) -> None:
        _rejected9 = (
            not _nonempty(self.interest_summary_id)
            or not _nonempty(self.interest_summary)
            or not _plain_int(self.rank)
            or self.rank < 1
        )
        if (
            _rejected9
        ):
            raise TerminalRetrievalContractError("invalid_ranked_interest")


@dataclass(frozen=True, slots=True)
class FixedStarterHit:
    """Provider-ranked ID only; corpus text is deliberately absent."""

    starter_id: str
    interest_summary_id: str | None
    rank: int
    component_receipt: LearnedComponentReceipt


@dataclass(frozen=True, slots=True)
class FixedStarterRetrievalBatch:
    user_state_snapshot_id: str
    context_scope: str
    corpus_version: str
    hits: tuple[FixedStarterHit, ...]
    component_receipt: LearnedComponentReceipt
    ignored_provider_fields: tuple[str, ...]
    contract_version: str = PA_RETRIEVAL_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class MaterializedFixedStarter:
    starter_id: str
    interest_summary_id: str | None
    rank: int
    title: str
    text: str
    topic: str
    hook_type: str
    freshness_class: str
    origin: str
    category: str
    context_scope: str
    corpus_version: str
    component_receipt: LearnedComponentReceipt


@dataclass(frozen=True, slots=True)
class FixedStarterMaterializationBatch:
    user_state_snapshot_id: str
    context_scope: str
    corpus_version: str
    candidates: tuple[MaterializedFixedStarter, ...]
    component_receipt: LearnedComponentReceipt
    materialized_stage: str = "before_i4"


@dataclass(frozen=True, slots=True)
class I4FixedStarterCandidate:
    starter_id: str
    interest_summary_id: str | None
    rank: int
    title: str
    text: str
    topic: str
    hook_type: str
    freshness_class: str
    origin: str
    category: str
    context_scope: str
    corpus_version: str
    primary_query: str
    persona_context: str
    provenance: Mapping[str, Any]
    component_receipt: LearnedComponentReceipt


FixedStarterRetriever = Callable[[dict[str, Any]], Awaitable[Any]]


def _validate_interests(
    interests: Sequence[RankedStarterInterest],
) -> tuple[RankedStarterInterest, ...]:
    frozen = tuple(interests)
    ids = [interest.interest_summary_id for interest in frozen]
    ranks = [interest.rank for interest in frozen]
    if len(ids) != len(set(ids)) or sorted(ranks) != list(
        range(1, len(ranks) + 1)
    ):
        raise TerminalRetrievalContractError("invalid_ranked_interests")
    return frozen


async def retrieve_fixed_starters(
    *,
    user_state_snapshot_id: str,
    context_scope: str,
    ranked_interests: Sequence[RankedStarterInterest],
    corpus: FrozenStarterCorpus,
    corpus_version: str,
    provider_binding: LearnedComponentBinding,
    invoke_retriever: FixedStarterRetriever,
    request_id: str,
    top_k: int,
    fallback_reason: str | None = None,
) -> FixedStarterRetrievalBatch:
    """Rank corpus IDs while retaining all text authority in the host."""

    _rejected2 = (
        not _nonempty(user_state_snapshot_id)
        or not _valid_scope(context_scope)
        or not _nonempty(request_id)
        or not _plain_int(top_k)
        or top_k < 1
        or corpus.corpus_version != corpus_version
    )
    if (
        _rejected2
    ):
        raise TerminalRetrievalContractError("retrieval_input_contract_failed")
    interests = _validate_interests(ranked_interests)
    if provider_binding.component_id != PA_RETRIEVER_COMPONENT_ID:
        raise TerminalRetrievalContractError("wrong_provider_component")
    if provider_binding.provider_id != PA_API_FALLBACK_PROVIDER_ID:
        raise TerminalRetrievalContractError("wrong_api_fallback_provider")

    def invoked_failure(reason: str) -> TerminalRetrievalContractError:
        return TerminalRetrievalContractError(
            reason,
            component_receipt=_failed_component_receipt(
                binding=provider_binding,
                input_snapshot_id=user_state_snapshot_id,
                request_id=request_id,
                reason=reason,
            ),
        )

    eligible = tuple(
        starter for starter in corpus.starters if context_scope in starter.context_scopes
    )
    if not eligible:
        raise TerminalRetrievalContractError("no_scope_eligible_starters")

    request_payload = {
        "contract_version": PA_RETRIEVAL_CONTRACT_VERSION,
        "request_id": request_id,
        "input_snapshot_id": user_state_snapshot_id,
        "user_state_snapshot_id": user_state_snapshot_id,
        "context_scope": context_scope,
        "corpus_version": corpus_version,
        "provider_binding": provider_binding.as_provenance(),
        "top_k": top_k,
        "ranked_interests": [
            {
                "interest_summary_id": interest.interest_summary_id,
                "interest_summary": interest.interest_summary,
                "rank": interest.rank,
            }
            for interest in interests
        ],
        "corpus": [
            {
                "starter_id": starter.starter_id,
                "title": starter.title,
                "text": starter.text,
                "topic": starter.topic,
                "hook_type": starter.hook_type,
                "freshness_class": starter.freshness_class,
                "category": starter.category,
            }
            for starter in eligible
        ],
    }
    try:
        response = await invoke_retriever(request_payload)
    except Exception as exc:  # noqa: BLE001 - provider failures fail closed
        raise invoked_failure("provider_invocation_failed") from exc
    with provider_validation(invoke_retriever, response):
        if not isinstance(response, dict) or set(response) != {"hits"} or not isinstance(
            response.get("hits"), list
        ):
            raise invoked_failure("retrieval_output_contract_failed")
        if len(response["hits"]) > min(top_k, len(eligible)):
            raise invoked_failure("retrieval_output_exceeds_top_k")

        eligible_by_id = {starter.starter_id: starter for starter in eligible}
        interest_ids = {interest.interest_summary_id for interest in interests}
        seen: set[str] = set()
        hit_rows: list[tuple[str, str | None, int]] = []
        ignored_fields: list[str] = []
        for rank, raw in enumerate(response["hits"], start=1):
            if not isinstance(raw, dict):
                raise invoked_failure("retrieval_output_contract_failed")
            required = {"starter_id", "interest_summary_id"}
            extras = set(raw) - required
            if not required.issubset(raw) or not extras.issubset(
                _IGNORED_PROVIDER_TEXT_FIELDS
            ):
                raise invoked_failure("retrieval_output_contract_failed")
            starter_id = raw.get("starter_id")
            interest_id = raw.get("interest_summary_id")
            if not _nonempty(starter_id) or starter_id not in eligible_by_id:
                raise invoked_failure("unknown_or_wrong_scope_starter_id")
            if starter_id in seen:
                raise invoked_failure("duplicate_starter_id")
            if interests:
                if interest_id not in interest_ids:
                    raise invoked_failure("unknown_interest_summary_id")
            elif interest_id is not None:
                raise invoked_failure("cold_hit_must_not_claim_interest")
            seen.add(starter_id)
            ignored_fields.extend(
                f"hits[{rank - 1}].{field}" for field in sorted(extras)
            )
            hit_rows.append(
                (cast(str, starter_id), cast(str | None, interest_id), rank)
            )

    fallback_reasons = [fallback_reason] if fallback_reason is not None else []
    if ignored_fields:
        fallback_reasons.append(
            "provider_output_fields_ignored:" + ",".join(ignored_fields)
        )
    effective_fallback_reason = (
        ";".join(fallback_reasons) if fallback_reasons else None
    )
    output_status = (
        "fallback" if effective_fallback_reason is not None else "success"
    )
    receipt = LearnedComponentReceipt(
        binding=provider_binding,
        input_snapshot_id=user_state_snapshot_id,
        request_id=request_id,
        score_semantics=(
            "not_scored" if output_status == "fallback" else "uncalibrated_api_proxy"
        ),
        output_status=output_status,
        fallback_reason=effective_fallback_reason,
    )
    hits = tuple(
        FixedStarterHit(
            starter_id=starter_id,
            interest_summary_id=interest_id,
            rank=rank,
            component_receipt=receipt,
        )
        for starter_id, interest_id, rank in hit_rows
    )
    return FixedStarterRetrievalBatch(
        user_state_snapshot_id=user_state_snapshot_id,
        context_scope=context_scope,
        corpus_version=corpus_version,
        hits=hits,
        component_receipt=receipt,
        ignored_provider_fields=tuple(ignored_fields),
    )


