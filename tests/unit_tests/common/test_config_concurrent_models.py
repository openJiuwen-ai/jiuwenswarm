# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""并发写 models.defaults 的回归测试：验证文件锁不丢失条目。

复现原 bug 场景：多线程/多进程同时写 model 配置，裸 load-modify-dump 会丢失更新，
update_config（threading.Lock + portalocker 文件锁）应保证全部条目最终都在配置里。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

import jiuwenswarm.common.config as cfg_mod


def _seed_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "defaults": [
                        {
                            "model_client_config": {
                                "api_base": "https://base.example.com/v1",
                                "api_key": "seed-key",
                                "model_name": "seed-model",
                                "client_provider": "OpenAI",
                            },
                            "model_config_obj": {"temperature": 0.95},
                            "is_default": True,
                        }
                    ]
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _make_entry(name: str) -> dict[str, Any]:
    return {
        "model_client_config": {
            "api_base": f"https://m{name}.example.com/v1",
            "api_key": f"key-{name}",
            "model_name": name,
            "client_provider": "OpenAI",
        },
        "model_config_obj": {"temperature": 0.95},
        "is_default": False,
    }


@pytest.fixture
def patched_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _seed_config(cfg)
    monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", cfg)
    return cfg


def test_concurrent_add_no_lost_update(patched_config):
    """N 线程并发 append model：最终所有 N+seed 条目都在 defaults 里。

    用单事务 update_config（读最新 + 追加 + 写回）模拟正确用法，验证文件锁
    在并发下不丢条目。对比：若用 ensure_defaults_list + update_default_models
    两步（中间无锁），会丢更新——这正是原 bug。
    """
    names = [f"glm-{i}" for i in range(20)]
    errors: list[BaseException] = []

    def _add(name: str) -> None:
        entry = _make_entry(name)
        try:
            def _mutate(data):
                models = data.get("models") or {}
                if not isinstance(models, dict):
                    models = {}
                defs = models.get("defaults")
                defs = list(defs) if isinstance(defs, list) else []
                if any(isinstance(d, dict) and d.get("model_client_config", {}).get("model_name") == name
                       for d in defs):
                    return None
                defs.append(entry)
                models["defaults"] = defs
                data["models"] = models
                if "default" in data["models"]:
                    del data["models"]["default"]
                return data
            cfg_mod.update_config(_mutate)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=_add, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出现异常: {errors}"

    data = cfg_mod.load_yaml_round_trip(patched_config)
    defaults = data["models"]["defaults"]
    got_names = {
        e["model_client_config"]["model_name"] for e in defaults if isinstance(e, dict)
    }
    expected = set(names) | {"seed-model"}
    assert got_names == expected, (
        f"丢失更新: 期望 {len(expected)} 个模型, 实际 {len(got_names)} 个; "
        f"缺失: {expected - got_names}"
    )


def test_ensure_defaults_creates_placeholder_when_missing(patched_config):
    """defaults 不存在时写入 ${API_BASE} 等模板占位符条目（保持原契约，供 _config_set 后续填充）。"""
    raw = cfg_mod.load_yaml_round_trip(patched_config)
    raw["models"].pop("defaults")
    raw["models"].pop("default", None)
    cfg_mod.dump_yaml_round_trip(patched_config, raw)

    defs = cfg_mod.ensure_defaults_list_in_config()
    assert isinstance(defs, list) and len(defs) == 1
    mcc = defs[0].get("model_client_config", {})
    assert mcc.get("api_base") == "${API_BASE}"
    assert mcc.get("model_name") == "${MODEL_NAME}"

    data = cfg_mod.load_yaml_round_trip(patched_config)
    written = data["models"].get("defaults")
    assert isinstance(written, list) and len(written) == 1
    assert written[0].get("model_client_config", {}).get("api_key") == "${API_KEY}"


