# Retrieval

## Purpose

`retrieval/` owns online retrieval.

It loads offline artifacts built by `indexing/`, runs retrieval over them, and returns ordered candidates or payloads.

## Canonical Online Route

The canonical route uses the progressive tree index:

1. load `tree_index.yaml` and `catalog.jsonl`
2. route the query through the progressive tree
3. return selected leaf payloads from the catalog

## Package Layout

### `retrieval/io/`

- loads tree and catalog artifacts

### `retrieval/tree/`

- progressive tree search
- disclosure decisions
- branch reduction
- trace generation

### `retrieval/protocols/`

- prompt generation
- display-name normalization
- output parsing

### `retrieval/service/`

- high-level retriever interfaces

## Main Entry Points

- `retrieval/service/retriever.py`
- `retrieval/tree/progressive.py`

Typical usage:

```python
from retrieval.service import RequestConfig, Retriever

retriever = Retriever.from_index("/abs/path/to/index")
payloads = retriever.search(
    "find tools for browser automation",
    search_config=RequestConfig(top_k=5, tags=("mobile",)),
)
```

Tag filtering is a deterministic pre-filter: disallowed leaves and their empty
ancestor branches are removed before the first LLM routing call. With an empty
`tags` tuple, retrieval uses the complete tree. With a non-empty tuple,
untagged candidates are excluded and candidates tagged `all` remain eligible by
default. A candidate must contain every requested tag unless it carries the
reserved wildcard tag `all`.

## Dependency Boundary

`retrieval/` does not import orchestration core runtime. Orchestration consumes retrieval through canonical retrieval modules and the retrieval adapter layer under `orchestration/`.
