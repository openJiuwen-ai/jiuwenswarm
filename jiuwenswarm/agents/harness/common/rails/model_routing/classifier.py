"""model_routing.classifier — 分类器加载工具。

本模块提供以下纯函数，由 _build_model_routing 在 build 时显式调用：
- load_mapper_config()     — 加载 classifier_mapper.json
- load_classifier_impl()   — 从 mapper.classifier.source 文本编译分类器函数（exec 注入）
- validate_score()         — 验 classify 返回值，0-100 否则兜底 50
- task_score()             — 查 score_table → default_score → 50

公开工具函数（source 文本可通过 import 引用）：
- _build_llm_model()       — 从 extras dict 构建 LLM Model 对象（带缓存）
- _parse_classifier_response() — 解析 LLM 输出 → (category, difficulty)
- _lookup_score()           — 查分数表 → int

分类器加载机制：
  classifier_mapper.json 的 classifier.source → 函数体文本
  → exec(compile("async def classify(prompt_text):\n{source}")) → classify 函数

  namespace 注入（仅注入数据，不注入函数）：
    _EXTRAS              dict — classifier.extras 字段（用户自定义配置）
    _CATEGORIES          tuple — categories 字段
    _DIFFICULTIES         tuple — difficulties 字段
    _SCORE_TABLE          dict — score 字段解析后 {(category,difficulty)->int}
    _DEFAULT_SCORE        dict — 难度默认分 {easy:10, medium:30, hard:50}
    imports 列表指定的模块（如 re、json）

  工具函数不自动注入——source 文本需要时自行 import：
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import (
        _build_llm_model, _parse_classifier_response, _lookup_score
    )
"""
from __future__ import annotations
import importlib
import json
import textwrap
from pathlib import Path
from typing import Any
from jiuwenswarm.common.utils import logger


# --------------------------------------------------------------------------- #
# 公开工具函数（source 文本可通过 import 引用）
# --------------------------------------------------------------------------- #

_LLM_MODEL_CACHE: dict[str, Any] = {}


def _build_llm_model(extras: dict) -> Any | None:
    """从 extras dict 构建 LLM Model 对象（带缓存）。

    extras 需含: api_base, api_key, model_name, client_provider, temperature。
    """
    cache_key = (
        f"{extras.get('api_base', '')}:{extras.get('model_name', '')}"
        f":{extras.get('client_provider', '')}:{extras.get('api_key', '')}"
    )
    cached = _LLM_MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        from openjiuwen.core.foundation.llm import ModelClientConfig, Model
        mcc = ModelClientConfig(
            api_base=extras.get("api_base", ""),
            api_key=extras.get("api_key", ""),
            model_name=extras.get("model_name", ""),
            client_provider=extras.get("client_provider", "OpenAI"),
            timeout=1800,
            verify_ssl=False,
        )
        from openjiuwen.core.foundation.llm.model_clients.base_model_client import ModelRequestConfig
        mrc = ModelRequestConfig(
            model_name=extras.get("model_name", ""),
            temperature=extras.get("temperature", 0),
        )
        model = Model(model_client_config=mcc, model_config=mrc)
        _LLM_MODEL_CACHE[cache_key] = model
        return model
    except Exception as exc:
        logger.debug("[ModelRouting] _build_llm_model failed: %s", exc)
        return None


