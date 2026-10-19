# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""只读 YAML 解析器的兼容性、安全限制及 C 扩展回退。"""

import pytest
import yaml

from jiuwenswarm.common import utils


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="当前 PyYAML 未提供 C 扩展")
def test_each_of_four_loads_uses_c_safe_loader_without_caching(tmp_path, monkeypatch):
    template, user = tmp_path / "template.yaml", tmp_path / "user.yaml"
    template.write_text("nested:\n  value: 1\n", encoding="utf-8")
    user.write_text("message: 中文\n", encoding="utf-8")
    calls = []

    class CountedCSafeLoader(yaml.CSafeLoader):
        def __init__(self, stream):
            calls.append(stream.name)
            super().__init__(stream)

    def forbidden_load(*args, **kwargs):
        pytest.fail("C 解析路径不应调用被 CodeCheck 禁止的通用反序列化入口")

    monkeypatch.setattr(yaml, "CSafeLoader", CountedCSafeLoader)
    monkeypatch.setattr(yaml, "load", forbidden_load)
    first = utils.load_yaml_dict(template)
    first["nested"]["value"] = 99
    assert utils.load_yaml_dict(template) == {"nested": {"value": 1}}
    assert utils.load_yaml_dict(template) == {"nested": {"value": 1}}
    assert utils.load_yaml_dict(user) == {"message": "中文"}
    assert calls == [str(path) for path in (template, template, template, user)]


def test_safe_loader_fallback_when_c_extension_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    config = tmp_path / "fallback.yaml"
    config.write_text("message: 中文\nflag: true\n", encoding="utf-8")
    calls = []
    original = yaml.safe_load

    def counted_load(stream):
        calls.append(stream.name)
        return original(stream)

    monkeypatch.setattr(yaml, "safe_load", counted_load)
    assert utils.load_yaml_dict(config) == {"message": "中文", "flag": True}
    assert calls == [str(config)]


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="当前 PyYAML 未提供 C 扩展")
@pytest.mark.parametrize("text", ["value: 1\n", "value: [unterminated\n", "value: !!python/object/apply:builtins.str [123]\n"])
def test_c_loader_is_disposed_on_success_and_parse_errors(tmp_path, monkeypatch, text):
    config = tmp_path / "dispose.yaml"
    config.write_text(text, encoding="utf-8")
    disposed = []

    class TrackedCSafeLoader(yaml.CSafeLoader):
        def dispose(self):
            disposed.append(self)
            super().dispose()

    monkeypatch.setattr(yaml, "CSafeLoader", TrackedCSafeLoader)
    if text == "value: 1\n":
        assert utils.load_yaml_dict(config) == {"value": 1}
    else:
        with pytest.raises(yaml.YAMLError):
            utils.load_yaml_dict(config)
    assert len(disposed) == 1


def _typed(value):
    if isinstance(value, dict):
        return type(value), frozenset((_typed(k), _typed(v)) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return type(value), tuple(_typed(v) for v in value)
    return type(value), value


@pytest.mark.parametrize("text", [
    "message: 你好 👋\nprompt: |\n  第一行\n  second line\nenv: ${UNSET_VALUE:-default}\n",
    "defaults: &defaults\n  flag: true\n  retries: 3\nagent:\n  <<: *defaults\n  retries: 4\n",
    "yes_value: yes\nfalse_value: off\nnone_value: null\nhex: 0x10\noctal: 012\nfloat: 1.5\nquoted: '001'\n",
    "date: 2026-10-16\ntimestamp: 2026-10-16T07:00:00Z\nbinary: !!binary aGVsbG8=\n",
    "duplicate: first\nduplicate: last\n",
    "# 空配置\n",
    "[one, two]\n",
    "scalar\n",
])
def test_values_and_types_match_safe_load(tmp_path, text):
    config = tmp_path / "compatibility.yaml"
    config.write_text(text, encoding="utf-8")
    expected = yaml.safe_load(text)
    result = utils.load_yaml_dict(config)
    assert _typed(result) == _typed(expected if isinstance(expected, dict) else {})


@pytest.mark.parametrize("text", [
    "value: [unterminated\n",
    "value: !unknown data\n",
    "value: !!python/object/apply:builtins.str [123]\n",
    "first: 1\n---\nsecond: 2\n",
])
@pytest.mark.parametrize("without_c_extension", [False, True])
def test_invalid_documents_and_python_object_tags_remain_rejected(tmp_path, monkeypatch, text, without_c_extension):
    if without_c_extension:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    config = tmp_path / "invalid.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(yaml.YAMLError) as caught:
        utils.load_yaml_dict(config)
    assert caught.value.problem_mark.name == str(config)


def test_missing_file_still_returns_empty_mapping(tmp_path):
    assert utils.load_yaml_dict(tmp_path / "missing.yaml") == {}
