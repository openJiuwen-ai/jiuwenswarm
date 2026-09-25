from jiuwenswarm.common.e2a.acp.prompt_blocks import (
    parse_prompt_blocks,
    resource_uri_to_path,
)


def test_parse_text_blocks_joined_with_newline():
    parsed = parse_prompt_blocks(
        {"prompt": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}
    )

    assert parsed.text == "one\ntwo"
    assert parsed.attachments == []
    assert parsed.media_items == []
    assert parsed.has_content


def test_parse_resource_block_renders_inline_context():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                {"type": "text", "text": "explain"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///workspace/a.py#L1-L2",
                        "text": "def a(): pass",
                        "mimeType": "text/x-python",
                    },
                },
            ]
        }
    )

    assert parsed.text == (
        'explain\n<context uri="file:///workspace/a.py#L1-L2">\ndef a(): pass\n</context>'
    )


def test_parse_resource_link_file_uri_becomes_attachment():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                {"type": "text", "text": "see file"},
                {"type": "resource_link", "uri": "file:///workspace/b.py", "name": "b.py"},
            ]
        }
    )

    assert parsed.attachments == [
        {"path": "/workspace/b.py", "type": "file", "filename": "b.py"}
    ]
    assert parsed.text == "see file"


def test_parse_resource_link_http_uri_becomes_text_reference():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                {"type": "text", "text": "see doc"},
                {"type": "resource_link", "uri": "https://example.com/doc", "name": "doc"},
            ]
        }
    )

    assert parsed.attachments == []
    assert parsed.text == "see doc\n[Linked resource] doc: https://example.com/doc"


def test_parse_image_block_becomes_media_item():
    parsed = parse_prompt_blocks(
        {"prompt": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]}
    )

    assert parsed.media_items == [
        {"type": "image", "base64Data": "aGVsbG8=", "mimeType": "image/png"}
    ]
    assert parsed.text == "(see attached context)"


def test_parse_blob_resource_with_image_mime_becomes_media_item():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///workspace/shot.png",
                        "blob": "aGVsbG8=",
                        "mimeType": "image/png",
                    },
                }
            ]
        }
    )

    assert parsed.media_items == [
        {
            "type": "image",
            "base64Data": "aGVsbG8=",
            "mimeType": "image/png",
            "filename": "shot.png",
        }
    ]


def test_parse_blob_resource_with_non_image_mime_is_ignored():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                {"type": "text", "text": "hello"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///workspace/data.bin",
                        "blob": "aGVsbG8=",
                        "mimeType": "application/octet-stream",
                    },
                },
            ]
        }
    )

    assert parsed.text == "hello"
    assert parsed.media_items == []


def test_parse_malformed_and_unknown_blocks_are_ignored():
    parsed = parse_prompt_blocks(
        {
            "prompt": [
                "not a dict",
                {"type": "audio", "data": "aGVsbG8="},
                {"type": "image"},
                {"type": "resource"},
                {"type": "resource_link"},
                {"type": "text", "text": "kept"},
            ]
        }
    )

    assert parsed.text == "kept"
    assert parsed.attachments == []
    assert parsed.media_items == []


def test_parse_falls_back_to_params_keys_when_prompt_is_not_a_list():
    parsed = parse_prompt_blocks({"text": "  hello  "})

    assert parsed.text == "hello"

    parsed = parse_prompt_blocks(
        {"content": "from content"}, fallback_keys=("content", "query", "text")
    )
    assert parsed.text == "from content"


def test_parse_empty_prompt_has_no_content():
    parsed = parse_prompt_blocks({"prompt": []})

    assert parsed.text == ""
    assert not parsed.has_content


def test_resource_uri_to_path_variants():
    assert resource_uri_to_path("file:///workspace/a.py") == "/workspace/a.py"
    assert resource_uri_to_path("file:///C:/proj/a.py") == "C:/proj/a.py"
    assert resource_uri_to_path("file://server/share/a.py") == "//server/share/a.py"
    assert resource_uri_to_path("/abs/path.py") == "/abs/path.py"
    assert resource_uri_to_path("C:\\proj\\a.py") == "C:\\proj\\a.py"
    assert resource_uri_to_path("https://example.com/a.py") is None
    assert resource_uri_to_path("zed://buffer/1") is None
