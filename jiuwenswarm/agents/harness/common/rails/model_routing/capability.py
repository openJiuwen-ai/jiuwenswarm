"""model_routing.capability — ModelCapability + 能力表构建。"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from jiuwenswarm.common.utils import logger


@dataclass
class ModelCapability:
    """模型能力表条目。

    model_name / model_group / model_provider / model_expertise_category /
    model_cost / model_performance / model_score / is_trusted / model_type。
    """

    model_name: str
    max_length: int = 65535  # 上下文窗口占位
    model_group: str = "unknown"  # 由条目顶层 model_group 提供，缺省 "unknown"
    model_provider: str = "unknown"  # 厂商，由条目顶层 model_provider 提供（非 config 的 service provider）
    model_expertise_category: list[str] = field(default_factory=list)  # e.g. ["coding", "reasoning"]
    model_cost: int = 0  # 相对成本
    model_performance: int = 0  # 基准得分
    model_score: int = 0  # 综合评分
    is_trusted: bool = False  # 可信标记（如内部端点）
    model_type: str = ""  # 模型类型（vision / audio / video / "" = 普通）

    # 框架扩展字段（set_llm 用，不在能力表内）
    model_id: Optional[str] = None  # 唯一标识=client_id；token 统计 key，同模型多 API 靠它区分
    model: Optional[Any] = None  # openjiuwen Model 实例引用
    # 持久化累积 token 用量（加载时由 _load_persisted_table 从 model_routing_list.json 合并）
    token_used: dict[str, Any] = field(default_factory=dict)


def _build_cap_from_entry(
    entry: Any,
    model_builder: Optional[Callable[[dict, dict], Any]] = None,
    *,
    force_model_type: str = "",
) -> ModelCapability:
    """从单个 config 条目（model_client_config + model_config_obj + 顶层字段）建一个 cap。

    - model_name：``model_client_config.model_name``（无则 alias / "unknown"）。
    - model_group / model_provider：条目顶层 ``model_group``/``model_provider``，缺省 "unknown"。
    - model_type：``force_model_type`` 优先，否则条目顶层 ``model_type``。
    - is_trusted：条目顶层读。
    - model_cost / model_performance / model_score / max_length：条目顶层读，缺省默认。
    - model：model_builder 构建（真切换用）；缺省则 model=None 仅推荐不切换。
    """
    mcc = entry.get("model_client_config", {}) if isinstance(entry, dict) else {}
    mco = entry.get("model_config_obj", {}) if isinstance(entry, dict) else {}
    name = str(mcc.get("model_name", "") or entry.get("alias", "") or "unknown")
    group = str(entry.get("model_group", "") or "unknown") if isinstance(entry, dict) else "unknown"
    vendor = str(entry.get("model_provider", "") or "unknown") if isinstance(entry, dict) else "unknown"

    def _int(field_name: str, default: int) -> int:
        """能力数值：config 条目 > 默认。0 视为合法值（不误判为 falsy）。"""
        ev = entry.get(field_name) if isinstance(entry, dict) else None
        if ev is not None and ev != "":
            try:
                return int(ev)
            except (TypeError, ValueError):
                pass
        return default

    is_trusted = bool(entry.get("is_trusted", False)) if isinstance(entry, dict) else False
    explicit_cid = str(mcc.get("client_id", "") or "").strip()
    api_base = str(mcc.get("api_base", "") or "")
    _api_key_raw = mcc.get("api_key", "")
    _api_key = _api_key_raw if isinstance(_api_key_raw, str) and _api_key_raw else ""
    if explicit_cid:
        model_id = explicit_cid
    else:
        model_id = hashlib.sha256(
            f"{name}|{api_base}|{_api_key}".encode("utf-8")
        ).hexdigest()[:12]
    model_obj: Optional[Any] = None
    if model_builder is not None and mcc:
        try:
            model_obj = model_builder(mcc, mco)
        except Exception as exc:
            logger.debug("[ModelRouting] model_builder failed for %s: %s", name, exc)
            model_obj = None
    if force_model_type:
        model_type = force_model_type
    else:
        model_type = str(entry.get("model_type", "") or "").strip().lower() if isinstance(entry, dict) else ""
    return ModelCapability(
        model_name=name,
        model_group=group,
        model_provider=vendor,
        is_trusted=is_trusted,
        model_type=model_type,
        model_id=model_id,
        model=model_obj,
        model_score=_int("model_score", 0),
        model_performance=_int("model_performance", 0),
        model_cost=_int("model_cost", 0),
        max_length=_int("max_length", 65535),
        model_expertise_category=(
            list(entry.get("model_expertise_category", []) or [])
            if isinstance(entry, dict)
            else []
        ),
    )


def _load_models_json() -> dict | None:
    """加载 sidecar 模式落盘的 models.json（relay spawn 前写入）。

    路径：``get_config_dir()/routing_state/models.json``。结构对齐 config.yaml::models::

        {"defaults": [<entry>, ...], "vision": {<entry>}}

    每个 entry 含 ``model_client_config`` / ``model_config_obj`` / 顶层能力字段，
    直接喂 ``_build_cap_from_entry``。key 在文件里（与 tip 文件同级安全，不进 os.environ）。

    sidecar 链路识别信号：文件存在 → sidecar 模式（读文件）；缺失 → stock 模式（读 config.yaml）。
    缺失/解析失败 → 返回 None（调用方回退 config.yaml）。
    """
    try:
        from jiuwenswarm.common.utils import get_config_dir
        path = get_config_dir() / "routing_state" / "models.json"
    except Exception as exc:
        logger.debug("[ModelRouting] get_config_dir failed in _load_models_json: %s", exc)
        return None
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info("[ModelRouting] models.json loaded from %s", path)
            return data
        logger.warning("[ModelRouting] models.json not a dict at %s, ignored", path)
        return None
    except Exception as exc:
        logger.warning("[ModelRouting] models.json load failed (%s): %s", path, exc)
        return None


def build_capability_table_from_config(
    config: dict[str, Any] | None,
    *,
    model_builder: Optional[Callable[[dict, dict], Any]] = None,
) -> list[ModelCapability]:
    """从能力表来源加载 ModelCapability 列表（启动时/配置更新时调用）。

    来源优先级（sidecar 模式优先）：
    1. ``routing_state/models.json``（relay sidecar spawn 前落盘，含全模型 + key）
       → ``json["defaults"]``，每条 -> 一个 cap（model_type 从条目顶层读）。
    2. 回退 ``config.yaml::models.defaults``（stock 模式）。

    vision 专用模型：优先 ``models.json::vision``，回退 ``config.yaml::models.vision``。
    api_base 配了才进表，作为 ``model_type="vision"`` 候选，仅含图请求时参与路由。
    """
    # 优先 models.json（sidecar 模式）；缺失回退 config.yaml（stock 模式）
    models_json = _load_models_json()
    json_defaults = models_json.get("defaults") if isinstance(models_json, dict) else None
    if isinstance(json_defaults, list):
        entries = json_defaults
    else:
        try:
            from jiuwenswarm.common.config import get_default_models
            entries = get_default_models(config) if config is not None else []
        except Exception as exc:
            logger.debug("[ModelRouting] load capability table failed: %s", exc)
            return []
    table: list[ModelCapability] = [_build_cap_from_entry(e, model_builder) for e in entries]
    # vision 专用模型：models.json::vision > config.yaml::models.vision
    vision_cfg = None
    if isinstance(models_json, dict):
        vision_cfg = models_json.get("vision")
    if vision_cfg is None:
        vision_cfg = (config or {}).get("models", {}).get("vision")
    if isinstance(vision_cfg, dict):
        vmcc = vision_cfg.get("model_client_config", {}) or {}
        if isinstance(vmcc, dict) and str(vmcc.get("api_base") or "").strip():
            table.append(_build_cap_from_entry(vision_cfg, model_builder, force_model_type="vision"))
    return table
