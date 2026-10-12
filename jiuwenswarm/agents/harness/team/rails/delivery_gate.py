# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Checks a submission-facing draft must pass before it is called ready.

Each check is decided from the draft and the records handed in with it.
None of them calls a model.

`citation_keys_resolve`
    Every citation key in the prose must have a bibliography entry, and every
    bibliography entry must carry a DOI or an arXiv id. A key with no entry is
    a dangling citation. An entry with neither identifier cannot be re-checked,
    so it is not a verified reference.

`numbers_trace_to_registry`
    A number printed in the prose must appear in the verified registry that the
    experiment stage recorded. The registry is a mapping of the printed token to
    the artifact it was measured from. A number with no entry has no measurement
    behind it.

`no_residue`
    Submission-facing text must not still contain a TODO, a placeholder, an
    unresolved author slot, or a claim-support label that GovernanceReviewRail
    asks the agent to keep as a working note. These are the same residue marks the rigor floor
    scans for at generation time; this gate repeats the check at delivery, where
    a miss becomes a refusal rather than a prompt note.

`figure_panels_match` (when the figures are given)
    A caption and the prose that cites a figure must name the panels the figure
    has and no others. A caption of a multi-panel figure describes each panel by
    its letter, (a), (b), ...; a letter beyond the last panel, in a caption, in
    "panel c" or in Figure~\ref{label}(c), points at nothing the reader can see.
    Each figure is given as {"label", "caption", "panels"}, "panels" being the
    number of panels the figure was drawn with.

`references_resolve` (when a resolver is given)
    A well-formed DOI is not evidence that the paper exists: a model writes a
    plausible identifier for an invented reference as easily as for a real one.
    Each cited reference's DOI (an arXiv id is written as its arXiv DOI) is
    looked up in the DOI registry. An identifier the registry does not know is
    a finding; one that could not be looked up is a finding too, because an
    unchecked reference is not a verified one. Delivery waits for the answer
    instead of guessing it.

`auditor_certified` (when a certificate is given)
    The checks above find nothing in a draft only if they could have found
    something. The certificate from `auditor_certificate.certify()` says whether
    the review stage's auditor detects planted value edits beyond chance (its
    e-process reached 1/alpha) and bounds how often it accuses genuine values.
    An uncertified auditor's clean report is not evidence, so it is a finding.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


_CITE = re.compile(r"\\cite[a-zA-Z*]*\{([^}]*)\}")
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_ARXIV = re.compile(r"\b(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?%?")
# "Support level:" and "Gate decision:" are the labels GovernanceReviewRail asks a
# writer for; the last group is a model writer talking about its own instructions
# ("the prompt supplied the counts"). All are working notes, not paper.
_RESIDUE = re.compile(
    r"\b(TODO|FIXME|TBD|XXX|placeholder|lorem ipsum|Author [A-E]\b|Support level:|Gate decision:"
    r"|prompt supplied|supplied (?:materials|facts)|(?:the|this) current (?:revision|draft)|this revision"
    r"|as instructed)",
    re.I,
)


_LETTERS = "abcdefgh"
_CAPTION_PANEL = re.compile(r"\(([a-h])\)")
_PROSE_PANELS = re.compile(r"\bpanels?\s+(\(?[a-h]\b\)?(?:\s*(?:,|and|&|to)\s*\(?[a-h]\b\)?)*)", re.I)


class DeliveryFinding:
    """One reason the draft is not ready, with the text that triggered it.

    A plain class so the review entry can load this file by path on Python 3.9.
    """

    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail

    def render(self) -> str:
        return f"{self.check}: {self.detail}"


def _cite_keys(prose: str) -> list[str]:
    keys: list[str] = []
    for group in _CITE.findall(prose):
        keys.extend(k.strip() for k in group.split(",") if k.strip())
    return keys


