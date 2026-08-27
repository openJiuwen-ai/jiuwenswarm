# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""模型 context_window 字段单测。

覆盖两类行为：
1. 配置加载：models.agentos 列表的每个条目进入缓存（带 _source 标记、is_default=False、
   仅 model_name 非空时追加）。支持多个条目（同名/异名皆可）；填写约束为
   (model_name, api_base, api_key) 三元组唯一，故不存在完全相同的两条。
   agentos 只认 list 格式。
2. context_window（模型支持的上下文总长度）字段，**每个模型条目均可配**
   （defaults / agentos / video / audio / vision / image_gen，放各自 model_config_obj）：
   - 目标落点是 core 的 ModelRequestConfig（供 core 人员加正式字段后从
     self.model_config.context_window 取值）。
   - 是否在 jiuwenswarm 出口 pop 由 reasoning_injector.core_has_context_window_field
     自动适配 core 字段状态，无需人工同步：
     * core 已加正式字段：context_window 作正式字段，core 自行 exclude 不发厂商、
       self.model_config.context_window 可读 -> 不 pop，值留给 core。
   - 出口 pop 不再守 _source=="agentos"：所有条目（含 defaults）一视同仁，
     过渡期都 pop 防发厂商，core 加字段后都停止 pop。
   - 不再挂 _agentos_ctx_window 普通属性：旧机制是把该值喂给
     ContextEngineConfig.context_window_tokens（压缩阈值）的桥接，已拆除（见第 3 类）。
   - 所有模型条目均生效，AgentOS 的 _source 标记只表示配置来源。
3. 压缩阈值桥接已拆除：_deep_agent_context_engine_config 不再做 agentos per-model 覆盖，
   context_window_tokens 只取全局 react.context_engine_config 值（或 None 由 core 兜底），
   与 defaults 行为一致；签名简化为只接受 react_cfg。

