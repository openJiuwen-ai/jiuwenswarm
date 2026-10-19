from jiuwenswarm.symphony.fingerprint.normalize import DataTypeVocabulary


def test_data_type_vocab_loads_yaml_defaults() -> None:
    vocab = DataTypeVocabulary.default()

    assert vocab.version == "data-type-v1"
    assert "json" in vocab.DEFAULT_VOCAB
    assert "image" in vocab.DEFAULT_VOCAB
    assert vocab.resolve("natural_language").normalized_value == "text"
    assert vocab.resolve("application/json").normalized_value == "json"


def test_data_type_vocab_derives_hierarchy_and_traits() -> None:
    vocab = DataTypeVocabulary.default()

    assert vocab.is_subtype("png", "image")
    assert vocab.is_subtype("png", "file")
    assert vocab.is_subtype("csv", "table")
    assert "json" in vocab.CONTENT_CARRIER_TYPES
    assert "markdown" in vocab.MARKUP_TEXT_TYPES
    assert "yaml" in vocab.STRUCTURED_TEXT_TYPES


def test_data_type_vocab_type_compatibility_is_preserved() -> None:
    vocab = DataTypeVocabulary.default()

    assert vocab.can_feed_by_type("json", "text")
    assert vocab.can_feed_by_type("text", "markdown")
    assert vocab.can_feed_by_type("html", "markdown")
    assert vocab.can_feed_by_type("yaml", "json")
    assert not vocab.can_feed_by_type("unknown", "text")
    assert not vocab.can_feed_by_type("text", "json")


def test_data_type_vocab_infers_url_before_image() -> None:
    vocab = DataTypeVocabulary.default()

    inference = vocab.infer_from_io_semantics(
        "image_url",
        "URL to the generated image",
    )

    assert inference is not None
    assert inference.data_type == "url"
    assert inference.method == "semantic_score"
    assert inference.confidence >= 0.9
    assert inference.evidence["name_token"] == "image_url"
    assert "_url" in inference.evidence["matched_terms"]
    assert "image_url" in inference.evidence["matched_terms"]
    assert "url" in inference.evidence["matched_terms"]
    assert inference.evidence["runner_up"]["data_type"] == "image"


def test_data_type_vocab_infers_explicit_remote_url_terms() -> None:
    vocab = DataTypeVocabulary.default()

    for name in ("remote_url", "download_url", "file_url", "source_url"):
        inference = vocab.infer_from_io_semantics(name, "")

        assert inference is not None
        assert inference.data_type == "url"
        assert inference.confidence >= 0.9


def test_data_type_vocab_does_not_infer_broad_web_terms_as_url() -> None:
    vocab = DataTypeVocabulary.default()

    assert vocab.infer_from_io_semantics("asset", "web asset") is None
    assert vocab.infer_from_io_semantics("content", "website content") is None


def test_data_type_vocab_infers_image_from_name_and_description() -> None:
    vocab = DataTypeVocabulary.default()

    name_inference = vocab.infer_from_io_semantics("photo", "")

    assert name_inference is not None
    assert name_inference.data_type == "image"
    assert name_inference.method == "semantic_score"
    assert name_inference.confidence > 0.7
    assert name_inference.evidence["name_token"] == "photo"
    assert name_inference.evidence["matched_terms"] == ["photo"]

    inference = vocab.infer_from_io_semantics(
        "result",
        "Generated screenshot as png bytes",
    )

    assert inference is not None
    assert inference.data_type == "image"
    assert inference.method == "semantic_score"
    assert inference.confidence > 0.7
    assert inference.evidence["name_token"] == "result"
    assert inference.evidence["matched_terms"] == ["png", "screenshot"]


def test_data_type_vocab_infers_extension_like_description_terms() -> None:
    vocab = DataTypeVocabulary.default()

    inference = vocab.infer_from_io_semantics("result", "Generated png bytes")

    assert inference is not None
    assert inference.data_type == "png"
    assert inference.method == "semantic_score"
    assert inference.confidence > 0.6
    assert inference.evidence["matched_terms"] == ["png"]


def test_data_type_vocab_does_not_infer_low_confidence_terms() -> None:
    vocab = DataTypeVocabulary.default()

    assert vocab.infer_from_io_semantics("result", "generated output") is None
