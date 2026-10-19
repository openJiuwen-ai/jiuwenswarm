from jiuwenswarm.symphony.orchestration.language import (
    default_plan_title,
    resolve_orchestration_language,
)


def test_resolve_orchestration_language_maps_config_values() -> None:
    assert resolve_orchestration_language("zh") == "cn"
    assert resolve_orchestration_language("cn") == "cn"
    assert resolve_orchestration_language("en") == "en"


def test_resolve_orchestration_language_falls_back_to_chinese() -> None:
    assert resolve_orchestration_language("fr") == "cn"
    assert resolve_orchestration_language(None) == "cn"


def test_deterministic_copy_uses_selected_language() -> None:
    assert default_plan_title("cn") == "Symphony 编排计划"
    assert default_plan_title("en") == "Symphony plan"
