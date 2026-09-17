from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ROOT_CATEGORIES_TEXT = """tree_root_categories:
  - id: external-service-automation
    name: External service automation and integrations
    description: Operate a named remote app, SaaS product, API, connector, account or cloud service.
    select_when: Route here only when the primary action is to read or change state in an external service or connected account.
    dont_select_when: Do not route local files, source code, datasets, media, science models, security investigations or simulated environments here.
    children:
      - id: sales-marketing-and-crm
        name: Sales, marketing and CRM
        description: Remote CRM, leads, outreach, campaigns, ads, SEO, social posting, recruiting and customer engagement.
      - id: communication-productivity-and-education
        name: Communication, productivity and education
        description: Remote email, chat, calendar, meetings, notes, LMS, forms, support desks and team workflow tools.
      - id: commerce-finance-and-operations
        name: Commerce, finance and operations
        description: Remote ecommerce, payments, invoices, accounting, certificates, shipping, logistics and back-office systems.
      - id: cloud-developer-and-data-platforms
        name: Cloud, developer and data platforms
        description: Remote cloud, hosting, auth, databases, scraping, analytics, CMS, AI APIs, observability and dev platforms.
      - id: content-media-and-document-services
        name: Content, media and document services
        description: Remote services for generation, conversion, signing, publishing or managing content, documents or media.
      - id: generic-api-connectors
        name: Generic API and connector workflows
        description: Generic MCP/Rube/Composio/API connector skills whose main purpose is tool discovery and remote execution.

  - id: documents-office-and-records
    name: Documents, office files and records
    description: Create, read, convert, extract from or edit local document and office-file artifacts.
    select_when: Route here when the primary object is a PDF, Word, PPT, spreadsheet, CSV, form, contract, report or record file.
    dont_select_when: Do not route remote service operations, source-code work, raw media tasks, dataset analytics or scientific modeling here.
    children:
      - id: pdf-documents-and-ocr
        name: PDF, documents and OCR
        description: PDF and document reading, editing, parsing, redaction, page OCR and document-to-text conversion.
      - id: spreadsheets-csv-and-tables
        name: Spreadsheets, CSV and tables
        description: Spreadsheet, CSV and table-file handling, formulas, table extraction and file-level reconciliation.
      - id: presentations-reports-and-deliverables
        name: Presentations, reports and deliverables
        description: PPTX, formatted reports, charts inside documents and business-facing deliverable generation.
      - id: forms-contracts-and-records
        name: Forms, contracts and records
        description: Forms, contracts, certificates, official records, compliance documents and document workflow artifacts.

  - id: data-analytics-and-visualization
    name: Data analytics, BI and visualization
    description: Analyze structured data to compute metrics, patterns, features, models, charts or business conclusions.
    select_when: Route here when the primary object is a dataset or table and the goal is analysis, cleaning, modeling or visualization.
    dont_select_when: Do not route office-file formatting, remote service operation, source-code implementation, security forensics or physical-science modeling here.
    children:
      - id: data-cleaning-and-reconciliation
        name: Data cleaning and reconciliation
        description: Data quality, deduplication, fuzzy matching, joins, missing values, normalization and validation.
      - id: statistics-features-and-modeling
        name: Statistics, features and modeling
        description: Statistical analysis, feature engineering, PCA, clustering, regression, causal analysis and GLM.
      - id: time-series-trends-and-anomalies
        name: Time series, trends and anomalies
        description: Time-series detrending, anomaly detection, contribution analysis, seasonality and trend workflows.
      - id: visualization-and-business-reporting
        name: Visualization and business reporting
        description: D3, dashboards, charts, BI narratives and visual explanation of quantitative results.
      - id: geospatial-and-local-data-analysis
        name: Geospatial and local data analysis
        description: Spatial tables, distances, map data, city/place datasets and geospatial analysis.

  - id: software-engineering-devops
    name: Software engineering and DevOps
    description: Modify, test, build, migrate, review or operate software projects and developer infrastructure.
    select_when: Route here when the primary object is source code, a repository, build system, runtime environment, CI job or developer workflow.
    dont_select_when: Do not route remote business app operations, document processing, media tasks, scientific domain work or security investigations here unless code work is primary.
    children:
      - id: language-framework-and-migration
        name: Languages, frameworks and migrations
        description: Language, framework and API migrations across Python, Java, Scala, Erlang, React, Spring and similar stacks.
      - id: build-packaging-and-environments
        name: Build, packaging and environments
        description: Build lifecycle, Maven, uv, package managers, dependencies, config files and environment setup.
      - id: testing-ci-and-quality
        name: Testing, CI and quality
        description: Unit tests, browser tests, CI analysis, bug finding, changelogs, validation and code quality.
      - id: frontend-web-and-interactive-artifacts
        name: Frontend, web and interactive artifacts
        description: Web UI implementation, browser testing, Three.js web artifacts and frontend interface work.
      - id: performance-parallel-and-ml-engineering
        name: Performance, parallel and ML engineering
        description: Performance tuning, parallelization, memory optimization, JAX, model-training infrastructure and GPU workflows.

  - id: security-privacy-and-risk
    name: Security, privacy and risk analysis
    description: Investigate or reduce security, privacy, vulnerability, network, identity or operational risk.
    select_when: Route here when the primary goal is security assessment, threat detection, vulnerability handling, forensics, hardening or risk reporting.
    dont_select_when: Do not route ordinary software testing, generic API automation, normal document redaction, business analytics or scientific signal analysis here.
    children:
      - id: network-forensics-and-threat-detection
        name: Network forensics and threat detection
        description: PCAP triage, tshark, Suricata, IDS events, malicious traffic, scans, DoS and beaconing.
      - id: vulnerability-audit-and-pentest
        name: Vulnerability audit and penetration testing
        description: Penetration testing, vulnerability scanning, CVSS extraction, Trivy reports and security audit deliverables.
      - id: secure-coding-and-systems-security
        name: Secure coding and systems security
        description: Secure coding, framework hardening, kernel fuzzing, syzkaller and low-level system security.
      - id: secrets-identity-and-cloud-security
        name: Secrets, identity and cloud security
        description: API keys, authentication, secrets management, access controls, cloud security and account risk.

  - id: media-multimodal-and-creative
    name: Media, multimodal and creative processing
    description: Transform or understand audio, video, images, graphics, 3D assets or multimodal creative content.
    select_when: Route here when the primary object is media content rather than a document file, dataset, remote service or codebase.
    dont_select_when: Do not route office-document OCR, structured data analysis, source-code work, remote SaaS operation or scientific modeling here.
    children:
      - id: audio-speech-and-voice
        name: Audio, speech and voice
        description: Speech-to-text, Whisper, VAD, speaker clustering, diarization, TTS, audiobooks and audio cleanup.
      - id: video-and-ffmpeg-workflows
        name: Video and FFmpeg workflows
        description: Video editing, filters, format conversion, frame extraction, segmenting, metadata and video understanding.
      - id: images-ocr-and-computer-vision
        name: Images, OCR and computer vision
        description: Image editing, screenshot/photo OCR, object counting, masks and computer-vision analysis.
      - id: graphics-3d-and-design-assets
        name: Graphics, 3D and design assets
        description: Canvas design, brand assets, OBJ export, mesh analysis, graphics pipelines and 3D artifacts.
      - id: generative-and-multimodal-ai
        name: Generative and multimodal AI
        description: AI generation or multimodal model workflows for image, audio, video, voice or mixed media.

  - id: science-engineering-modeling
    name: Science, engineering and mathematical modeling
    description: Solve domain equations, simulations or analyses in science, engineering, math or physical systems.
    select_when: Route here when scientific or engineering domain semantics are primary, even if code or data processing is involved.
    dont_select_when: Do not route generic software work, business analytics, remote API operations, document processing, media editing or security forensics here.
    children:
      - id: control-optimization-simulation
        name: Control, optimization and simulation
        description: Control design, MPC, PID, state-space models, vehicle dynamics, CasADi and simulation metrics.
      - id: power-energy-industrial
        name: Power, energy and industrial systems
        description: Power flow, energy markets, locational prices, industrial processes and manufacturing guidance.
      - id: astronomy-seismology-environment
        name: Astronomy, seismology and environment
        description: Astronomy, seismology, hydrology, meteorology, environmental drivers and domain sensor data.
      - id: chemistry-materials-quantum-math
        name: Chemistry, materials, quantum and math
        description: Chemistry, materials, quantum workflows, symbolic math, numerical math and scientific fitting.

  - id: search-research-and-knowledge
    name: Search, research and knowledge work
    description: Find, retrieve, verify, cite or organize information from sources or reference datasets.
    select_when: Route here when the primary goal is evidence gathering, lookup, literature work, knowledge retrieval or information organization.
    dont_select_when: Do not route execution in a remote account, local file editing, source-code work, media processing, data modeling or security investigation here.
    children:
      - id: web-and-browser-research
        name: Web and browser research
        description: Web search, browser evidence gathering, page extraction, current lookup and source comparison.
      - id: academic-citation-and-literature
        name: Academic, citation and literature work
        description: Academic search, citations, literature evidence, research summaries and scholarly source organization.
      - id: enterprise-and-domain-knowledge
        name: Enterprise and domain knowledge
        description: Internal artifact search, wiki lookup, documentation search and structured domain references.
      - id: local-reference-and-travel-datasets
        name: Local reference and travel datasets
        description: Lookup-only flights, accommodations, attractions, restaurants, cities, distances and local references.
      - id: taxonomy-and-information-organization
        name: Taxonomy and information organization
        description: Taxonomy building, categorization, indexing, curation and knowledge organization.

  - id: embodied-simulation-and-interactive-tasks
    name: Embodied, simulated and interactive tasks
    description: Act inside simulated, embodied, game-like or interactive environments with state and actions.
    select_when: Route here when the primary task is navigation, object manipulation, environment state change, game-state reasoning or action execution.
    dont_select_when: Do not route real SaaS APIs, local files, source-code work, media processing, data analysis or physical-system modeling here.
    children:
      - id: goal-planning-and-verification
        name: Goal planning and verification
        description: Interpret goals, decompose tasks, track progress and verify completion in an interactive environment.
      - id: navigation-search-and-scanning
        name: Navigation, search and scanning
        description: Navigate, inspect surroundings, search likely locations and locate objects, tools or targets.
      - id: object-manipulation-and-transport
        name: Object manipulation and transport
        description: Pick up, retrieve, carry, place, store, dispose, combine or move objects.
      - id: environment-devices-and-state-changes
        name: Environment, devices and state changes
        description: Open/close containers, operate devices, heat, cool, clean or change object/environment state.
      - id: games-and-domain-simulators
        name: Games and domain simulators
        description: Game libraries, state parsers, maps, mechanics and interactive domain-specific simulators.

  - id: skill-agent-meta-workflows
    name: Agent, skill and workflow meta tasks
    description: Work on skills, agents, prompts, memory, retrieval, benchmarks, plans or orchestration itself.
    select_when: Route here only when the task is about building, testing, evaluating or coordinating the agent/skill system.
    dont_select_when: Do not route normal domain tasks here just because they may use a skill or agent internally.
    children:
      - id: skill-authoring-and-packaging
        name: Skill authoring and packaging
        description: Create, edit, document, package, vet or improve skills and skill collections.
      - id: benchmark-and-artifact-evaluation
        name: Benchmark and artifact evaluation
        description: SkillsBench, artifact evaluation, reproducibility checks, badges and benchmark harnesses.
      - id: planning-orchestration-and-workflows
        name: Planning, orchestration and workflows
        description: Task decomposition, file-based planning, workload balancing, dialogue graphs, PDDL and workflow coordination.
      - id: memory-retrieval-and-knowledge-systems
        name: Memory, retrieval and knowledge systems
        description: Agent memory, skill retrieval, indexing, ranking and context-management workflows.
"""