def _parse_classifier_response(
    content: str,
    categories: tuple[str, ...] | None = None,
    difficulties: tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """解析 LLM 分类器返回的 JSON 文本 → (category, difficulty)。"""
    import re
    cats = categories or ("chat", "reasoning", "coding", "summarization", "format")
    diffs = difficulties or ("easy", "medium", "hard")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
        cat = str(obj.get("category", "unknown")).strip().lower()
        dif = str(obj.get("difficulty", "unknown")).strip().lower()
        cat = cat if cat in cats else "unknown"
        dif = dif if dif in diffs else "unknown"
        return cat, dif
    except Exception as exc:
        logger.debug("[ModelRouting] parse classifier JSON failed: %s", exc)
    cat_m = re.search(r'"?category"?\s*[:=]\s"?([a-zA-Z]+)"?', text, re.IGNORECASE)
    dif_m = re.search(r'"?difficulty"?\s*[:=]\s"?([a-zA-Z]+)"?', text, re.IGNORECASE)
    cat = cat_m.group(1).lower() if cat_m and cat_m.group(1).lower() in cats else "unknown"
    dif = dif_m.group(1).lower() if dif_m and dif_m.group(1).lower() in diffs else "unknown"
    return cat, dif


def _lookup_score(
    category: str,
    difficulty: str,
    score_table: dict | None = None,
    default_score: dict | None = None,
) -> int:
    """查 score_table → default_score → 50。"""
    st = score_table or {}
    ds = default_score or {"easy": 10, "medium": 30, "hard": 50}
    v = st.get((category, difficulty))
    if v is not None:
        return v
    return ds.get(difficulty, 50)


# --------------------------------------------------------------------------- #
# routing_state 文件缺失补拷（build rail 时的安全网）
# --------------------------------------------------------------------------- #

_ROUTING_STATE_FILES = ("classifier_mapper.json", "model_capability_map.json")


def ensure_routing_state_files() -> None:
    """检查 routing_state 目录下的 JSON 文件，缺失的从包内模板补拷。

    已存在的文件不覆盖（保留用户的自定义）。
    在 _build_model_routing 调用 load_mapper_config 之前执行。
    """
    import shutil
    try:
        from jiuwenswarm.common.utils import get_config_dir, _find_package_root
    except Exception as exc:
        logger.debug("[ModelRouting] import utils failed in ensure_routing_state_files: %s", exc)
        return
    try:
        pkg_root = _find_package_root()
        if pkg_root is None:
            return
        src_dir = pkg_root / "resources" / "model_routing"
        dst_dir = get_config_dir() / "routing_state"
        dst_dir.mkdir(parents=True, exist_ok=True)
        for fn in _ROUTING_STATE_FILES:
            src = src_dir / fn
            dst = dst_dir / fn
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
                logger.info("[ModelRouting] missing file restored: %s → %s", fn, dst)
    except Exception as exc:
        logger.warning("[ModelRouting] routing_state file restore failed: %s", exc)


# --------------------------------------------------------------------------- #
# classifier_mapper.json 加载（categories / difficulties / score / classifier 映射表）
# --------------------------------------------------------------------------- #

def _mapper_paths() -> list[Path]:
    """classifier_mapper.json 的候选路径（用户目录优先，包内兜底）。"""
    paths = []
    try:
        from jiuwenswarm.common.utils import get_config_dir
        paths.append(get_config_dir() / "routing_state" / "classifier_mapper.json")
    except Exception as exc:
        logger.debug("[ModelRouting] get_config_dir failed: %s", exc)
    try:
        from jiuwenswarm.common.utils import _find_package_root
        paths.append(_find_package_root() / "resources" / "model_routing" / "classifier_mapper.json")
    except Exception as exc:
        logger.debug("[ModelRouting] _find_package_root failed: %s", exc)
    return paths


def load_mapper_config() -> dict:
    """从 classifier_mapper.json 加载分类/分数/分类器映射配置。

    按优先级：
    1. ~/.jiuwenswarm/config/routing_state/classifier_mapper.json（用户自定义）
    2. <package>/resources/model_routing/classifier_mapper.json（包内兜底）

    若用户文件缺 classifier 字段 → 自动从包内模板补上默认值（合并策略）。

    返回 dict，含 categories / difficulties / score_table / default_score / classifier。
    加载失败 → raise（build 不建 rail）。
    """
    paths = _mapper_paths()
    config_path = next((p for p in paths if p.exists()), None)
    if config_path is None:
        raise FileNotFoundError(
            f"classifier_mapper.json not found; tried: {', '.join(str(p) for p in paths)}"
        )

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    if not isinstance(cfg, dict) or not cfg.get("categories") or not cfg.get("difficulties"):
        raise ValueError(f"classifier_mapper.json invalid: {config_path}")

    cats = tuple(str(c) for c in cfg["categories"])
    diffs = tuple(str(d) for d in cfg["difficulties"])

    score_table: dict[tuple[str, str], int] = {}
    raw_score = cfg.get("score", {})
    if isinstance(raw_score, dict):
        for k, v in raw_score.items():
            parts = str(k).split(".", 1)
            if len(parts) == 2 and isinstance(v, (int, float)):
                score_table[(parts[0], parts[1])] = int(v)

    # 合 classifier 配置：用户文件缺 classifier → 从包内模板补默认值
    classifier_cfg = cfg.get("classifier")
    if classifier_cfg is None:
        pkg_path = next((p for p in reversed(paths) if p.exists()), None)
        if pkg_path and pkg_path != config_path:
            try:
                with open(pkg_path, encoding="utf-8") as f_pkg:
                    pkg_cfg = json.load(f_pkg)
                classifier_cfg = pkg_cfg.get("classifier", {})
                logger.info("[ModelRouting] classifier cfg merged from package template %s", pkg_path)
            except Exception:
                classifier_cfg = {}
        else:
            classifier_cfg = {}

    logger.info(
        "[ModelRouting] mapper config loaded from %s: categories=%s difficulties=%s num_scores=%d",
        config_path, cats, diffs, len(score_table),
    )
    return {
        "categories": cats,
        "difficulties": diffs,
        "score_table": score_table,
        "default_score": {"easy": 10, "medium": 30, "hard": 50},
        "classifier": classifier_cfg,
    }


# --------------------------------------------------------------------------- #
# 分类器实现加载（文本注入 → exec compile）
# --------------------------------------------------------------------------- #

def load_classifier_impl(mapper: dict) -> tuple[Any, str]:
    """从 mapper.classifier.source 文本编译分类器函数。

    流程：
    1. 读取 classifier.source 内联文本（函数体）
    2. 构建 exec namespace（仅注入数据：_EXTRAS/_CATEGORIES/_DIFFICULTIES/_SCORE_TABLE/_DEFAULT_SCORE）
    3. 注入 imports 列表指定的模块
    4. 编译 async def classify(prompt_text):\n{source}
    5. exec() → 得到 classify 函数对象

    工具函数不自动注入——source 文本需要时自行 import：
      from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import (
          _build_llm_model, _parse_classifier_response, _lookup_score
      )

    构建失败 → raise（_build_model_routing 不建 rail）。
    """
    clf_cfg = mapper.get("classifier")
    if not clf_cfg or not isinstance(clf_cfg, dict):
        raise ValueError("classifier_mapper.json missing 'classifier' field")

    source_text = clf_cfg.get("source")
    if not source_text or not source_text.strip():
        raise ValueError("classifier.source is empty")

    logger.info(
        "[ModelRouting] classifier source text loaded: %d chars, extras keys=%s",
        len(source_text),
        list(clf_cfg.get("extras", {}).keys()),
    )

    # ── 构建 exec namespace（仅数据，不注入函数）──
    extras = clf_cfg.get("extras", {})
    ns: dict[str, Any] = {
        "_EXTRAS": extras,
        "_CATEGORIES": mapper.get("categories", ("chat", "reasoning", "coding", "summarization", "format")),
        "_DIFFICULTIES": mapper.get("difficulties", ("easy", "medium", "hard")),
        "_SCORE_TABLE": mapper.get("score_table", {}),
        "_DEFAULT_SCORE": mapper.get("default_score", {"easy": 10, "medium": 30, "hard": 50}),
    }

    # ── 注入 imports ──
    for mod_name in clf_cfg.get("imports", []):
        try:
            ns[mod_name] = importlib.import_module(mod_name)
        except ImportError as exc:
            raise ImportError(f"classifier.imports module '{mod_name}' not found: {exc}") from exc

    # ── 编译 ──
    func_src = f"async def classify(prompt_text):\n{textwrap.indent(source_text.strip(), '    ')}"
    try:
        code = compile(func_src, "<classifier_source>", "exec")
    except SyntaxError as e:
        raise SyntaxError(
            f"classifier.source syntax error: {e.msg} (line {e.lineno})\n"
            f"Text:\n{func_src}"
        ) from e

    local_ns: dict = {}
    try:
        exec(code, ns, local_ns)
    except Exception as exc:
        raise RuntimeError(f"classifier.source exec failed: {exc}") from exc

    classify_fn = local_ns.get("classify")
    if classify_fn is None or not callable(classify_fn):
        raise RuntimeError("classifier.source produced no callable 'classify' function")

    logger.info("[ModelRouting] classifier loaded OK (text-injection)")
    return classify_fn, "text-injection:classifier_mapper.json"


# --------------------------------------------------------------------------- #
# 分数验证
# --------------------------------------------------------------------------- #

def validate_score(raw: Any) -> int:
    """验证 classify 返回的 score：必须是 0-100 的数值；否则兜底 50。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 50
    if v < 0 or v > 100:
        return 50
    return v


def task_score(category: str, difficulty: str, mapper: dict) -> int:
    """从 mapper 的 score_table 查 (category, difficulty) → score；兜底按难度。"""
    score_table = mapper.get("score_table", {})
    default_score = mapper.get("default_score", {})
    v = score_table.get((category, difficulty))
    if v is not None:
        return v
    return default_score.get(difficulty, 50)
