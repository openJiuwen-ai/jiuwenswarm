# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.extensions.manager import _extension_dir_paths_from_config


def test_extension_dirs_default_to_builtin_when_empty_string() -> None:
    paths = _extension_dir_paths_from_config(
        {"extensions": {"extension_dirs": ""}}
    )

    assert paths == ["jiuwenswarm/extensions"]


def test_extension_dirs_default_to_builtin_when_missing() -> None:
    assert _extension_dir_paths_from_config({}) == ["jiuwenswarm/extensions"]
    assert _extension_dir_paths_from_config({"extensions": {}}) == [
        "jiuwenswarm/extensions"
    ]


def test_extension_dirs_append_builtin_after_custom_paths() -> None:
    paths = _extension_dir_paths_from_config(
        {"extensions": {"extension_dirs": "custom/a; custom/b "}}
    )

    assert paths == ["custom/a", "custom/b", "jiuwenswarm/extensions"]


def test_extension_dirs_do_not_duplicate_builtin_path() -> None:
    paths = _extension_dir_paths_from_config(
        {
            "extensions": {
                "extension_dirs": "custom/a;jiuwenswarm/extensions;custom/a"
            }
        }
    )

    assert paths == ["custom/a", "jiuwenswarm/extensions"]
