# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS 备份模型的输入侧上下文窗口字段单测。

覆盖两类行为：
1. 配置加载：models.agentos 列表的每个条目进入缓存（带 _source 标记、is_default=False、
   仅 model_name 非空时追加）。支持多个条目（同名/异名皆可）；填写约束为
   (model_name, api_base, api_key) 三元组唯一，故不存在完全相同的两条。
   agentos 只认 list 格式。
2. 输入侧 max_tokens 别名（用户可读的上下文窗口上限）：
   - 不进 core 的 ModelRequestConfig.max_tokens（绝不作为输出 token 上限发往厂商）：
     reasoning_injector._build_model_request_kwargs 在公共出口对 agentos 统一 pop 掉
     max_tokens，覆盖 build_model_from_entry / config.validate_model / warmup 等所有路径。
   - 不进 ModelRequestConfig 的 extra（防 base_model_client 经 model_dump 透传给 SDK
     抛 unexpected keyword argument）：build_model_from_entry 把该值挂到 **Model 普通属性**
     _agentos_ctx_window（Model 是普通 Python 类，普通属性不进 model_dump）。
   - 只进 core 的 ContextEngineConfig.context_window_tokens（压缩阈值，不发厂商）：
     _deep_agent_context_engine_config 的 per-model 覆盖——传了 model 时从选中 Model 的
     普通属性 _agentos_ctx_window 读（路径 A，精确，同名多条目可区分"选中哪个用哪个的值"）；
     仅在未传 model 时才从 config agentos 列表按 model_name 反查取首个（路径 B，兜底）。
     路径 B 不在"传了 model 但读不到值"时启用，防本条目无 max_tokens 时取到同名别的值。

