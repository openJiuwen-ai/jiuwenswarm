# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Hidden imports for JiuwenMemory's in-process agent and video profiles."""

# These layers contain the kernel's string-based Producer registrations. Keep
# optional model runtimes and remote storage backends outside the collected roots.
MEMORY_RUNTIME_PACKAGES = (
    "jiuwen_memory.api",
    "jiuwen_memory.config",
    "jiuwen_memory.common.security",
    "jiuwen_memory.common.normalizer.normalizer_impl",
    "jiuwen_memory.construction",
    "jiuwen_memory.control",
    "jiuwen_memory.ingest",
    "jiuwen_memory.retrieval",
    "jiuwen_memory_adapter.jiuwenswarm",
)

# The adapter defaults use local Markdown + SQLite shadow indexes, in-memory
# fallback stores, OpenAI-compatible clients and lightweight text components.
# Do not collect all of jiuwen_memory: that also includes optional BGE, HanLP,
# spaCy, Milvus, PostgreSQL, Redis and Elasticsearch implementations.
MEMORY_RUNTIME_MODULES = (
    "jiuwen_memory.common.audit.audit_impl.in_memory_audit_logger",
    "jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger",
    "jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker",
    "jiuwen_memory.common.chunker.chunker_impl.recursive_chunker",
    "jiuwen_memory.common.embedder.embedder_impl.hashing_embedder",
    "jiuwen_memory.common.embedder.embedder_impl.openai_embedder",
    "jiuwen_memory.common.feature_extractor.feature_extractor_impl.keyword_feature_extractor",
    "jiuwen_memory.common.llm.llm_impl.echo_llm",
    "jiuwen_memory.common.llm.llm_impl.openai_llm",
    "jiuwen_memory.common.lock.lock_impl.in_memory_lock",
    "jiuwen_memory.common.log.log_impl.default_log_setup",
    "jiuwen_memory.common.reranker.reranker_impl.overlap_reranker",
    "jiuwen_memory.common.tokenizer.tokenizer_impl.jieba_tokenizer",
    "jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer",
    "jiuwen_memory.storage.domain_store_impl.composite_domain_store",
    "jiuwen_memory.storage.domain_store_impl.graph_recaller",
    "jiuwen_memory.storage.domain_store_impl.keyword_recaller",
    "jiuwen_memory.storage.domain_store_impl.shadow_recaller",
    "jiuwen_memory.storage.domain_store_impl.vector_recaller",
    "jiuwen_memory.storage.fs_impl.in_memory_fs_store",
    "jiuwen_memory.storage.fs_impl.local_fs",
    "jiuwen_memory.storage.fs_impl.encrypted_fs_store",
    "jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store",
    "jiuwen_memory.storage.fusion_impl.in_memory_fusion_store",
    "jiuwen_memory.storage.graph_impl.in_memory_graph_store",
    "jiuwen_memory.storage.kv_impl.in_memory_kv_store",
    "jiuwen_memory.storage.kv_impl.sqlite_kv_store",
    "jiuwen_memory.storage.kv_impl.encrypted_kv_store",
    "jiuwen_memory.storage.markdown_impl.local_markdown_store",
    "jiuwen_memory.storage.shadow_impl.sqlite_shadow_index",
    "jiuwen_memory.storage.store_manager_impl.composite_store_manager",
    "jiuwen_memory.storage.vector_impl.in_memory_vector_store",
    "jiuwen_memory.storage.watchdog_impl.local_watchdog",
)
