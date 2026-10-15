"""构思模块的纯 Python 文献检索与结果校验核心函数。"""

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MIN_KEY_PAPERS = 3
# Compatibility module kept for callers that import the pure helpers directly.
# The authoritative workflow contract requires exactly ten references.
REQUIRED_REFERENCES = 10


SOURCE_ALIASES = {
    "arxiv": "arxiv",
    "semantic_scholar": "semantic_scholar",
    "semanticscholar": "semantic_scholar",
    "s2": "semantic_scholar",
    "openalex": "openalex",
    "open_alex": "openalex",
    "crossref": "crossref",
    "cross_ref": "crossref",
}
USER_AGENT = "JiuwenSwarm-Conception/0.6"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_str(value):
    return isinstance(value, str) and bool(value.strip())


def _parse_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError
    year, month, day = (int(part) for part in value.split("-"))
    month_days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not 1 <= month <= 12 or not 1 <= day <= month_days[month - 1]:
        raise ValueError
    return value


def _normalize_source(value):
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return SOURCE_ALIASES.get(key)


def _input_errors(payload):
    errors = []
    request = payload.get("research_request")
    if not isinstance(request, dict) or not _is_non_empty_str(request.get("direction")):
        errors.append("research_request.direction 必须是非空字符串")
    else:
        for key in ("problem_context", "desired_outcome"):
            if key in request and not _is_non_empty_str(request[key]):
                errors.append(f"research_request.{key} 如提供则必须是非空字符串")
        excluded_scope = request.get("excluded_scope")
        if excluded_scope is not None and (
            not isinstance(excluded_scope, list)
            or not excluded_scope
            or not all(isinstance(item, str) and item.strip() for item in excluded_scope)
        ):
            errors.append(
                "research_request.excluded_scope 如提供则必须是非空字符串列表"
            )

    domain = payload.get("domain")
    if not isinstance(domain, dict) or not _is_non_empty_str(domain.get("domain_name")):
        errors.append("domain.domain_name 必须是非空字符串")

    resources = payload.get("resource_constraints")
    if not isinstance(resources, dict):
        errors.append("resource_constraints 必须是对象")
    else:
        if "gpu_type" in resources and not _is_non_empty_str(resources["gpu_type"]):
            errors.append("resource_constraints.gpu_type 如提供则必须是非空字符串")
        if not _is_int(resources.get("gpu_hours")) or resources["gpu_hours"] < 0:
            errors.append("resource_constraints.gpu_hours 必须是大于或等于 0 的整数")
        if not _is_int(resources.get("memory_gb")) or resources["memory_gb"] <= 0:
            errors.append("resource_constraints.memory_gb 必须是大于 0 的整数")
        if not _is_int(resources.get("time_budget_days")) or resources["time_budget_days"] <= 0:
            errors.append("resource_constraints.time_budget_days 必须是大于 0 的整数")
        budget_value = resources.get("budget")
        if budget_value is not None and (
            isinstance(budget_value, bool)
            or not isinstance(budget_value, (int, float))
            or budget_value < 0
        ):
            errors.append("resource_constraints.budget 必须是大于或等于 0 的数字")

    search = payload.get("search_constraints")
    if not isinstance(search, dict):
        errors.append("search_constraints 必须是对象")
    else:
        query = search.get("query")
        if query is not None and not _is_non_empty_str(query):
            errors.append("search_constraints.query 如提供则必须是非空字符串")
        sources = search.get("sources")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) and item.strip() for item in sources
        ):
            errors.append("search_constraints.sources 必须是非空字符串列表")
        elif any(_normalize_source(item) is None for item in sources):
            errors.append(
                "search_constraints.sources 仅支持 arxiv、semantic_scholar、openalex 和 crossref"
            )
        max_results = search.get("max_results")
        if not _is_int(max_results) or not REQUIRED_REFERENCES <= max_results <= 50:
            errors.append(
                f"search_constraints.max_results 必须是 {REQUIRED_REFERENCES} 到 50 的整数"
            )
        languages = search.get("languages")
        if languages is not None and (
            not isinstance(languages, list)
            or not languages
            or not all(isinstance(item, str) and item.strip() for item in languages)
        ):
            errors.append("search_constraints.languages 如提供则必须是非空字符串列表")
        try:
            date_from = _parse_date(search.get("date_from"))
            date_to = _parse_date(search.get("date_to"))
            if date_from and date_to and date_from > date_to:
                errors.append("search_constraints.date_from 不得晚于 date_to")
        except ValueError:
            errors.append("search_constraints.date_from/date_to 必须使用 YYYY-MM-DD")

    revision = payload.get("revision_context")
    if revision is not None and (
        not isinstance(revision, dict)
        or not isinstance(revision.get("previous_output"), dict)
        or not _is_non_empty_str(revision.get("feedback"))
    ):
        errors.append("revision_context 必须包含 previous_output 对象和非空 feedback")
    return errors


