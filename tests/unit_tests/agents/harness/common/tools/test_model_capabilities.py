from jiuwenswarm.agents.harness.common.tools.model_capabilities import (
    resolve_model_supports_image,
)


def test_explicit_modalities_true():
    assert resolve_model_supports_image(["text", "image"]) is True


def test_explicit_modalities_text_only():
    assert resolve_model_supports_image(["text"]) is False


def test_unset_modalities_defaults_false():
    assert resolve_model_supports_image(None) is False


def test_empty_modalities_false():
    assert resolve_model_supports_image([]) is False