注意：不再有 max_output_tokens（输出侧用户自定义已移除）。输出 token 上限完全由
core 默认行为决定，agentos 不参与。
"""

from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine.schema.config import ContextEngineConfig
from openjiuwen.core.foundation.llm.schema.config import ModelRequestConfig
from openjiuwen.harness import DeepAgent
from jiuwenswarm.common.config import get_default_models
from jiuwenswarm.common.context_window import resolve_context_window_tokens
from jiuwenswarm.common.reasoning_injector import (
    build_reasoning_model_request_kwargs,
    core_has_context_window_field,
)
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


def _agentos_block(name: str = "agentos-pro", *, with_ctx_window: bool = True,
                   context_window: int = 131072, api_key: str = "sk-y") -> dict:
    mco: dict = {"temperature": 0.95}
    if with_ctx_window:
        mco["context_window"] = context_window
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
        assert agentos[0]["model_config_obj"]["context_window"] == 131072

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


class TestContextWindowPopAdaptsToCoreField:
    """context_window 的出口行为随 core 的正式字段状态自动适配。"""

    @staticmethod
    def test_core_context_window_field_detection_matches_core_schema():
        # CI 可能使用尚未声明正式字段的 agent-core；检测结果应与 schema 一致。
        expected = "context_window" in ModelRequestConfig.model_fields
        assert core_has_context_window_field() is expected

    @staticmethod
    def test_agentos_context_window_adapts_to_core_field():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://y"},
            model_config_obj=agentos["model_config_obj"],
            model_name="agentos-pro",
        )
        if core_has_context_window_field():
            assert kwargs["context_window"] == 131072
        else:
            assert "context_window" not in kwargs
        assert "_source" not in kwargs

    @staticmethod
    def test_agentos_context_window_in_model_request_config_adapts_to_core_field():
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        dump = model.model_config.model_dump()
        if core_has_context_window_field():
            assert dump["context_window"] == 131072
        else:
            assert "context_window" not in dump
        assert "_source" not in dump
        assert dump.get("max_tokens") is None

    @staticmethod
    def test_agentos_no_longer_attaches_agentos_ctx_window_attr():
        # 旧机制（挂 Model._agentos_ctx_window 普通属性喂压缩阈值）已拆除
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        assert getattr(model, "_agentos_ctx_window", None) is None

    @staticmethod
    def test_agentos_context_window_popped_when_core_lacks_field(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: False,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://y"},
            model_config_obj=agentos["model_config_obj"],
            model_name="agentos-pro",
        )
        assert "context_window" not in kwargs
        assert "_source" not in kwargs

    @staticmethod
    def test_agentos_context_window_not_in_model_request_config_when_core_lacks_field(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: False,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        dump = model.model_config.model_dump()
        # 当前本地 core 已有正式字段，模拟过渡期时值会落为 None；旧 core
        # 则不会包含该字段。
        assert dump.get("context_window") is None
        assert "_source" not in dump
        assert dump.get("max_tokens") is None

    @staticmethod
    def test_defaults_context_window_also_popped_when_core_lacks_field(monkeypatch):
        # 守卫不再守 is_agentos：defaults 配的 context_window 过渡期同样被 pop 防发厂商
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: False,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        defaults = next(e for e in entries
                        if e.get("model_config_obj", {}).get("_source") != "agentos")
        mco = defaults["model_config_obj"]
        assert "_source" not in mco
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://x"},
            model_config_obj={**mco, "context_window": 999999},  # 模拟 defaults 配 context_window
            model_name="gpt-main",
        )
        # defaults 无 _source 标记，但守卫已不依赖它 -> 过渡期同样 pop
        assert "context_window" not in kwargs

    @staticmethod
    def test_defaults_context_window_kept_when_core_has_field(monkeypatch):
        # core 加字段后，defaults 的 context_window 也停止 pop，留给 core
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: True,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        defaults = next(e for e in entries
                        if e.get("model_config_obj", {}).get("_source") != "agentos")
        mco = defaults["model_config_obj"]
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://x"},
            model_config_obj={**mco, "context_window": 999999},
            model_name="gpt-main",
        )
        assert kwargs.get("context_window") == 999999

    @staticmethod
    def test_defaults_context_window_in_model_request_config(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: True,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        defaults = next(e for e in entries
                        if e.get("model_config_obj", {}).get("_source") != "agentos")
        model = build_model_from_entry(
            defaults["model_client_config"],
            {**defaults["model_config_obj"], "context_window": 999999},
        )
        assert getattr(model.model_config, "context_window", None) == 999999

    @staticmethod
    def test_model_request_config_max_tokens_none_for_agentos():
        # 双保险：agentos 的 ModelRequestConfig.max_tokens 始终 None
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        assert model.model_config.max_tokens is None


class TestContextWindowKeptWhenCoreHasField:
    """core 加 context_window 正式字段后，所有模型均保留该内部元数据。"""

    @staticmethod
    def test_agentos_context_window_kept_in_request_kwargs(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: True,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        kwargs = build_reasoning_model_request_kwargs(
            model_client_config={"client_provider": "OpenAI", "api_base": "http://y"},
            model_config_obj=agentos["model_config_obj"],
            model_name="agentos-pro",
        )
        # core 有字段 -> 不 pop，context_window 留给 core 读取
        assert kwargs.get("context_window") == 131072
        # _source 内部标记仍 pop（不进 ModelRequestConfig）
        assert "_source" not in kwargs

    @staticmethod
    def test_agentos_context_window_in_model_request_config_when_core_has_field(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: True,
        )
        entries = get_default_models(_config(agentos=[_agentos_block()]))
        agentos = next(e for e in entries
                       if e.get("model_config_obj", {}).get("_source") == "agentos")
        model = build_model_from_entry(agentos["model_client_config"], agentos["model_config_obj"])
        # core 有字段时 context_window 留在 ModelRequestConfig 的 extra（core 加正式字段后
        # 会被提升为正式字段 self.model_config.context_window）
        dump = model.model_config.model_dump()
        assert dump.get("context_window") == 131072
        assert "_source" not in dump
        assert dump.get("max_tokens") is None

    @staticmethod
    def test_default_context_window_in_model_request_config_when_core_has_field(monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.reasoning_injector.core_has_context_window_field",
            lambda: True,
        )
        entry = _defaults_entry()
        entry["model_config_obj"]["context_window"] = 999999
        model = build_model_from_entry(
            entry["model_client_config"],
            entry["model_config_obj"],
        )
        dump = model.model_config.model_dump()
        assert dump.get("context_window") == 999999
        assert dump.get("max_tokens") is None


class TestCompressionBridgeRemoved:
    """_deep_agent_context_engine_config 已拆除 agentos per-model 覆盖：
    context_window_tokens 只取全局 react.context_engine_config 值，
    不再从 Model._agentos_ctx_window / config 反查 max_tokens 覆盖。
    签名简化为只接受 react_cfg（旧 full_config/model_name/model 参数已删）。"""

    @staticmethod
    def test_legacy_call_unchanged():
        cfg = _config(react_cw=65536)
        cec = _deep_agent_context_engine_config(cfg["react"])
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_agentos_no_longer_overrides_context_window_tokens():
        # 旧路径 A 已拆：即便选中 agentos Model，context_window_tokens 仍取全局值，不被 131072 覆盖
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        cec = _deep_agent_context_engine_config(cfg["react"])
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_no_agentos_block_falls_back_to_react():
        cfg = _config(agentos=None, react_cw=65536)
        cec = _deep_agent_context_engine_config(cfg["react"])
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_agentos_without_context_window_does_not_change_global():
        cfg = _config(agentos=[_agentos_block(with_ctx_window=False)], react_cw=65536)
        cec = _deep_agent_context_engine_config(cfg["react"])
        assert cec.context_window_tokens == 65536

    @staticmethod
    def test_react_none_yields_none():
        # react 无 context_engine_config -> context_window_tokens=None（由 core 兜底）
        cec = _deep_agent_context_engine_config({})
        assert cec.context_window_tokens is None

    @staticmethod
    def test_signature_rejects_legacy_kwargs():
        # 新签名只接受 react_cfg；旧 full_config/model_name/model 实参应 TypeError
        cfg = _config(agentos=[_agentos_block()], react_cw=65536)
        with pytest.raises(TypeError):
            _deep_agent_context_engine_config(cfg["react"], full_config=cfg)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            _deep_agent_context_engine_config(cfg["react"], model_name="agentos-pro")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            _deep_agent_context_engine_config(cfg["react"], model=object())  # type: ignore[call-arg]


class TestContextWindowPrecedence:
    """模型列表与运行时 fallback 共享同一套窗口优先级。"""

    @staticmethod
    def test_selected_model_window_beats_explicit_model_mapping():
        config = {
            "react": {
                "context_engine_config": {
                    "model_context_window_tokens": {"agentos-pro": 90000},
                }
            }
        }
        assert resolve_context_window_tokens(
            "agentos-pro",
            context_engine_config=config["react"],
            model_config_obj={"_source": "agentos", "context_window": 131072},
        ) == 131072

    @staticmethod
    def test_global_window_beats_selected_model_window():
        config = {
            "react": {
                "context_engine_config": {
                    "context_window_tokens": 65536,
                }
            }
        }
        assert resolve_context_window_tokens(
            "agentos-pro",
            context_engine_config=config["react"],
            model_config_obj={"_source": "agentos", "context_window": 131072},
        ) == 65536

    @staticmethod
    def test_explicit_mapping_is_used_when_selected_model_window_is_missing():
        config = {
            "context_engine_config": {
                "model_context_window_tokens": {"agentos-pro": 90000},
            }
        }
        assert resolve_context_window_tokens(
            "agentos-pro",
            context_engine_config=config,
            model_config_obj={"_source": "agentos"},
        ) == 90000

    @staticmethod
    def test_default_model_window_beats_explicit_model_mapping():
        config = {
            "context_engine_config": {
                "model_context_window_tokens": {"gpt-main": 90000},
            }
        }
        assert resolve_context_window_tokens(
            "gpt-main",
            context_engine_config=config,
            model_config_obj={"context_window": 131072},
        ) == 131072

    @staticmethod
    def test_deep_agent_uses_global_before_model_request_window():
        deep_agent = DeepAgent.__new__(DeepAgent)
        deep_agent._react_agent = SimpleNamespace(
            config=SimpleNamespace(
                model_name="agentos-pro",
                model_config_obj=ModelRequestConfig(
                    model="agentos-pro",
                    context_window=131072,
                ),
                context_engine_config=ContextEngineConfig(
                    context_window_tokens=65536,
                    model_context_window_tokens={"agentos-pro": 90000},
                ),
            )
        )
        assert deep_agent._resolve_context_window_tokens() == 65536

        deep_agent._react_agent.config.context_engine_config = ContextEngineConfig(
            model_context_window_tokens={"agentos-pro": 90000},
        )
        expected = 131072 if core_has_context_window_field() else 90000
        assert deep_agent._resolve_context_window_tokens() == expected
