# Indexing

## Purpose

`indexing/` owns offline progressive tree index construction.

Given skill/plugin material directories or pre-scanned JSONL, it builds the tree, catalog, and manifest artifacts consumed later by `retrieval/` and orchestration.

## Main Outputs

- `tree_index.yaml`
- `catalog.jsonl`
- `manifest.json`

Each candidate can carry normalized tags. Directory builds read `tags` from
`SKILL.md` frontmatter, while pre-scanned JSONL builds read `tags` from
`contentExtendParam`. Tags are written to each `catalog.jsonl` row, while
`manifest.json.tag_counts` summarizes the available filter values.

## Main Components

### `indexing/tree/`

- scans skills
- builds the capability tree
- supports LLM-driven tree construction and fallback tree generation

### `indexing/catalog/`

- defines catalog records
- builds the leaf catalog used by online progressive retrieval

### `indexing/io/`

- reads and writes tree, catalog, and manifest artifacts

### `indexing/workflows/`

- coordinates full builds and incremental add/delete rebuilds
- writes only the progressive tree artifacts listed above

## Main Entry Point

- `indexing/workflows/index_builder.py`

Typical usage:

```python
from indexing.workflows.index_builder import IndexBuilder

IndexBuilder.build(
    item_paths=["/abs/path/to/skills"],
    output_dir="/abs/path/to/index",
)
```

## Dependency Boundary

`indexing/` does not depend on `retrieval.service` or orchestration runtime code. It produces artifacts; it does not execute online retrieval.