def citation_keys_resolve(prose: str, bibliography: dict[str, str]) -> list[DeliveryFinding]:
    """Return one finding per citation key that is dangling or unverifiable."""
    findings: list[DeliveryFinding] = []
    for key in dict.fromkeys(_cite_keys(prose)):   # one finding per key, not per citation
        entry = bibliography.get(key)
        if entry is None:
            findings.append(DeliveryFinding(
                "dangling_citation", f"{key} is cited and has no bibliography entry",
            ))
            continue
        if not (_DOI.search(entry) or _ARXIV.search(entry)):
            findings.append(DeliveryFinding(
                "unverified_reference",
                f"{key} has neither a DOI nor an arXiv id, so it cannot be re-checked",
            ))
    return findings


def _bare(token: str) -> str:
    """The number in a printed token: "12\\%", "12%" and "12" are one value."""
    return token.replace("\\%", "").rstrip("%")


def numbers_trace_to_registry(prose: str, registry: dict[str, str]) -> list[DeliveryFinding]:
    """Return one finding per printed number absent from the verified registry.

    `registry` maps the exact printed token ("0.857", "12%") to the artifact ref
    it was measured from. An empty ref is treated as absent: a number that names
    no artifact is not traced.
    """
    findings: list[DeliveryFinding] = []
    seen: set[str] = set()
    # A registry written from LaTeX holds "0.28\\%" where the prose token is
    # "0.28"; compared raw, every escaped percentage reads as untraced. Compare
    # the number itself.
    traced = {_bare(k) for k, ref in registry.items() if ref}
    for token in _NUMBER.findall(prose):
        if token in seen:
            continue
        seen.add(token)
        if _bare(token) not in traced:
            findings.append(DeliveryFinding(
                "untraced_number",
                f"{token} is printed and has no entry in the verified registry",
            ))
    return findings


def no_residue(prose: str) -> list[DeliveryFinding]:
    """Return one finding per residue mark still in the draft."""
    return [
        DeliveryFinding("residue", f"{m.group(0)} left in submission-facing text")
        for m in _RESIDUE.finditer(prose)
    ]


def figure_panels_match(figures: list[dict], prose: str) -> list[DeliveryFinding]:
    """One finding per undescribed panel and per panel letter that names no panel."""
    findings: list[DeliveryFinding] = []
    for fig in figures:
        n = fig["panels"] if fig["panels"] > 1 else 0   # a single-panel figure has no lettered panels
        named = set(_CAPTION_PANEL.findall(fig["caption"]))
        findings += [DeliveryFinding("missing_panel_description",
                                     f"the caption of {fig['label']} does not describe panel ({x})")
                     for x in _LETTERS[:n] if x not in named]
        findings += [DeliveryFinding("nonexistent_panel",
                                     f"the caption of {fig['label']} names panel ({x}); the figure has "
                                     f"{n or 'no lettered'} panels")
                     for x in sorted(named) if x not in _LETTERS[:n]]
        cited = re.findall(r"\\ref\{%s\}\(?([a-h])\b" % re.escape(fig["label"]), prose)
        findings += [DeliveryFinding("nonexistent_panel",
                                     f"the prose cites {fig['label']}({x}); the figure has {n or 'no lettered'} panels")
                     for x in sorted(set(cited)) if x not in _LETTERS[:n]]
    # "panel c" with no figure named: wrong only when no figure has a panel c.
    most = max((f["panels"] for f in figures), default=0)
    loose = {x.lower() for m in _PROSE_PANELS.findall(prose) for x in re.findall(r"\b([a-h])\b", m, re.I)}
    findings += [DeliveryFinding("nonexistent_panel", f"the prose names panel {x}; no figure has one")
                 for x in sorted(loose) if most < 2 or x not in _LETTERS[:most]]
    return findings


def reference_identifier(entry: str) -> str | None:
    """The DOI in a bibliography entry, else its arXiv id written as the arXiv DOI."""
    m = _DOI.search(entry)
    if m:
        return m.group(0).rstrip(".,;}")
    m = _ARXIV.search(entry)
    if m:
        arxiv_id = re.sub(r"v\d+$", "", re.sub(r"(?i)^arxiv:", "", m.group(0)))
        return f"10.48550/arXiv.{arxiv_id}"
    return None