注意：不再有 max_output_tokens（输出侧用户自定义已移除）。输出 token 上限完全由
core 默认行为决定，agentos 不参与。
"""

import pytest

from jiuwenswarm.common.config import get_default_models
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _deep_agent_context_engine_config,
    build_model_from_entry,
)


def _defaults_entry(name: str = "gpt-main") -> dict:
    return {
        "model_client_config": {
            "api_base": "http://x",
            "api_key": "sk-x",
            "model_name": name,
            "client_provider": "OpenAI",
            "timeout": 360,
            "verify_ssl": True,
            "custom_headers": {},
        },
        "model_config_obj": {"temperature": 0.95},
        "is_default": True,
    }


def _agentos_block(name: str = "agentos-pro", *, with_max_tokens: bool = True,
                   max_tokens: int = 131072, api_key: str = "sk-y") -> dict:
    mco: dict = {"temperature": 0.95}
    if with_max_tokens:
        mco["max_tokens"] = max_tokens
    return {
        "model_client_config": {
            "api_base": "http://y",
            "api_key": api_key,
            "model_name": name,
            "client_provider": "OpenAI",
            "verify_ssl": True,
            "timeout": 1800,
        },
        "model_config_obj": mco,
    }


def _config(*, agentos=None, react_cw: int | None = 65536) -> dict:
    """构造测试 config。agentos 参数：None=无 agentos；list=agentos 列表。"""
    cfg: dict = {"models": {"defaults": [_defaults_entry()]}}
    if agentos is not None:
        cfg["models"]["agentos"] = agentos
    react: dict = {}
    if react_cw is not None:
        react = {"context_engine_config": {"context_window_tokens": react_cw}}
    cfg["react"] = react
    return cfg


class TestAgentosConfigLoading:
    """get_default_models 对 agentos 列表的加载行为。"""

    @staticmethod
    def test_agentos_list_appended_with_source_and_no_default():
        cfg = _config(agentos=[_agentos_block("agentos-pro")])
        entries = get_default_models(cfg)
        agentos = [e for e in entries if e.get("model_config_obj", {}).get("_source") == "agentos"]
        assert len(agentos) == 1
        assert agentos[0]["is_default"] is False
        assert agentos[0]["model_client_config"]["model_name"] == "agentos-pro"
        assert agentos[0]["model_config_obj"]["max_tokens"] == 131072

    @staticmethod
    def test_agentos_list_multiple_entries():
        cfg = _config(agentos=[_agentos_block("agentos-a"), _agentos_block("agentos-b")])
        entries = get_default_models(cfg)
        agentos = [e for e in entries if e.get("model_config_obj", {}).get("_source") == "agentos"]
        assert len(agentos) == 2
        names = {e["model_client_config"]["model_name"] for e in agentos}
        assert names == {"agentos-a", "agentos-b"}
        assert all(e["is_default"] is False for e in agentos)

    @staticmethod
    def test_agentos_list_same_name_both_appended():
        # 同名两条 agentos（api_base/api_key 不同 -> 三元组唯一、合法）：都追加
        cfg = _config(agentos=[
            _agentos_block("dup", api_key="sk-1"),
            _agentos_block("dup", api_key="sk-2"),
        ])
        entries = get_default_models(cfg)
        agentos = [e for e in entries if e.get("model_config_obj", {}).get("_source") == "agentos"]
        assert len(agentos) == 2
        keys = {e["model_client_config"]["api_key"] for e in agentos}
        assert keys == {"sk-1", "sk-2"}

    @staticmethod
    def test_agentos_not_appended_when_model_name_empty():
        # model_name 为空 = 该条未配置，跳过不入缓存
        cfg = _config(agentos=[_agentos_block("")])
        entries = get_default_models(cfg)
        assert all(e.get("model_config_obj", {}).get("_source") != "agentos" for e in entries)

    @staticmethod
    def test_agentos_absent_does_not_affect_defaults():
        entries = get_default_models(_config(agentos=None))
        assert len(entries) == 1
        assert entries[0]["model_client_config"]["model_name"] == "gpt-main"
        assert entries[0]["is_default"] is True

    @staticmethod
    def test_agentos_never_competes_for_default_flag():
        # agentos 始终 is_default=False，即便与 defaults 同名也不抢默认
        cfg = {"models": {"defaults": [_defaults_entry()], "agentos": [_agentos_block("gpt-main")]},
               "react": {}}
        entries = get_default_models(cfg)
        agentos = [e for e in entries if e.get("model_config_obj", {}).get("_source") == "agentos"]
        assert len(agentos) == 1
        assert agentos[0]["is_default"] is False
        defaults = [e for e in entries if e.get("model_config_obj", {}).get("_source") != "agentos"]
        assert defaults and defaults[0]["is_default"] is True


class TestAgentosMaxTokensNotInModelRequestConfig:
    """agentos 的 max_tokens 是输入侧别名，绝不进 core 的 ModelRequestConfig.max_tokens
    （否则会被误当成输出 token 上限发往厂商）。build_model_from_entry 把它挪到 extra
    键 _agentos_ctx_window（随 Model 带入缓存），并从输出侧 kwargs pop 掉 max_tokens。"""

    @staticmethod
    def test_agentos_max_tokens_not_set_on_model_request_config():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        # 输出侧字段 max_tokens 必须为 None（输入侧值已挪走，不进 ModelRequestConfig）
        assert model.model_config.max_tokens is None
        # 输入侧值挂到 Model 普通属性（不进 ModelRequestConfig 的 extra）
        assert getattr(model, "_agentos_ctx_window", None) == 131072

    @staticmethod
    def test_agentos_max_tokens_not_in_model_dump():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        dump = model.model_config.model_dump()
        # max_tokens 是声明字段，dump 里必有此键；关键是值必须为 None
        assert dump.get("max_tokens") is None
        # _source 是 extra，不应残留
        assert "_source" not in dump
        # _agentos_ctx_window 挂在 Model 普通属性上，绝不进 ModelRequestConfig 的
        # extra——否则 base_model_client._build_request_params 会经 model_dump
        # 透传给 SDK，导致 agentos 推理抛 unexpected keyword argument（P1 回归点）。
        assert "_agentos_ctx_window" not in dump
        # 双保险：Model 普通属性上有值（输入侧窗口仍可被路径 A 读取）
        assert getattr(model, "_agentos_ctx_window", None) == 131072

    @staticmethod
    def test_defaults_max_tokens_remains_none():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        defaults = next(e for e in entries
                        if e.get("model_config_obj", {}).get("_source") != "agentos")
        model = build_model_from_entry(defaults["model_client_config"], defaults["model_config_obj"])
        assert model.model_config.max_tokens is None
        # defaults 不带 agentos 普通属性
        assert getattr(model, "_agentos_ctx_window", None) is None

    @staticmethod
    def test_source_marker_stripped_by_reasoning_injector():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://y"},
            model_config_obj=agentos["model_config_obj"],
            model_name="agentos-pro",
        )
        # _source 不应流到 SDK kwargs（_agentos_ctx_window 本就不进 kwargs，改挂 Model 属性）
        assert "_source" not in kwargs
        assert "_agentos_ctx_window" not in kwargs

    @staticmethod
    def test_agentos_ctx_window_not_in_model_request_config_dump():
        # _agentos_ctx_window 不进 ModelRequestConfig（改挂 Model 普通属性），
        # 故 model_dump 不含它；关键是 max_tokens 仍为 None、_source 不残留。
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://y"},
            model_config_obj=agentos["model_config_obj"],
            model_name="agentos-pro",
        )
        from openjiuwen.core.foundation.llm.schema.config import ModelRequestConfig
        mrc = ModelRequestConfig(**kwargs)
        assert getattr(mrc, "_agentos_ctx_window", None) is None
        assert getattr(mrc, "_source", None) is None
        assert mrc.max_tokens is None


class TestContextWindowTokensOverride:
    """_deep_agent_context_engine_config 的 per-model context_window_tokens 覆盖（仅 agentos）。

    路径 A（首选）：传了 model 时，从选中 Model 的普通属性 _agentos_ctx_window 读——精确，同名可区分。
    路径 B（回退）：仅在未传 model 时，从 config agentos 列表按 model_name 反查（兜底）。
    """

    @staticmethod
    def test_legacy_call_unchanged_without_full_config():
        cfg = _config(react_cw=65536)
        cec = _deep_agent_context_engine_config(cfg["react"])
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_agentos_match_overrides_context_window_via_model():
        # 路径 A：传入选中 Model，直接从其普通属性读
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        entries = get_default_models(cfg)
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="agentos-pro", model=model
        )
        # agentos 的 max_tokens(131072) 覆盖了全局 react 基础值 65536
        assert cec.context_window_tokens == 131072

    @staticmethod
    def test_agentos_match_overrides_via_pathb_when_no_model():
        # 路径 B：不传 model，从 config agentos 列表按 model_name 反查
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="agentos-pro"
        )
        assert cec.context_window_tokens == 131072

    @staticmethod
    def test_defaults_name_not_overridden():
        # 请求 defaults 的 model_name -> 不匹配 agentos -> 用 react 基础值
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="gpt-main"
        )
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_defaults_model_not_overridden_via_extra():
        # 选中 defaults 的 Model 不带 _agentos_ctx_window extra -> 不覆盖
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        entries = get_default_models(cfg)
        defaults = next(e for e in entries
                        if e.get("model_config_obj", {}).get("_source") != "agentos")
        model = build_model_from_entry(defaults["model_client_config"], defaults["model_config_obj"])
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="gpt-main", model=model
        )
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_no_agentos_block_falls_back_to_react():
        cfg = _config(agentos=None, react_cw=65536)
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="anything"
        )
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_agentos_without_max_tokens_not_overriding():
        # agentos 块没配 max_tokens -> Model 不带 _agentos_ctx_window 属性 -> 不覆盖，回退基础值
        cfg = _config(agentos=[_agentos_block(with_max_tokens=False)], react_cw=65536)
        entries = get_default_models(cfg)
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="agentos-pro", model=model
        )
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_same_name_different_max_tokens_uses_selected():
        # 核心用例：两条同名 agentos，max_tokens 不同；路径 A 从选中 Model 读，
        # 能精确"选中哪个用哪个的值"。
        cfg = _config(agentos=[
            _agentos_block("dup", max_tokens=131072, api_key="sk-1"),
            _agentos_block("dup", max_tokens=65536, api_key="sk-2"),
        ], react_cw=32768)
        entries = get_default_models(cfg)
        agentos_entries = [e for e in entries
                           if e.get("model_config_obj", {}).get("_source") == "agentos"]
        assert len(agentos_entries) == 2
        m0 = build_model_from_entry(agentos_entries[0]["model_client_config"],
                                    agentos_entries[0]["model_config_obj"])
        m1 = build_model_from_entry(agentos_entries[1]["model_client_config"],
                                    agentos_entries[1]["model_config_obj"])
        # 选中 m0 -> 用 m0 的 131072
        cec0 = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="dup", model=m0
        )
        assert cec0.context_window_tokens == 131072
        # 选中 m1 -> 用 m1 的 65536（而非首个的 131072）
        cec1 = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="dup", model=m1
        )
        assert cec1.context_window_tokens == 65536

    @staticmethod
    def test_same_name_pathb_takes_first_match():
        # 路径 B（不传 model）同名时取首个匹配——这是路径 B 的已知局限，
        # 路径 A 存在的意义即在于此。此用例固化路径 B 的兜底行为。
        cfg = _config(agentos=[
            _agentos_block("dup", max_tokens=131072, api_key="sk-1"),
            _agentos_block("dup", max_tokens=65536, api_key="sk-2"),
        ], react_cw=32768)
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="dup"  # 不传 model -> 路径 B
        )
        # 首个匹配是 131072
        assert cec.context_window_tokens == 131072

    @staticmethod
    def test_model_without_attr_does_not_fallback_to_pathb():
        # P2 回归：传了 model，但该 Model 无 _agentos_ctx_window 属性（agentos
        # 未配 max_tokens），且 config 里存在同名带 max_tokens 的 agentos 条目——
        # 路径 B 不应启用，否则会用别人的值错误覆盖，应保持全局基础值。
        cfg = _config(agentos=[
            # 本条目标同名、但没配 max_tokens -> Model 不带 _agentos_ctx_window
            _agentos_block("dup", with_max_tokens=False, api_key="sk-1"),
            # 另一条同名、配了 max_tokens=131072（路径 B 若启用会命中它）
            _agentos_block("dup", max_tokens=131072, api_key="sk-2"),
        ], react_cw=32768)
        entries = get_default_models(cfg)
        agentos_entries = [e for e in entries
                           if e.get("model_config_obj", {}).get("_source") == "agentos"]
        # 选中第一条（无 max_tokens）
        m0 = build_model_from_entry(agentos_entries[0]["model_client_config"],
                                    agentos_entries[0]["model_config_obj"])
        assert getattr(m0, "_agentos_ctx_window", None) is None  # 确无属性
        cec = _deep_agent_context_engine_config(
            cfg["react"], full_config=cfg, model_name="dup", model=m0
        )
        # 路径 B 未启用 -> 不被第二条的 131072 覆盖，保持全局基础值 32768
        assert cec.context_window_tokens == 32768
