"""model_routing.capability — ModelCapability + table builder + ranking."""
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
    model_group: str = "unknown"  # 由 model_name 本地映射
    model_provider: str = "unknown"  # 厂商，由 model_name 本地映射（非 config 的 service provider）
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


def _capability_rank(cap: ModelCapability) -> float:
    """能力排序值：高=更强。优先 model_score，次 model_performance。不考虑 model_size。"""
    for val in (cap.model_score, cap.model_performance):
        try:
            v = float(val)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


# --------------------------------------------------------------------------- #
# 能力映射表（model_capability_map.json）
# --------------------------------------------------------------------------- #
# 两部分：
# - vendor_map：model_name 子串 -> (model_group, model_provider)，按顺序首匹配（前缀越长放越前）。
# - models：按 model_name 精确匹配的能力覆盖（model_cost / model_performance / model_score /
#   max_length / model_group / model_provider）。命中则覆盖 config 条目里的同名字段。
#
# 查找顺序：~/.jiuwenswarm/config/routing_state/model_capability_map.json（用户自定义）
#         > <package>/resources/model_capability_map.json（包内模板）
# 文件缺失或解析失败 -> 返回空表（group/provider 回退 "unknown"，无 cost/score 覆盖），不抛。


def _ensure_user_copy(filename: str) -> None:
    """确保用户 routing_state 目录下有 filename 的副本。

    若用户目录下不存在，从包内模板拷贝过去，让用户可以自定义覆盖。
    已存在则不覆盖（保留用户的修改）。
    """
    import shutil
    try:
        from jiuwenswarm.common.utils import get_config_dir, _find_package_root
    except Exception as exc:
        logger.debug("[ModelRouting] import utils failed in _ensure_user_copy: %s", exc)
        return
    try:
        pkg_root = _find_package_root()
        if pkg_root is None:
            return
        src = pkg_root / "resources" / "model_routing" / filename
        if not src.exists():
            return
        dst_dir = get_config_dir() / "routing_state"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / filename
        if not dst.exists():
            shutil.copy2(src, dst)
            logger.info("[ModelRouting] copied template %s to %s", filename, dst)
    except Exception as exc:
        logger.debug("[ModelRouting] template copy for %s failed: %s", filename, exc)


def _load_capability_map() -> dict:
    """加载 model_capability_map.json，返回 {"vendor_map": [...], "models": {...}}。

    先确保用户目录有模板副本（从包内拷贝），然后按顺序加载：
    1. ~/.jiuwenswarm/config/routing_state/model_capability_map.json（用户自定义）
    2. <package>/resources/model_routing/model_capability_map.json（包内兜底）

    文件缺失或解析失败 -> 返回空表（group/provider 回退 "unknown"，无 cost/score 覆盖），不抛。
    """
    _ensure_user_copy("model_capability_map.json")

    paths: list = []
    try:
        from jiuwenswarm.common.utils import get_config_dir
        paths.append(get_config_dir() / "routing_state" / "model_capability_map.json")
    except Exception as exc:
        logger.debug("[ModelRouting] get_config_dir failed: %s", exc)
    try:
        from jiuwenswarm.common.utils import _find_package_root
        pr = _find_package_root()
        if pr is not None:
            paths.append(pr / "resources" / "model_routing" / "model_capability_map.json")
    except Exception as exc:
        logger.debug("[ModelRouting] _find_package_root failed: %s", exc)

    path = next((p for p in paths if p.exists()), None)
    if path is None:
        logger.debug(
            "[ModelRouting] capability map not found, tried: %s",
            ", ".join(str(p) for p in paths) or "(no paths)",
        )
        return {"vendor_map": [], "models": {}}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as exc:
        logger.warning("[ModelRouting] capability map load failed (%s): %s", path, exc)
        return {"vendor_map": [], "models": {}}

    vendor_map: list[tuple[str, str, str]] = []
    raw_vm = cfg.get("vendor_map") if isinstance(cfg, dict) else None
    if isinstance(raw_vm, list):
        for item in raw_vm:
            if isinstance(item, dict):
                pfx = str(item.get("prefix", "") or "").lower()
                grp = str(item.get("group", "") or "")
                prov = str(item.get("provider", "") or "")
                if pfx:
                    vendor_map.append((pfx, grp, prov))
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                vendor_map.append((str(item[0]).lower(), str(item[1]), str(item[2])))
    models = cfg.get("models") if isinstance(cfg, dict) else None
    if not isinstance(models, dict):
        models = {}
    logger.info(
        "[ModelRouting] capability map loaded from %s: vendor_prefixes=%d models=%d",
        path, len(vendor_map), len(models),
    )
    return {"vendor_map": vendor_map, "models": models}