def _http_status(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "jiuwenswarm-delivery-gate"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def doi_registry_resolver(
    status: Callable[[str], int] = _http_status,
    cache: dict[str, bool] | None = None,
) -> Callable[[str], bool | None]:
    """A resolve(identifier) that asks the DOI registry itself.

    The registry (doi.org's handle API) is the authority on whether a DOI
    exists. A bibliographic index is not: checking the submitted paper's
    references against OpenAlex reported three real 2025 arXiv papers as
    unknown, because the index had not recorded their arXiv DOIs. The registry
    knew all three. An index answers "we have not seen it"; the registry answers
    "it does not exist", which is the question this gate asks.

    Returns True for a registered DOI (HTTP 200), False for an unknown one
    (HTTP 404), None when the registry could not be asked. Definite answers go
    into `cache`, so a report can be rebuilt offline from the same answers.
    """
    answers = {} if cache is None else cache

    def resolve(identifier: str) -> bool | None:
        if identifier in answers:
            return answers[identifier]
        url = "https://doi.org/api/handles/" + urllib.parse.quote(identifier, safe="/")
        try:
            code = status(url)
        except OSError:
            return None
        found = True if code == 200 else False if code == 404 else None
        if found is not None:
            answers[identifier] = found
        return found

    return resolve


def references_resolve(
    prose: str,
    bibliography: dict[str, str],
    resolve: Callable[[str], bool | None],
) -> list[DeliveryFinding]:
    """One finding per cited reference whose identifier names no record, or was not checked.

    References with no entry or no identifier are already reported by
    citation_keys_resolve and are not looked up here.
    """
    findings: list[DeliveryFinding] = []
    for key in dict.fromkeys(_cite_keys(prose)):
        entry = bibliography.get(key)
        identifier = reference_identifier(entry) if entry else None
        if identifier is None:
            continue
        found = resolve(identifier)
        if found is False:
            findings.append(DeliveryFinding(
                "unresolvable_reference",
                f"{key}: {identifier} is not a registered DOI",
            ))
        elif found is None:
            findings.append(DeliveryFinding(
                "reference_not_checked",
                f"{key}: {identifier} could not be looked up; delivery waits for the answer",
            ))
    return findings


def auditor_certified(
    certificate: dict,
    max_false_accusation: float | None = None,
) -> list[DeliveryFinding]:
    """One finding when the auditor's certificate does not carry its verdict.

    `certificate` is `Certificate.to_record()`. Uncertified means the auditor's
    detections were not distinguishable from chance at the certificate's alpha;
    `max_false_accusation` additionally caps the anytime-valid bound on how
    often it accuses a genuine value.
    """
    findings: list[DeliveryFinding] = []
    name = certificate.get("auditor", "auditor")
    if not certificate.get("certified"):
        findings.append(DeliveryFinding(
            "uncertified_auditor",
            f"{name}: e-process {certificate.get('e_final', 0):.3g} after "
            f"{certificate.get('rounds', 0)} planted rounds, below 1/alpha = "
            f"{1 / certificate.get('alpha', 0.05):.0f}; its clean report is not evidence",
        ))
    bound = certificate.get("false_accusation_upper", 1.0)
    if max_false_accusation is not None and bound > max_false_accusation:
        findings.append(DeliveryFinding(
            "false_accusation_bound",
            f"{name}: false-accusation bound {bound:.3f} exceeds {max_false_accusation:.3f}",
        ))
    return findings


def delivery_ready(
    prose: str,
    bibliography: dict[str, str],
    registry: dict[str, str],
    resolve: Callable[[str], bool | None] | None = None,
    certificate: dict | None = None,
    max_false_accusation: float | None = None,
    figures: list[dict] | None = None,
) -> list[DeliveryFinding]:
    """Run every check. An empty list is the only ready result.

    `resolve` adds the existence check on cited references; without it the
    gate runs offline and checks identifiers by form only. `certificate` adds
    the check that the review stage's auditor is certified. `figures` adds the
    check that captions and prose name only the panels each figure has.
    """
    findings = (
        citation_keys_resolve(prose, bibliography)
        + numbers_trace_to_registry(prose, registry)
        + no_residue(prose)
    )
    if resolve is not None:
        findings += references_resolve(prose, bibliography, resolve)
    if certificate is not None:
        findings += auditor_certified(certificate, max_false_accusation)
    if figures is not None:
        findings += figure_panels_match(figures, prose)
    return findings