def parse_root_categories_text(text: str) -> list[Any]:
    payload = _parse_json_or_yaml(text)
    return extract_root_categories(payload, source="root_categories")


def coerce_root_categories_value(value: Any, *, allow_path: bool = True) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return extract_root_categories(value, source="root_categories")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _looks_like_inline_taxonomy(text):
            return parse_root_categories_text(text)
        if allow_path:
            return text
        return parse_root_categories_text(text)
    return value


def root_categories_to_text(value: Any) -> str:
    if value in (None, "", [], ()):
        return DEFAULT_ROOT_CATEGORIES_TEXT.rstrip()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_ROOT_CATEGORIES_TEXT.rstrip()
        if not _looks_like_inline_taxonomy(text):
            loaded = _read_path_if_exists(text)
            if loaded is not None:
                return loaded.strip()
            return DEFAULT_ROOT_CATEGORIES_TEXT.rstrip()
        return text
    if isinstance(value, list):
        return _dump_root_categories({"tree_root_categories": value})
    if isinstance(value, dict):
        if "tree_root_categories" in value or "root_categories" in value:
            return _dump_root_categories(value)
        return _dump_root_categories({"tree_root_categories": extract_root_categories(value, source="root_categories")})
    return str(value)


def extract_root_categories(payload: Any, *, source: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tree_root_categories", "root_categories"):
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{source}: field '{key}' must be a list")
            return value
    raise ValueError(f"{source}: expected a root category list or object with tree_root_categories/root_categories")


def _parse_json_or_yaml(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse root_categories: {exc}") from exc


def _dump_root_categories(payload: Any) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()


def _looks_like_inline_taxonomy(text: str) -> bool:
    stripped = text.lstrip()
    return (
        "\n" in stripped
        or stripped.startswith("[")
        or stripped.startswith("{")
        or stripped.startswith("- ")
        or stripped.startswith("tree_root_categories:")
        or stripped.startswith("root_categories:")
    )


def _read_path_if_exists(text: str) -> str | None:
    try:
        path = Path(text).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


__all__ = [
    "DEFAULT_ROOT_CATEGORIES_TEXT",
    "coerce_root_categories_value",
    "extract_root_categories",
    "parse_root_categories_text",
    "root_categories_to_text",
]