_CAP_MAP: dict = _load_capability_map()


def _map_model_group_provider(model_name: str) -> tuple[str, str]:
    """按 model_name 子串映射 (model_group, 厂商)。未命中返回 ("unknown","unknown")。

    例：GLM-5.1 -> ("GLM", "zhipu")；qwen3-max -> ("Qwen", "alibaba")。
    前缀表来自 model_capability_map.json::vendor_map，按顺序首匹配。
    """
    name = (model_name or "").lower()
    for prefix, group, vendor in _CAP_MAP.get("vendor_map", ()):
        if prefix and prefix in name:
            return group, vendor
    return "unknown", "unknown"


def _build_cap_from_entry(
    entry: Any,
    model_builder: Optional[Callable[[dict, dict], Any]] = None,
    *,
    force_model_type: str = "",
) -> ModelCapability:
    """从单个 config 条目（model_client_config + model_config_obj + 顶层字段）建一个 cap。

    - model_name：``model_client_config.model_name``（无则 alias / "unknown"）。
    - model_group / model_provider：``_map_model_group_provider`` 按 model_name 推导；
      model_capability_map.json::models[name] 里的 model_group/model_provider 可覆盖。
    - model_type：``force_model_type`` 优先，否则条目顶层 ``model_type``。
    - is_trusted：条目顶层读。
    - model_cost / model_performance / model_score / max_length：优先用
      model_capability_map.json::models[name] 里的值（命中覆盖），否则条目顶层值，否则默认。
    - model：model_builder 构建（真切换用）；缺省则 model=None 仅推荐不切换。
    """
    mcc = entry.get("model_client_config", {}) if isinstance(entry, dict) else {}
    mco = entry.get("model_config_obj", {}) if isinstance(entry, dict) else {}
    name = str(mcc.get("model_name", "") or entry.get("alias", "") or "unknown")
    group, vendor = _map_model_group_provider(name)
    ovr = _CAP_MAP.get("models", {}).get(name) if isinstance(_CAP_MAP.get("models"), dict) else None
    ovr = ovr if isinstance(ovr, dict) else {}

    def _int(field_name: str, default: int) -> int:
        """能力数值：config 条目 > map 覆盖 > 默认。0 视为合法值（不误判为 falsy）。"""
        ev = entry.get(field_name) if isinstance(entry, dict) else None
        if ev is not None and ev != "":
            try:
                return int(ev)
            except (TypeError, ValueError):
                pass
        if field_name in ovr and ovr[field_name] is not None:
            try:
                return int(ovr[field_name])
            except (TypeError, ValueError):
                return default
        return default

    group = str(ovr.get("model_group") or group)
    vendor = str(ovr.get("model_provider") or vendor)
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


def build_capability_table_from_config(
    config: dict[str, Any] | None,
    *,
    model_builder: Optional[Callable[[dict, dict], Any]] = None,
) -> list[ModelCapability]:
    """从 config.yaml::models.defaults 加载能力表（启动时/配置更新时调用）。

    - 普通模型：``models.defaults`` 列表，每条 -> 一个 cap（model_type 从条目顶层读）。
    - vision 专用模型：``models.vision``（api_base 配了才进表），作为 ``model_type="vision"`` 候选，
      仅含图请求时参与路由（``_decide_and_select`` 在非含图请求里排除 vision 候选）。
    - 能力字段（cost/performance/score/max_length/group/provider）由
      model_capability_map.json 按 model_name 覆盖（命中即用），未命中回退 config 条目值/默认。
    """
    try:
        from jiuwenswarm.common.config import get_default_models

        entries = get_default_models(config) if config is not None else []
    except Exception as exc:
        logger.debug("[ModelRouting] load capability table failed: %s", exc)
        return []
    table: list[ModelCapability] = [_build_cap_from_entry(e, model_builder) for e in entries]
    # vision 专用模型（models.vision，api_base 配了才进表）
    vision_cfg = (config or {}).get("models", {}).get("vision")
    if isinstance(vision_cfg, dict):
        vmcc = vision_cfg.get("model_client_config", {}) or {}
        if isinstance(vmcc, dict) and str(vmcc.get("api_base") or "").strip():
            table.append(_build_cap_from_entry(vision_cfg, model_builder, force_model_type="vision"))
    return table
