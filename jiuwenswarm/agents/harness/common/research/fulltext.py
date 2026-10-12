# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Open-access full text of a paper, split into titled sections.

`fetch(work)` tries the open routes in the order the research harness ranks
them (acquisition/routes.py), first answer wins:

  arxiv     arxiv.org/html/<id>, the LaTeX-rendered HTML of an arXiv paper;
            the arXiv PDF when that page does not exist
  pmc       Europe PMC's JATS XML of a PubMed Central article, found by the
            PMCID OpenAlex records or else by a Europe PMC lookup of the DOI
  openalex  the work's open-access location recorded by OpenAlex
  unpaywall the best open-access location Unpaywall knows for the DOI
            (only when UNPAYWALL_EMAIL is set, as Unpaywall asks)

HTML and JATS XML are parsed with the standard library. A PDF is read only
when PyMuPDF is installed; without it a PDF-only paper counts as not
retrieved, with the reason recorded.

`results_text(sections)` is the text of the sections that report results, the
part of a paper an evidence stage reads findings from.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Callable

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
USER_AGENT = "jiuwenswarm-research/1.0 (open-access full-text reader)"
RESULTS_TITLE = re.compile(r"\b(results?|findings|evaluation|experiments?|empirical|outcomes?)\b", re.I)
_NOT_RESULTS = re.compile(r"\b(setup|set-up|design|methods?|materials|protocol)\b", re.I)
_PDF_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)?(abstract|introduction|related work|background|"
    r"methods?|materials and methods|methodology|experiments?|experimental results|results?|"
    r"results and discussion|evaluation|discussion|conclusions?|limitations|references)\s*$", re.I)


def _get(url: str, timeout: int = 40) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


