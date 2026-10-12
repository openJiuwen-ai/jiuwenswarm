# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Method figure: numbers never reach the card, a painting with a numeral is never used, replay is offline."""

import base64
import json

import pytest

from jiuwenswarm.agents.harness.common.research import gateway as gw
from jiuwenswarm.agents.harness.common.research import method_figure as mf

CARD = {"claim": "Screen 40 papers", "flow": "LR",
        "modules": [{"id": "a", "label": "Search 5 indexes", "role": "input", "area": 1},
                    {"id": "b", "label": "Stance synthesis", "role": "contribution", "area": 1}],
        "edges": [{"from": "a", "to": "b", "label": "top 10"}, {"from": "a", "to": "missing"}]}


def test_card_carries_no_digits_and_the_contribution_is_largest():
    card = mf.normalize_card(CARD, ["Figure"])
    assert card["claim"] == "Screen papers"
    assert [m["label"] for m in card["modules"]] == ["Search indexes", "Stance synthesis"]
    assert card["modules"][1]["area"] > card["modules"][0]["area"]
    assert card["edges"] == [{"from": "a", "to": "b", "style": "solid", "label": "top"}]
    assert "NUMERALS ARE FORBIDDEN" in mf.card_to_prompt(card)


def _gateway(tmp_path, monkeypatch, critiques):
    pngs = iter([b"png-0", b"png-1", b"png-2"])
    answers = iter([json.dumps(CARD)] + [json.dumps(c) for c in critiques])

    def post(url, key, body, timeout=600):
        if url.endswith("/images/generations"):
            return {"data": [{"b64_json": base64.b64encode(next(pngs)).decode()}], "usage": {"total_tokens": 7}}
        return {"choices": [{"message": {"content": next(answers)}}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(gw, "_post", post)
    rows = []
    return gw.CachedGateway(tmp_path / "figure_cache.jsonl", replay=False, on_usage=rows.append), rows


def test_a_painting_with_a_numeral_is_revised_and_never_accepted(tmp_path, monkeypatch):
    gateway, rows = _gateway(tmp_path, monkeypatch, [
        {"critic_suggestions": "step numbers painted", "numerals_visible": ["1", "2"],
         "revised_layout_card": CARD},
        {"critic_suggestions": "No changes needed.", "numerals_visible": [], "revised_layout_card": None},
    ])
    out = mf.generate("method", "caption", gateway)
    assert [r["numerals_visible"] for r in out["rounds"]] == [["1", "2"], []]
    assert out["accepted"] == out["rounds"][1]["image"]
    assert (tmp_path / out["accepted"]).read_bytes() == b"png-1"
    assert sum(r["total_tokens"] for r in rows) == 3 + 2 * (7 + 3)

    replay = gw.CachedGateway(tmp_path / "figure_cache.jsonl", replay=True)
    monkeypatch.setattr(gw, "_post", None)   # any network call would fail
    assert mf.generate("method", "caption", replay) == out
    with pytest.raises(SystemExit):
        mf.generate("another method", "caption", replay)


def test_no_figure_when_every_painting_shows_a_numeral(tmp_path, monkeypatch):
    numeral = {"critic_suggestions": "", "numerals_visible": ["3"], "revised_layout_card": None}
    gateway, _ = _gateway(tmp_path, monkeypatch, [numeral] * 3)
    assert mf.generate("method", "caption", gateway)["accepted"] is None


def test_a_transparent_painting_is_shown_on_white():
    pytest.importorskip("PIL")
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (40, 20), (0, 0, 0, 0)).save(buf, format="PNG")
    clear = buf.getvalue()
    assert mf.has_alpha(clear)
    flat = Image.open(io.BytesIO(mf.on_white(clear)))
    assert flat.mode == "RGB" and flat.getpixel((5, 5)) == (255, 255, 255)
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    assert not mf.has_alpha(buf.getvalue()) and mf.on_white(buf.getvalue()) == buf.getvalue()