def _http_json(url, headers=None):
    request = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"文献 API 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise RuntimeError(f"无法连接文献 API：{reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("文献 API 返回了无法解析的数据") from exc


def _http_xml(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:
            return ET.fromstring(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"arXiv API 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise RuntimeError(f"无法连接 arXiv API：{reason}") from exc
    except ET.ParseError as exc:
        raise RuntimeError("arXiv API 返回了无法解析的 XML") from exc


def _compact_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _date_allowed(published, date_from, date_to):
    if not published:
        return True
    paper_date = published[:10]
    try:
        _parse_date(paper_date)
    except ValueError:
        return True
    return not ((date_from and date_from > paper_date) or (date_to and paper_date > date_to))


def _search_arxiv(query, max_results, date_from, date_to):
    search_query = f"all:{query}"
    if date_from or date_to:
        lower = (date_from or "1991-01-01").replace("-", "") + "0000"
        upper = (date_to or "2099-12-31").replace("-", "") + "2359"
        search_query += f" AND submittedDate:[{lower} TO {upper}]"
    params = urlencode(
        {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )
    root = _http_xml(f"https://export.arxiv.org/api/query?{params}")
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", namespace):
        raw_id = _compact_text(entry.findtext("atom:id", default="", namespaces=namespace))
        paper_id = re.sub(r"v\d+$", "", raw_id.rsplit("/", 1)[-1])
        published = _compact_text(entry.findtext("atom:published", default="", namespaces=namespace))[:10]
        if not paper_id or not _date_allowed(published, date_from, date_to):
            continue
        papers.append(
            {
                "id": paper_id,
                "title": _compact_text(entry.findtext("atom:title", default="", namespaces=namespace)),
                "source": "arxiv",
                "url": f"https://arxiv.org/abs/{paper_id}",
                "authors": [
                    _compact_text(author.findtext("atom:name", default="", namespaces=namespace))
                    for author in entry.findall("atom:author", namespace)
                ],
                "published_date": published,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "venue": "arXiv",
                "abstract": _compact_text(
                    entry.findtext("atom:summary", default="", namespaces=namespace)
                )[:2000],
            }
        )
    return papers


def _search_semantic_scholar(query, max_results, date_from, date_to, api_key):
    api_key = str(api_key or "").strip()
    params = {
        "query": query,
        "limit": max_results,
        "fields": "paperId,title,url,abstract,authors,year,venue,externalIds,publicationDate",
    }
    if date_from and date_to:
        params["year"] = f"{date_from[:4]}-{date_to[:4]}"
    data = _http_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode(params),
        headers={"x-api-key": api_key} if api_key else None,
    )
    papers = []
    for item in data.get("data", []):
        external_ids = item.get("externalIds") or {}
        arxiv_id = _compact_text(external_ids.get("ArXiv"))
        doi = _compact_text(external_ids.get("DOI"))
        semantic_id = _compact_text(item.get("paperId"))
        paper_id = arxiv_id or (f"DOI:{doi}" if doi else f"S2:{semantic_id}")
        published = _compact_text(item.get("publicationDate"))
        if not semantic_id or not _date_allowed(published, date_from, date_to):
            continue
        papers.append(
            {
                "id": paper_id,
                "title": _compact_text(item.get("title")),
                "source": "semantic_scholar",
                "url": _compact_text(item.get("url"))
                or f"https://www.semanticscholar.org/paper/{semantic_id}",
                "authors": [
                    _compact_text(author.get("name"))
                    for author in item.get("authors") or []
                    if isinstance(author, dict)
                ],
                "published_date": published,
                "year": item.get("year") if _is_int(item.get("year")) else None,
                "venue": _compact_text(item.get("venue")) or "无",
                "abstract": _compact_text(item.get("abstract"))[:2000],
            }
        )
    return papers


def _openalex_abstract(inverted_index):
    if not isinstance(inverted_index, dict):
        return ""
    positioned = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            positioned.extend((position, word) for position in positions if _is_int(position))
    return _compact_text(" ".join(word for _, word in sorted(positioned)))[:2000]


def _search_openalex(query, max_results, date_from, date_to, api_key):
    params = {"search": query, "per_page": max_results}
    filters = []
    if date_from:
        filters.append(f"from_publication_date:{date_from}")
    if date_to:
        filters.append(f"to_publication_date:{date_to}")
    if filters:
        params["filter"] = ",".join(filters)
    api_key = str(api_key or "").strip()
    if api_key:
        params["api_key"] = api_key
    data = _http_json("https://api.openalex.org/works?" + urlencode(params))
    papers = []
    for item in data.get("results", []):
        ids = item.get("ids") or {}
        arxiv_url = _compact_text(ids.get("arxiv"))
        arxiv_id = re.sub(r"v\d+$", "", arxiv_url.rsplit("/", 1)[-1]) if arxiv_url else ""
        doi = _compact_text(item.get("doi") or ids.get("doi"))
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
        openalex_id = _compact_text(item.get("id")).rsplit("/", 1)[-1]
        paper_id = arxiv_id or (f"DOI:{doi}" if doi else f"OpenAlex:{openalex_id}")
        published = _compact_text(item.get("publication_date"))
        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        if not openalex_id or not _date_allowed(published, date_from, date_to):
            continue
        papers.append(
            {
                "id": paper_id,
                "title": _compact_text(item.get("title") or item.get("display_name")),
                "source": "openalex",
                "url": _compact_text(primary_location.get("landing_page_url"))
                or (f"https://doi.org/{doi}" if doi else _compact_text(item.get("id"))),
                "authors": [
                    _compact_text((authorship.get("author") or {}).get("display_name"))
                    for authorship in item.get("authorships") or []
                    if isinstance(authorship, dict)
                ],
                "published_date": published,
                "year": item.get("publication_year")
                if _is_int(item.get("publication_year"))
                else None,
                "venue": _compact_text(source.get("display_name")) or "无",
                "abstract": _openalex_abstract(item.get("abstract_inverted_index")),
            }
        )
    return papers


def _crossref_date(item):
    for key in ("published-online", "published-print", "published", "issued"):
        date_parts = (item.get(key) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            parts = date_parts[0]
            if parts and _is_int(parts[0]):
                year = parts[0]
                month = parts[1] if len(parts) > 1 and _is_int(parts[1]) else 1
                day = parts[2] if len(parts) > 2 and _is_int(parts[2]) else 1
                return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def _search_crossref(query, max_results, date_from, date_to, email, api_token):
    params = {"query.bibliographic": query, "rows": max_results}
    filters = []
    if date_from:
        filters.append(f"from-pub-date:{date_from}")
    if date_to:
        filters.append(f"until-pub-date:{date_to}")
    if filters:
        params["filter"] = ",".join(filters)
    email = str(email or "").strip()
    if email:
        params["mailto"] = email
    api_token = str(api_token or "").strip()
    headers = (
        {"Crossref-Plus-API-Token": f"Bearer {api_token}"} if api_token else None
    )
    data = _http_json(
        "https://api.crossref.org/works?" + urlencode(params), headers=headers
    )
    papers = []
    for item in (data.get("message") or {}).get("items", []):
        doi = _compact_text(item.get("DOI"))
        published = _crossref_date(item)
        if not doi or not _date_allowed(published, date_from, date_to):
            continue
        title_values = item.get("title") or []
        venue_values = item.get("container-title") or []
        authors = []
        for author in item.get("author") or []:
            if not isinstance(author, dict):
                continue
            name = _compact_text(
                " ".join(filter(None, [author.get("given"), author.get("family")]))
                or author.get("name")
            )
            if name:
                authors.append(name)
        abstract = html.unescape(re.sub(r"<[^>]+>", " ", str(item.get("abstract") or "")))
        papers.append(
            {
                "id": f"DOI:{doi}",
                "title": _compact_text(title_values[0] if title_values else ""),
                "source": "crossref",
                "url": f"https://doi.org/{doi}",
                "authors": authors,
                "published_date": published,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "venue": _compact_text(venue_values[0] if venue_values else item.get("publisher"))
                or "无",
                "abstract": _compact_text(abstract)[:2000],
            }
        )
    return papers


def _search_literature(payload):
    search = payload["search_constraints"]
    query = _compact_text(search.get("query")) or _compact_text(
        payload["research_request"]["direction"]
    )
    max_results = search["max_results"]
    date_from = _parse_date(search.get("date_from"))
    date_to = _parse_date(search.get("date_to"))
    sources = list(dict.fromkeys(_normalize_source(item) for item in search["sources"]))
    api_config = payload.get("api_config") or {}
    semantic_scholar_api_key = api_config.get("semantic_scholar_api_key") or os.getenv(
        "SEMANTIC_SCHOLAR_API_KEY"
    )
    openalex_api_key = api_config.get("openalex_api_key") or os.getenv(
        "OPENALEX_API_KEY"
    )
    crossref_email = api_config.get("crossref_email") or os.getenv("CROSSREF_EMAIL")
    crossref_plus_api_token = api_config.get("crossref_plus_api_token") or os.getenv(
        "CROSSREF_PLUS_API_TOKEN"
    )
    source_results = []
    source_errors = []
    for source in sources:
        try:
            if source == "arxiv":
                found = _search_arxiv(query, max_results, date_from, date_to)
            elif source == "semantic_scholar":
                found = _search_semantic_scholar(
                    query,
                    max_results,
                    date_from,
                    date_to,
                    semantic_scholar_api_key,
                )
            elif source == "openalex":
                found = _search_openalex(
                    query,
                    max_results,
                    date_from,
                    date_to,
                    openalex_api_key,
                )
            else:
                found = _search_crossref(
                    query,
                    max_results,
                    date_from,
                    date_to,
                    crossref_email,
                    crossref_plus_api_token,
                )
            source_results.append(found)
        except RuntimeError as exc:
            source_errors.append(f"{source}: {exc}")

    unique = []
    seen_ids = set()
    seen_titles = set()
    for index in range(max((len(items) for items in source_results), default=0)):
        for items in source_results:
            if index >= len(items):
                continue
            paper = items[index]
            title_key = re.sub(r"\W+", "", paper["title"].lower())
            paper["authors"] = [name for name in paper.get("authors", []) if name]
            paper["venue"] = _compact_text(paper.get("venue")) or "无"
            if (
                not paper["title"]
                or not title_key
                or paper["id"] in seen_ids
                or title_key in seen_titles
                or not paper["authors"]
                or not _is_int(paper.get("year"))
            ):
                continue
            seen_ids.add(paper["id"])
            seen_titles.add(title_key)
            unique.append(paper)
            if len(unique) == max_results:
                return unique
    if len(unique) < REQUIRED_REFERENCES:
        details = f"；失败来源：{'；'.join(source_errors)}" if source_errors else ""
        raise RuntimeError(
            f"真实检索仅得到 {len(unique)} 篇有效论文，正式输出需要 "
            f"{REQUIRED_REFERENCES} 篇；请调整 query、日期、来源或 max_results{details}"
        )
    return unique


def _has_paper_citation(text, paper_ids):
    return any(f"[{paper_id}]" in text for paper_id in paper_ids)


_CITATION_RE = re.compile(r"\[([^\[\]\r\n]+)\]")


def _citation_errors(text, paper_ids, field_name):
    """Return both missing-citation and out-of-reference citation errors.

    The research synthesizer uses this validator before deciding whether to
    spend its single repair turn.  It must therefore enforce the same
    reference-closure rule as the workflow's final acceptance validator.
    """
    cited_ids = set(_CITATION_RE.findall(str(text or "")))
    errors = []
    if not (cited_ids & paper_ids):
        errors.append(f"{field_name} 必须使用 [论文id] 标注文献依据")
    unsupported = sorted(cited_ids - paper_ids)
    if unsupported:
        errors.append(
            f"{field_name} 引用了 references 之外的论文：{', '.join(unsupported)}"
        )
    return errors


def _output_errors(result, candidates):
    errors = []
    question = result.get("research_question")
    if not isinstance(question, dict) or any(
        not _is_non_empty_str(question.get(key))
        for key in ("topic", "scope", "success_criteria")
    ):
        errors.append("research_question 字段不完整")

    hypotheses = result.get("hypotheses")
    hypothesis_ids = set()
    if not isinstance(hypotheses, list) or not hypotheses:
        errors.append("hypotheses 必须是非空列表")
    else:
        for item in hypotheses:
            if (
                not isinstance(item, dict)
                or not _is_non_empty_str(item.get("id"))
                or not re.fullmatch(r"H[1-9]\d*", item["id"])
                or item.get("verifiable") is not True
                or not _is_non_empty_str(item.get("claim"))
            ):
                errors.append("hypotheses 必须使用唯一的 H 加正整数 id 且全部可验证")
                break
            hypothesis_ids.add(item["id"])
        if len(hypothesis_ids) != len(hypotheses):
            errors.append("hypotheses.id 不得重复")

    gap = result.get("gap_report")
    if not isinstance(gap, dict) or any(
        not _is_non_empty_str(gap.get(key))
        for key in ("research_question", "existing_state", "missing_capability")
    ):
        errors.append("gap_report 字段不完整")
    elif isinstance(question, dict) and gap["research_question"] != question.get("topic"):
        errors.append("gap_report.research_question 必须与 research_question.topic 一致")
    elif "opportunities" in gap and (
        not isinstance(gap["opportunities"], list)
        or not gap["opportunities"]
        or not all(_is_non_empty_str(item) for item in gap["opportunities"])
    ):
        errors.append("gap_report.opportunities 如提供则必须是非空字符串列表")

    candidate_index = {item["id"]: item for item in candidates}
    references = result.get("references")
    reference_ids = set()
    reference_index = {}
    if not isinstance(references, list) or len(references) != REQUIRED_REFERENCES:
        errors.append(f"references 必须恰好包含 {REQUIRED_REFERENCES} 篇")
    else:
        for item in references:
            if not isinstance(item, dict):
                errors.append("references 的每个元素都必须是对象")
                continue
            required_text = ("id", "title", "source", "url", "citation_text", "relevance")
            if any(not _is_non_empty_str(item.get(key)) for key in required_text):
                errors.append("references 存在缺失字段或空字符串")
                continue
            authors = item.get("authors")
            if (
                not isinstance(authors, list)
                or not authors
                or not all(isinstance(name, str) and name.strip() for name in authors)
            ):
                errors.append(f"references[{item['id']}].authors 必须是非空字符串列表")
            if not _is_int(item.get("year")):
                errors.append(f"references[{item['id']}].year 必须是整数")
            if "venue" in item and not _is_non_empty_str(item["venue"]):
                errors.append(f"references[{item['id']}].venue 如提供则必须非空")
            related_ids = item.get("related_hypothesis_ids", [])
            if not isinstance(related_ids, list) or any(
                related_id not in hypothesis_ids for related_id in related_ids
            ):
                errors.append(
                    f"references[{item['id']}].related_hypothesis_ids 含不存在的假设 id"
                )

            paper_id = item["id"]
            if paper_id in reference_ids:
                errors.append(f"references.id 不得重复：{paper_id}")
                continue
            reference_ids.add(paper_id)
            reference_index[paper_id] = item
            candidate = candidate_index.get(paper_id)
            if candidate is None:
                errors.append(f"references 包含未由 API 检索到的论文：{paper_id}")
                continue
            for key in ("title", "authors", "year", "source", "url"):
                if item.get(key) != candidate.get(key):
                    errors.append(f"references[{paper_id}].{key} 与 API 记录不一致")
            if "venue" in item and item["venue"] != candidate["venue"]:
                errors.append(f"references[{paper_id}].venue 与 API 记录不一致")

    papers = result.get("key_papers")
    selected_ids = set()
    if not isinstance(papers, list) or len(papers) < MIN_KEY_PAPERS:
        errors.append(f"key_papers 至少需要 {MIN_KEY_PAPERS} 篇")
    elif any(
        not isinstance(item, dict)
        or any(
            not _is_non_empty_str(item.get(key))
            for key in ("id", "title", "source", "url", "method_key")
        )
        or (
            "is_baseline" in item
            and not isinstance(item.get("is_baseline"), bool)
        )
        for item in papers
    ):
        errors.append("key_papers 存在缺失字段或类型错误")
    else:
        selected_ids = {item["id"] for item in papers}
        if len(selected_ids) != len(papers):
            errors.append("key_papers.id 不得重复")
        for item in papers:
            candidate = candidate_index.get(item["id"])
            if candidate is None:
                errors.append(f"key_papers 包含未由 API 检索到的论文：{item['id']}")
                continue
            for key in ("title", "source", "url"):
                if item[key] != candidate[key]:
                    errors.append(f"key_papers[{item['id']}].{key} 与 API 记录不一致")
            reference = reference_index.get(item["id"])
            if reference is None:
                errors.append(f"key_papers[{item['id']}] 不在 references 中")
            else:
                for key in ("title", "source", "url"):
                    if item[key] != reference[key]:
                        errors.append(
                            f"key_papers[{item['id']}].{key} 与 references 不一致"
                        )

    frontier = result.get("research_frontier")
    if not isinstance(frontier, dict) or not _is_non_empty_str(
        frontier.get("frontier_text")
    ):
        errors.append("research_frontier.frontier_text 必须非空")

    if reference_ids and isinstance(gap, dict):
        for key in ("existing_state", "missing_capability"):
            errors.extend(
                _citation_errors(
                    gap.get(key, ""), reference_ids, f"gap_report.{key}"
                )
            )
    if reference_ids and isinstance(frontier, dict):
        errors.extend(
            _citation_errors(
                frontier.get("frontier_text", ""),
                reference_ids,
                "research_frontier.frontier_text",
            )
        )
    return errors


def validate_input(payload):
    """返回业务输入错误；空列表表示输入合法。"""
    return _input_errors(payload if isinstance(payload, dict) else {})


def search_literature(payload):
    """校验输入并通过真实文献 API 返回候选论文。"""
    errors = validate_input(payload)
    if errors:
        raise ValueError("；".join(errors))
    return _search_literature(payload)


def validate_output(payload, candidates, result):
    """校验 Agent 草稿并组装可交付结果；不调用模型、不自动修改。"""
    input_errors = validate_input(payload)
    if input_errors:
        return {"status": "FAILED", "errors": input_errors}
    if not isinstance(candidates, list):
        return {"status": "FAILED", "errors": ["candidates 必须是列表"]}
    if not isinstance(result, dict):
        return {"status": "FAILED", "errors": ["draft 必须是 JSON 对象"]}

    errors = _output_errors(result, candidates)
    if errors:
        return {"status": "FAILED", "errors": errors, "partial": result}
    return {
        **result,
        "domain": payload["domain"],
        "resource_constraints": payload["resource_constraints"],
        "status": "PASS",
    }
