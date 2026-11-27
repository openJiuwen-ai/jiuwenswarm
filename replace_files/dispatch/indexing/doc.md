# Indexing

## Purpose

`indexing/` owns offline artifact construction.

Given a set of skill directories, it builds the retrieval assets consumed later by `retrieval/` and `orchestration/`.

## Main Outputs

- `tree_index.yaml`
- `catalog.jsonl`
- `embedding_records.jsonl`
- `embedding_index.json`
- `bm25_index.json`
- `manifest.json`

## Main Components

### `indexing/tree/`

- scans skills
- builds the capability tree
- supports LLM-driven tree construction and fallback tree generation

### `indexing/catalog/`

- defines catalog records
- builds retrieval text used by embedding and BM25 stages

### `indexing/embedding/`

- builds embedding records and embedding index files

### `indexing/bm25/`

- builds BM25 documents and BM25 index files

### `indexing/io/`

- reads and writes tree, catalog, and manifest artifacts

### `indexing/workflows/`

- coordinates full builds and incremental add/delete rebuilds

## Main Entry Point

- [indexing/workflows/index_builder.py](/home/doujzc/codes/Demo/indexing/workflows/index_builder.py)

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