def test_update_config_retries_on_concurrent_change(patched_config):
    """并发写同一字段时 update_config 不丢计数（文件锁串行化，无丢失更新）。"""
    barrier = threading.Barrier(2)
    results: dict[str, int] = {"ok": 0}

    def _writer() -> None:
        barrier.wait()
        for _ in range(10):

            def _m(data):
                data["counter"] = data.get("counter", 0) + 1
                return data

            cfg_mod.update_config(_m)
            results["ok"] += 1

    ts = [threading.Thread(target=_writer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    data = cfg_mod.load_yaml_round_trip(patched_config)
    assert data.get("counter") == 20, f"丢失计数: {data.get('counter')}"


def test_ensure_defaults_returns_locked_snapshot_without_write(patched_config):
    """defaults 已存在时：mutator 返回 None→update_config 返回锁内读到的快照，
    不触发磁盘写，调用方拿到锁内可信值（无锁外重读的 TOCTOU）。"""
    before_mtime = patched_config.stat().st_mtime_ns
    defs = cfg_mod.ensure_defaults_list_in_config()
    after_mtime = patched_config.stat().st_mtime_ns

    assert isinstance(defs, list) and len(defs) == 1
    assert defs[0]["model_client_config"]["model_name"] == "seed-model"
    assert before_mtime == after_mtime, "未变更却触发了磁盘写"


def test_atomic_replace_retries_on_permission_error(patched_config, monkeypatch):
    """_atomic_replace 对 PermissionError 自动重试，最终成功（不静默无操作）。"""
    src = patched_config.parent / "tmp_src.yaml"
    src.write_text("x: 1", encoding="utf-8")
    call_count = {"n": 0}
    real_replace = os.replace

    def _flaky_replace(s, d):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise PermissionError("simulated concurrent hold")
        return real_replace(s, d)

    dst = patched_config.parent / "tmp_dst.yaml"
    monkeypatch.setattr(os, "replace", _flaky_replace)
    try:
        cfg_mod._atomic_replace(src, dst)
        assert call_count["n"] == 3, f"应重试到第3次成功，实际 {call_count['n']}"
        assert dst.read_text(encoding="utf-8") == "x: 1"
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def test_atomic_replace_raises_when_exhausted(patched_config, monkeypatch):
    """_atomic_replace 总尝试次数耗尽后抛 PermissionError，不静默返回。"""
    src = patched_config.parent / "tmp_src2.yaml"
    src.write_text("x: 1", encoding="utf-8")
    dst = patched_config.parent / "tmp_dst2.yaml"

    def _always_fail(s, d):
        raise PermissionError("simulated persistent hold")

    monkeypatch.setattr(os, "replace", _always_fail)
    try:
        with pytest.raises(PermissionError):
            cfg_mod._atomic_replace(src, dst, max_attempts=2)
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def test_config_set_models_embed_in_one_transaction(patched_config):
    """/config set 改主模型+多模态+embed 三段同改，单事务原子写、字段不丢。"""
    def _mutate(data):
        # 主模型 defaults[0]
        models = data.get("models") or {}
        if not isinstance(models, dict):
            models = {}
            data["models"] = models
        defs = models.get("defaults")
        if not (isinstance(defs, list) and defs):
            defs = [{
                "model_client_config": {"api_base": "${API_BASE}", "api_key": "${API_KEY}",
                                        "model_name": "${MODEL_NAME}", "client_provider": "${MODEL_PROVIDER}"},
                "model_config_obj": {"temperature": 0.95}, "is_default": True}]
            models["defaults"] = defs
        mcc = defs[0].setdefault("model_client_config", {})
        mcc["api_base"] = "https://main-changed.example.com/v1"
        mcc["model_name"] = "main-changed"
        # 多模态 vision
        vision = models.setdefault("vision", {})
        vision.setdefault("model_client_config", {})["model_name"] = "vision-changed"
        # embed
        embed = data.setdefault("embed", {})
        embed["embed_model"] = "embed-changed"
        return data

    cfg_mod.update_config(_mutate)
    data = cfg_mod.load_yaml_round_trip(patched_config)
    assert data["models"]["defaults"][0]["model_client_config"]["model_name"] == "main-changed"
    assert data["models"]["defaults"][0]["model_client_config"]["api_base"] == "https://main-changed.example.com/v1"
    assert data["models"]["vision"]["model_client_config"]["model_name"] == "vision-changed"
    assert data["embed"]["embed_model"] == "embed-changed"
    # 原主模型的其他字段未丢(只改了 name+api_base，api_key 仍在)
    assert data["models"]["defaults"][0]["model_client_config"]["api_key"] == "seed-key"


def test_config_set_and_model_add_concurrent(patched_config):
    """/config set 改主模型 与 /model add 追加 并发交叉，互不丢失。"""
    errors: list[BaseException] = []

    def _config_set_main():
        def _m(data):
            models = data.get("models") or {}
            defs = models.get("defaults")
            if isinstance(defs, list) and defs and isinstance(defs[0], dict):
                defs[0].setdefault("model_client_config", {})["api_base"] = "https://cfg-set.example.com/v1"
            return data
        try:
            for _ in range(15):
                cfg_mod.update_config(_m)
        except BaseException as e:
            errors.append(e)

    def _model_add():
        try:
            for i in range(15):
                entry = _make_entry(f"add-{i}")
                def _m(data, e=entry):
                    models = data.get("models") or {}
                    defs = list(models.get("defaults") or [])
                    defs.append(e)
                    models["defaults"] = defs
                    data["models"] = models
                    return data
                cfg_mod.update_config(_m)
        except BaseException as e:
            errors.append(e)

    t1 = threading.Thread(target=_config_set_main)
    t2 = threading.Thread(target=_model_add)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"并发异常: {errors}"
    data = cfg_mod.load_yaml_round_trip(patched_config)
    defs = data["models"]["defaults"]
    names = {e["model_client_config"]["model_name"] for e in defs if isinstance(e, dict)}
    # /config set 改的 api_base 不能被 /model add 覆盖回 seed
    first_mcc = defs[0]["model_client_config"]
    assert first_mcc["api_base"] == "https://cfg-set.example.com/v1", "主模型 api_base 被并发覆盖丢失"
    # 15 个 add 全在
    missing = {f"add-{i}" for i in range(15)} - names
    assert not missing, f"/model add 的条目在并发 /config set 下丢失: {missing}"


def test_mutation_validation_error_does_not_write(patched_config):
    """mutator 抛校验异常时不写盘（_command_model add/switch TOCTOU 修复的关键保证）。

    模拟 _command_model 的事务模式：mutator 在锁内做校验，校验失败抛异常，
    update_config 不应执行 dump，文件内容不变。
    """
    class _ValidationError(Exception):
        pass

    before_mtime = patched_config.stat().st_mtime_ns
    with pytest.raises(_ValidationError):
        def _m(data):
            models = data.get("models") or {}
            defs = models.get("defaults") or []
            if any(isinstance(d, dict) and d.get("model_client_config", {}).get("model_name") == "seed-model"
                   for d in defs):
                raise _ValidationError("duplicate")
            return data
        cfg_mod.update_config(_m)
    after_mtime = patched_config.stat().st_mtime_ns
    assert before_mtime == after_mtime, "校验失败时不应写盘"


def test_concurrent_model_add_no_lost_update(patched_config):
    """模拟 _command_model add 单事务：N 线程并发追加模型，全部条目最终都在。"""
    names = [f"cmd-add-{i}" for i in range(20)]
    errors: list[BaseException] = []

    def _add(name: str) -> None:
        entry = _make_entry(name)
        try:
            def _m(data):
                models = data.get("models") or {}
                if not isinstance(models, dict):
                    models = {}
                    data["models"] = models
                defs = models.get("defaults")
                defs = list(defs) if isinstance(defs, list) else []
                if any(isinstance(d, dict) and d.get("model_client_config", {}).get("model_name") == name
                       for d in defs):
                    return None
                defs.append(entry)
                models["defaults"] = defs
                if "default" in models:
                    models.pop("default", None)
                return data
            cfg_mod.update_config(_m)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=_add, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 add 异常: {errors}"
    data = cfg_mod.load_yaml_round_trip(patched_config)
    got = {e["model_client_config"]["model_name"] for e in data["models"]["defaults"] if isinstance(e, dict)}
    expected = set(names) | {"seed-model"}
    assert got == expected, f"_command_model add 并发丢条目: 缺 {expected - got}"