class _Sections(HTMLParser):
    """Text under each h1-h4 heading; scripts, styles, navigation and math markup are skipped."""

    _SKIP = {"script", "style", "nav", "header", "footer", "math", "figure", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[dict] = [{"title": "", "text": "", "level": 1}]
        self._skip = 0
        self._heading: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4") and not self._skip:
            self._heading = []
            self._level = int(tag[1])

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in ("h1", "h2", "h3", "h4") and self._heading is not None:
            title = " ".join("".join(self._heading).split())
            self.sections.append({"title": title, "text": "", "level": self._level})
            self._heading = None
        elif tag in ("p", "div", "li") and not self._skip:
            self.sections[-1]["text"] += "\n"

    def handle_data(self, data):
        if self._skip:
            return
        if self._heading is not None:
            self._heading.append(data)
        else:
            self.sections[-1]["text"] += data


def _tidy(sections: list[dict]) -> list[dict]:
    out = []
    for s in sections:
        text = re.sub(r"[ \t\r\f\v]+", " ", s["text"])
        text = re.sub(r"\s*\n\s*", "\n", text).strip()
        if text:
            out.append({"title": re.sub(r"^[\d.\s]+", "", s["title"]).strip(), "text": text,
                        "level": s.get("level", 1)})
    return out


def html_sections(html: str) -> list[dict]:
    parser = _Sections()
    parser.feed(html)
    return _tidy(parser.sections)


def jats_sections(xml: bytes) -> list[dict]:
    """Top-level <sec> elements of a JATS article body, with their titles."""
    body = ET.fromstring(xml).find(".//body")
    if body is None:
        return []
    sections = []
    for sec in body.findall("sec"):
        title = sec.findtext("title") or ""
        paragraphs = [" ".join("".join(p.itertext()).split()) for p in sec.iter("p")]
        sections.append({"title": title, "text": "\n".join(paragraphs)})
    return _tidy(sections)


def pdf_sections(pdf: bytes) -> list[dict] | None:
    """Sections of a PDF split at heading lines; None when PyMuPDF is not installed."""
    try:
        import fitz
    except ImportError:
        return None
    with fitz.open(stream=io.BytesIO(pdf), filetype="pdf") as doc:
        lines = "\n".join(page.get_text() for page in doc).splitlines()
    sections = [{"title": "", "text": ""}]
    for line in lines:
        if _PDF_HEADING.match(line):
            sections.append({"title": line.strip(), "text": ""})
        else:
            sections[-1]["text"] += line + "\n"
    return _tidy(sections)


def results_text(sections: list[dict]) -> str:
    """The text of the sections whose title says they report results, with their subsections."""
    texts, within = [], None
    for s in sections:
        if within is not None and s["level"] > within:
            texts.append(s["text"])
            continue
        within = None
        if RESULTS_TITLE.search(s["title"]) and not _NOT_RESULTS.search(s["title"]):
            texts.append(s["text"])
            within = s["level"]
    return "\n".join(texts)


def _arxiv_id(work: dict) -> str:
    doi = (work.get("doi") or "").lower()
    m = re.search(r"10\.48550/arxiv\.(.+)$", doi)
    return (work.get("arxiv_id") or (m.group(1) if m else "")).strip()


def routes(work: dict) -> list[tuple[str, str]]:
    """(route, url) candidates for an OpenAlex work, best first."""
    out = []
    arxiv = _arxiv_id(work)
    if arxiv:
        out += [("arxiv", f"https://arxiv.org/html/{arxiv}"), ("arxiv", f"https://arxiv.org/pdf/{arxiv}")]
    pmcid = ((work.get("ids") or {}).get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1]
    doi = (work.get("doi") or "").replace("https://doi.org/", "")
    if pmcid:
        pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
        out.append(("pmc", f"{EUROPE_PMC}/{pmcid}/fullTextXML"))
    elif doi and not arxiv:
        out.append(("pmc", f"{EUROPE_PMC}/search?format=json&resultType=lite&query="
                           + urllib.parse.quote(f'DOI:"{doi}" AND OPEN_ACCESS:Y')))
    best = work.get("best_oa_location") or {}
    for url in (best.get("pdf_url"), (work.get("open_access") or {}).get("oa_url"), best.get("landing_page_url")):
        if url and all(url != u for _, u in out):
            out.append(("openalex", url))
    if doi and os.environ.get("UNPAYWALL_EMAIL"):
        out.append(("unpaywall", f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
                                 f"?email={urllib.parse.quote(os.environ['UNPAYWALL_EMAIL'])}"))
    return out


def _parse(body: bytes, content_type: str, url: str) -> list[dict] | None:
    if body[:5] == b"%PDF-" or "pdf" in content_type:
        return pdf_sections(body)
    if url.endswith("/fullTextXML") or "xml" in content_type:
        return jats_sections(body)
    return html_sections(body.decode("utf-8", "replace"))


def _looked_up(route: str, body: bytes) -> str:
    """The open copy a lookup answer names (Unpaywall, or Europe PMC search by DOI); "" when none."""
    if route == "unpaywall":
        loc = json.loads(body).get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url") or ""
    hits = json.loads(body).get("resultList", {}).get("result", [])
    pmcid = next((h["pmcid"] for h in hits if h.get("pmcid")), "")
    return f"{EUROPE_PMC}/{pmcid}/fullTextXML" if pmcid else ""


def fetch(work: dict, get: Callable[[str], tuple[bytes, str]] = _get) -> dict:
    """{"route", "url", "sections"} for the first route that yields sections, or
    {"route": None, "attempts": [...]} with each route's reason for failing."""
    attempts = []
    for route, url in routes(work):
        try:
            body, ctype = get(url)
            if route == "unpaywall" or "/search?" in url:
                url = _looked_up(route, body)
                if not url:
                    attempts.append(f"{route}: no open-access copy")
                    continue
                body, ctype = get(url)
            sections = _parse(body, ctype, url)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, ET.ParseError) as exc:
            attempts.append(f"{route}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if sections is None:
            attempts.append(f"{route}: a PDF, and PyMuPDF is not installed")
        elif sum(len(s["text"]) for s in sections) < 2000:
            attempts.append(f"{route}: too little text (a landing page, not the paper)")
        else:
            return {"route": route, "url": url, "sections": sections}
    return {"route": None, "attempts": attempts}
