# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Result figures: field style resolution, printed numbers, drawing. Offline."""

import pytest

from jiuwenswarm.agents.harness.common.research import result_figures as rf

SPEC = {
    "figure_id": "demo", "style_profile": "nature_family", "caption": "Share among 9 papers; 95\\% intervals.",
    "panels": [
        {"kind": "forest", "ref": "0.5", "rows": [{"label": "All (9)", "value": "0.78", "lo": "0.40", "hi": "0.97"}]},
        {"kind": "grouped_bar", "categories": ["earlier", "later"], "series": ["supports"],
         "values": {"supports": ["2", "5"]}},
    ],
}


def test_discipline_decides_before_venue():
    assert rf.resolve_style("ICLR", "public health")["style_profile"] == "biomed_omics"
    assert rf.resolve_style("ICLR")["style_profile"] == "stem_conference"
    assert rf.resolve_style("Nature Human Behaviour")["style_profile"] == "nature_family"
    assert rf.resolve_style("", "sociology of education")["venue_family"] == "social_behavioral"
    assert rf.resolve_style()["selection_basis"] == "default"


def test_printed_tokens_lists_every_number_the_figure_shows():
    assert rf.printed_tokens(SPEC) == ["9", "95", "0.5", "0.78", "0.40", "0.97", "2", "5"]


def test_render_writes_each_format(tmp_path):
    pytest.importorskip("matplotlib")
    assert rf.render(SPEC, tmp_path) == ["demo.pdf", "demo.png"]
    assert (tmp_path / "demo.pdf").read_bytes() == _redraw(tmp_path / "again")


def _redraw(path):
    rf.render(SPEC, path)
    return (path / "demo.pdf").read_bytes()
