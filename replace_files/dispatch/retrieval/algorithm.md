# Retrieval Algorithm

## Problem Definition

Given:

- a loaded offline index
- a user query
- optional LLM
- optional embedding model

Return:

- the best matching executable payloads in ranked order

## Unified Retrieval Policy

`auto` mode follows this policy:

1. if LLM is available, run progressive tree retrieval first
2. if embedding is available, generate embedding top-`k` results
3. always generate BM25 top-`k` results when BM25 index is available
4. append later-stage results after earlier-stage results, skipping duplicates

This means the backfill stages always generate their own full `top_k` candidate lists. They are not restricted to the number of remaining slots.

## Stage 1: Progressive Tree Retrieval

Input:

- query
- tree root
- `top_k`

Process:

1. start from the current visible subtree
2. if the structure is trivial, use deterministic shortcuts
3. otherwise ask the LLM to choose among the current visible boundary nodes
4. recurse into selected branches or terminate on selected items
5. reduce branch results to the requested `top_k`

Important rules:

- every LLM routing decision sees only the current visible subtree
- the model outputs the visible boundary node display names only
- display names are uniquified automatically if collisions exist
- single-candidate situations do not call the LLM

## Stage 2: Embedding Backfill

Input:

- query
- embedding index
- `top_k`

Process:

1. embed the query
2. retrieve embedding top-`k` candidates
3. keep the original embedding order
4. append only candidates not already chosen by progressive retrieval

## Stage 3: BM25 Backfill

Input:

- query
- BM25 index
- `top_k`

Process:

1. normalize and tokenize the query
2. score all indexed documents with BM25
3. retrieve BM25 top-`k` candidates
4. append only candidates not already chosen by earlier stages

## Merge Rule

Final order is:

1. progressive head results
2. embedding backfill results not already selected
3. BM25 backfill results not already selected

The merge is append-only and stable within each stage.

## Fallback Cases

- no LLM + embedding available: `embedding + bm25`
- no LLM + no embedding: `bm25`
- no BM25 index: skip BM25 stage
- no embedding index: skip embedding stage

## Main Implementations

- [retrieval/tree/progressive.py](/home/doujzc/codes/Demo/retrieval/tree/progressive.py)
- [retrieval/semantic/embedding.py](/home/doujzc/codes/Demo/retrieval/semantic/embedding.py)
- [retrieval/lexical/bm25.py](/home/doujzc/codes/Demo/retrieval/lexical/bm25.py)
- [retrieval/merge/append.py](/home/doujzc/codes/Demo/retrieval/merge/append.py)
- [retrieval/service/retriever.py](/home/doujzc/codes/Demo/retrieval/service/retriever.py)
