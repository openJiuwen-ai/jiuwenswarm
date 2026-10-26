# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RESULTS = 8
DEFAULT_PAID_PROVIDER_ORDER = ("petal", "tavily", "perplexity", "bocha", "serper", "jina")

PAID_API_KEYS = {
    "tavily": "TAVILY_API_KEY",
    "bocha": "BOCHA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "serper": "SERPER_API_KEY",
    "jina": "JINA_API_KEY",
}

KNOWN_PAID_PROVIDERS = frozenset(PAID_API_KEYS.keys() | {"petal"})

MIN_SNIPPET_AVG_LEN = 25
MIN_ANSWER_ONLY_LEN = 100
MAX_RESULTS_CAP = 20
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 120
