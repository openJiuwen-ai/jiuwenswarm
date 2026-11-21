# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the pure version-parsing helpers in version_source."""

from datetime import datetime, timezone

import pytest

from jiuwenswarm.common.version_source import (
    is_prerelease_version,
    release_sort_key,
    release_timestamp_key,
    strip_prerelease_suffix,
)


class TestIsPrereleaseVersion:
    @pytest.mark.parametrize(
        "version",
        [
            "0.2.0-beta1",
            "0.2.0.beta.1",
            "0.2.0alpha",
            "0.2.0rc2",
            "0.2.0.dev0",
            "v0.2.1b1",
            "0.3.0pre",
        ],
    )
    def test_prerelease_markers_are_detected(self, version: str):
        assert is_prerelease_version(version) is True

    @pytest.mark.parametrize(
        "version",
        ["0.2.0", "0.2.10", "1.0.0", "v0.2.3", "", None, "2026-10-27"],
    )
    def test_stable_versions_are_not_prereleases(self, version):
        assert is_prerelease_version(version) is False


class TestStripPrereleaseSuffix:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.2.0.beta1", "0.2.0"),
            ("0.2.3-rc.2", "0.2.3"),
            ("v0.2.3", "0.2.3"),
            ("0.2.0", "0.2.0"),
            ("1.2.3alpha4", "1.2.3"),
            ("  0.2.1.dev0  ", "0.2.1"),
        ],
    )
    def test_suffix_is_removed(self, version: str, expected: str):
        assert strip_prerelease_suffix(version) == expected

    def test_non_version_text_is_returned_stripped(self):
        assert strip_prerelease_suffix("latest") == "latest"

    def test_empty_input(self):
        assert strip_prerelease_suffix("") == ""


class TestReleaseSortKey:
    def test_numeric_base_comparison(self):
        assert release_sort_key("0.2.10") > release_sort_key("0.2.9")

    def test_stable_outranks_prerelease_at_same_base(self):
        assert release_sort_key("0.2.3") > release_sort_key("0.2.3.beta1")

    def test_prerelease_type_ordering(self):
        keys = {
            marker: release_sort_key(f"0.2.3.{marker}1")
            for marker in ("dev", "alpha", "beta", "rc", "pre")
        }
        assert keys["dev"] < keys["alpha"] < keys["beta"] < keys["rc"] < keys["pre"]

    def test_prerelease_number_orders_within_type(self):
        assert release_sort_key("0.2.3.beta2") > release_sort_key("0.2.3.beta1")

    def test_base_dominates_prerelease_type(self):
        # 0.2.4 dev is still newer than 0.2.3 pre.
        assert release_sort_key("0.2.4.dev1") > release_sort_key("0.2.3.pre1")

    def test_v_prefix_is_ignored(self):
        assert release_sort_key("v0.2.3") == release_sort_key("0.2.3")

    def test_max_picks_newest_release(self):
        versions = ["0.2.9", "0.2.10", "0.2.10.beta1", "0.2.11-rc.1"]
        assert max(versions, key=release_sort_key) == "0.2.11-rc.1"
        assert max(["0.2.10", "0.2.10.beta1"], key=release_sort_key) == "0.2.10"


class TestReleaseTimestampKey:
    def test_zulu_normalized_to_utc(self):
        assert release_timestamp_key("2026-10-26T09:35:41Z") == datetime(
            2026, 10, 26, 9, 35, 41, tzinfo=timezone.utc
        )

    def test_offset_normalized_to_utc(self):
        assert release_timestamp_key("2026-10-26T17:35:41+08:00") == (
            datetime(2026, 10, 26, 9, 35, 41, tzinfo=timezone.utc)
        )

    @pytest.mark.parametrize(
        "value", ["", "   ", "not-a-timestamp", "2026-10-26T09:35:41"]
    )
    def test_invalid_or_naive_timestamps_raise(self, value: str):
        with pytest.raises(ValueError):
            release_timestamp_key(value)
