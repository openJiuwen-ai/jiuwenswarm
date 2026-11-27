# Retrieval

## Purpose

`retrieval/` owns online retrieval.

It loads offline artifacts built by `indexing/`, runs retrieval over them, and returns ordered candidates or payloads.

## Canonical Online Route

`auto` mode uses one unified route:

1. progressive LLM tree retrieval
2. embedding full-top-k backfill
3. BM25 full-top-k backfill
4. ordered dedupe merge

There are no parallel legacy retrieval strategies in the repository anymore.

## Package Layout

### `retrieval/io/`

- loads tree, catalog, embedding, and BM25 artifacts

### `retrieval/tree/`

- progressive tree search
- disclosure decisions
- branch reduction
- trace generation

### `retrieval/semantic/`

- embedding retrieval

### `retrieval/lexical/`

- BM25 retrieval

### `retrieval/merge/`

- appends and deduplicates backfill candidates

### `retrieval/protocols/`

- prompt generation
- display-name normalization
- output parsing

### `retrieval/service/`

- high-level retriever interfaces

## Main Entry Points

- [retrieval/service/retriever.py](/home/doujzc/codes/Demo/retrieval/service/retriever.py)
- [retrieval/tree/progressive.py](/home/doujzc/codes/Demo/retrieval/tree/progressive.py)

Typical usage:

```python
from retrieval.service.retriever import Retriever

retriever = Retriever.from_index("/abs/path/to/index")
payloads = retriever.search("find tools for browser automation", top_k=5)
```

## Dependency Boundary

`retrieval/` does not import orchestration core runtime. Orchestration consumes retrieval through canonical retrieval modules and the retrieval adapter layer under `orchestration/`.
