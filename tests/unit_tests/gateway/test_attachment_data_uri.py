# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for stripping data-URI prefixes from uploaded base64 payloads."""

import pytest

from gateway.common.attachment_safety import strip_data_uri_prefix


class TestStripDataUriPrefix:
    def test_pdf_data_uri_is_stripped(self):
        assert (
            strip_data_uri_prefix("data:application/pdf;base64,JVBERi0xLjQK")
            == "JVBERi0xLjQK"
        )

    def test_image_data_uri_is_stripped(self):
        assert strip_data_uri_prefix("data:image/png;base64,iVBORw0KGgo=") == "iVBORw0KGgo="

    def test_prefix_without_base64_marker_is_stripped(self):
        assert strip_data_uri_prefix("data:text/plain,hello") == "hello"

    def test_commas_inside_payload_are_preserved(self):
        assert strip_data_uri_prefix("data:text/csv;base64,a,b,c") == "a,b,c"

    @pytest.mark.parametrize("payload", ["JVBERi0xLjQK", "iVBORw0KGgo=", "plain-text"])
    def test_plain_base64_is_untouched(self, payload: str):
        assert strip_data_uri_prefix(payload) == payload

    def test_empty_string(self):
        assert strip_data_uri_prefix("") == ""
