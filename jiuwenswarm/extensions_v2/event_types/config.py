# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration presentation and pre-save events; no persistence lives here."""

from collections.abc import Mapping
from typing import NotRequired, TypedDict

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset


class ConfigProvider(TypedDict):
    plugin: str
    tier: str


class ConfigDescribeInput(TypedDict):
    key: str
    label: str
    description: str
    isHidden: bool
    provider: ConfigProvider
    scope: str


class ConfigSetInput(TypedDict):
    key: str
    value: bool | str
    previous: bool | str
    provider: ConfigProvider
    origin: dict[str, str]
    scope: str


class ConfigDescribeResult(TypedDict):
    label: str
    description: NotRequired[str]
    isHidden: bool


class ConfigRow(TypedDict):
    key: str
    label: str
    description: NotRequired[str]
    kind: str
    value: bool | str
    options: NotRequired[list[str]]
    provider: ConfigProvider
    isLocked: bool


class ConfigExtensionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def valid_describe_result(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and {"label", "isHidden"} <= set(value) <= {"label", "description", "isHidden"}
        and isinstance(value["label"], str) and 0 < len(value["label"].strip()) <= 128
        and type(value["isHidden"]) is bool
        and ("description" not in value or (
            isinstance(value["description"], str) and len(value["description"]) <= 2048
        ))
    )


def valid_set_result(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) == {"deny"}:
        return isinstance(value["deny"], str) and 0 < len(value["deny"].strip()) <= 512
    return set(value) == {"value"} and type(value["value"]) in (bool, str)


def register_config_events(bus: ExtensionEventBus) -> None:
    from jiuwenswarm.extensions_v2.config_extension.dispatch import valid_describe_input, valid_set_input
    from jiuwenswarm.extensions_v2.event_handlers.config import config_describe_core, config_set_core

    bus.register_event_type("config.describe", match_pattern_subset, config_describe_core,
                            input_validator=valid_describe_input, output_validator=valid_describe_result)
    bus.register_event_type("config.set", match_pattern_subset, config_set_core,
                            input_validator=valid_set_input, output_validator=valid_set_result)
