"""Index root documents once; enrich only the candidates the model will see."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Sequence

import yaml

from .types import SkillDocument
from .cards import capability_card
from .sources import read_sources, source_fingerprint
from .capability_metadata import declared_contract
from .catalog_cache import CatalogCache
from .execution import SelectionBusy, checkpoint
from .freshness import file_stamp, iter_skill_files, sources_unchanged

logger = logging.getLogger(__name__)


class CatalogChanged(SelectionBusy):
    """A candidate no longer belongs to the indexed, consistently read version."""


def directory_id(directory: str | Path) -> str:
    # Resolve symlinks and normalize Windows casing so scope comparisons agree.
    return os.path.normcase(str(Path(directory).resolve()))


def _descriptions(metadata: dict) -> tuple[str, ...]:
    """Use authored descriptions only; do not translate or duplicate fields."""
    descriptions = []
    seen = set()
    for key in ("description", "description_zh", "description_en"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = " ".join(value.split())
        if normalized not in seen:
            descriptions.append(value.strip())
            seen.add(normalized)
    return tuple(descriptions)


def _root_version(document):
    checkpoint()
    path = Path(document.directory) / 'SKILL.md'
    root = Path(document.catalog_root or document.directory)
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise CatalogChanged('Skill root now resolves outside its registered directory')
        stamp = file_stamp(path)
        if stamp is None or stamp[1] > 1024 * 1024:
            raise CatalogChanged('Skill root was removed or exceeds the indexing limit')
        return str(resolved), stamp
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, CatalogChanged):
            raise
        raise CatalogChanged('Skill root is no longer readable') from exc


def hydrate_document(
    document: SkillDocument, *, cache: CatalogCache | None = None,
    observed_files: dict | None = None,
) -> SkillDocument:
    """Return the original full candidate fields, after validating its sources.

    Root changes cannot silently alter an existing ranking snapshot. References
    are independent of ranking and can refresh just this candidate's details.
    """
    if not document.root_sha256:
        # Legacy callers can still construct complete SkillDocument values.
        return document
    before = _root_version(document)
    raw = None
    if before != document.root_stamp:
        try:
            raw = (Path(document.directory) / 'SKILL.md').read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise CatalogChanged('Skill root changed or is unreadable') from exc
        if hashlib.sha256(raw.encode()).hexdigest() != document.root_sha256:
            raise CatalogChanged('Skill root content changed since indexing')
    key = (document.id, document.root_sha256, document.capability_description)
    cached = cache.get_details(key) if cache is not None else None
    if cached is not None:
        observed = dict(cached.observed_files)
        if sources_unchanged(observed) and _root_version(document) == before:
            if observed_files is not None:
                observed_files.update(observed)
            return replace(document, fingerprint=cached.document.fingerprint,
                           capability_record=cached.document.capability_record, root_raw='')
    checkpoint()
    if raw is None:
        raw = document.root_raw or (cache.get_root(document.id, document.root_sha256)
                                    if cache is not None else None)
    if raw is None:
        try:
            raw = (Path(document.directory) / 'SKILL.md').read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise CatalogChanged('Skill root changed or is unreadable') from exc
    if hashlib.sha256(raw.encode()).hexdigest() != document.root_sha256:
        raise CatalogChanged('Skill root content changed since indexing')
    observed = {}
    sources = read_sources(document.directory, raw, observed_files=observed)
    record = asdict(declared_contract(document.capability_description, sources))
    record = {k: sorted(v) if isinstance(v, frozenset) else v for k, v in record.items()}
    if _root_version(document) != before or not sources_unchanged(observed):
        raise CatalogChanged('Skill material changed while preparing its candidate')
    hydrated = replace(document, fingerprint=source_fingerprint('', sources),
                       capability_record=record, root_raw='')
    if cache is not None:
        cache.put_root(document.id, document.root_sha256, raw)
        cache.put_details(key, hydrated, observed)
    if observed_files is not None:
        observed_files.update(observed)
    return hydrated


def _read_document(root, directory, parent, resolved, stamp, *, text_mode, text_max_chars):
    path = parent / 'SKILL.md'
    if stamp[1] > 1024 * 1024:
        raise ValueError('SKILL.md exceeds the 1 MiB local indexing limit')
    raw = path.read_text(encoding='utf-8-sig')
    metadata: dict = {}
    body = raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            metadata = yaml.safe_load('\n'.join(lines[1:end])) or {}
            body = '\n'.join(lines[end + 1:])
    if not isinstance(metadata, dict):
        raise ValueError('Skill front matter must be a mapping')
    name = str(metadata.get('name') or directory.name)
    headings = '\n'.join(line for line in body.splitlines() if line.startswith('#'))
    descriptions = _descriptions(metadata)
    description = '\n'.join(descriptions)
    parts = ((name, description) if text_mode == 'description' and description.strip()
             else (name, description, headings, body))
    text = ('\n'.join((capability_card(name, descriptions[0] if descriptions else '', body),
                       *(" ".join(value.split())[:600] for value in descriptions[1:])))
            if text_mode == 'capability' else '\n'.join(parts))[:text_max_chars]
    document = SkillDocument(
        os.path.normcase(str(parent)), name, str(parent), text, '', description[:4000], {},
        root_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        root_stamp=(str(resolved), stamp), root_raw=raw,
        capability_description=description[:16384], catalog_root=str(root),
    )
    # Files may be replaced while being read. Do not cache a mixed version.
    if _root_version(document) != document.root_stamp:
        raise CatalogChanged('Skill root changed while indexing')
    return document


def read_catalog(
    roots: Sequence[Path], *, text_max_chars: int, text_mode: str = 'full',
    observed_files: dict | None = None, lightweight: bool = False,
    cache: CatalogCache | None = None,
) -> tuple[SkillDocument, ...]:
    """Keep the full legacy result by default; lightweight skips candidate details."""
    documents: dict[str, SkillDocument] = {}
    for root, directory, parent, resolved, stamp in iter_skill_files(roots):
        checkpoint()
        path = parent / 'SKILL.md'
        try:
            identity = os.path.normcase(str(parent))
            if identity not in documents and len(documents) >= 10000:
                raise RuntimeError('Local skill catalog exceeds the 10000 entry indexing limit')
            version = (str(resolved), stamp)
            key = (str(root), str(directory), text_mode, text_max_chars)
            document = cache.get_document(key, version) if cache is not None else None
            if document is None:
                document = _read_document(root, directory, parent, resolved, stamp,
                                          text_mode=text_mode, text_max_chars=text_max_chars)
                if cache is not None:
                    cache.put_root(document.id, document.root_sha256, document.root_raw)
                    cache.put_document(key, version, replace(document, root_raw=''))
            if not lightweight:
                document = hydrate_document(document, cache=cache, observed_files=observed_files)
            # A pipeline must not keep every SKILL.md alive outside the bounded
            # cache. Only the few selected candidates may reload evicted roots.
            documents[identity] = replace(document, root_raw='') if document.root_raw else document
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning('[SkillSelection] skipping unreadable skill %s: %s', path, exc)
    return tuple(documents.values())
