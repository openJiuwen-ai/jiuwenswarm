# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The method overview figure, generated through a layout card that carries no numbers.

The method follows PaperBanana's layout-card path. The planner and critic prompts
and `normalize_card`'s area rule are adapted from PaperBanana's
agents/planner_agent.py, agents/critic_agent.py and utils/layout_card.py
(github.com/dwzhu-pku/PaperBanana, Apache License 2.0,
http://www.apache.org/licenses/LICENSE-2.0); the numeral rules are ours.

  plan      a chat model reads the method description and the caption and
            answers with a layout card: modules (short label, role, relative
            area, glyph), edges, zones and strings that must never be painted.
  paint     an image model paints the card literally.
  critique  a vision model compares the painting with the method and the card,
            and answers with a revised card or "no changes". It also lists every
            numeral it can see in the image.
  repeat    until the critic asks for no change, at most `rounds` paintings;
            the figure is the last painting with no visible numeral.

A generative image model paints plausible digits whether or not they are true,
so this figure shows structure and order only. The card is stripped of digits
before it is painted, the paint prompt forbids numerals, and a painting with any
visible numeral is never accepted. Numbers belong to the result figures, which
are drawn from the verified registry.

Planner, painter and critic go through `gateway.CachedGateway`: every answer
is cached by request (paintings as PNG files beside the cache), so a replay
rebuilds the record byte for byte without the network or a key.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PLANNER_SYSTEM = """Given the method description of a paper and the caption of its overview figure, design the
figure as a layout card that a renderer will paint literally.

Output a single JSON object with keys claim, flow, modules, edges, zones, forbidden.
- claim: the one sentence the figure argues.
- flow: one of LR, RL, TB, BT.
- modules: id, label (at most four words), role (contribution|process|input|output|audit),
  area (relative canvas share), glyph (a short ink-line icon name such as search, filter, judge,
  ledger, gate, document, chart).
- area is a contribution claim: the contribution-role modules must be visibly larger than the
  supporting ones. Never give every module the same area.
- edges: from, to, style (solid|dashed), optional short label.
- zones: id, tint (a pale colour name), children (module ids) for modules that belong together.
- forbidden: strings that must never be painted (figure titles, the caption, counts).

Labels are painted literally. Write only declarative labels, never conditional or meta phrasing
such as "optional" or "if space allows". Labels and the claim contain no digits and no numbers
of any kind: no counts, percentages, thresholds, years, step numbers or version numbers."""

CRITIC_SYSTEM = """You are the lead visual designer for a top AI venue. Check the attached diagram against the
method description, the figure caption and the layout card it was painted from.

Content: the diagram reflects the method without omitting or inventing components; labels are
spelled correctly and make sense; no meta or conditional phrasing ("optional", "if space allows",
"TODO") is painted; the caption text is not painted inside the image.
Numerals: list every digit or number visible anywhere in the image, including step numbers,
axis ticks, counts and version numbers. The figure must contain none.
Presentation: the flow is easy to follow, the layout is uncluttered, there is no redundant
colour legend, and the background is plain white edge to edge (no vignette, dark border, gradient or
shadow). Every label is a real word or phrase; a misspelled or truncated label is a required change.

Answer with one JSON object:
{"critic_suggestions": "the critique, or 'No changes needed.'",
 "numerals_visible": ["each numeral you can see"],
 "revised_layout_card": the revised card with the same schema and stable ids, or null when no
 changes are needed}"""

_FLOWS = {"LR": "left to right", "RL": "right to left", "TB": "top to bottom", "BT": "bottom to top"}
_DIGITS = re.compile(r"\s*\d+(?:[.,:/]\d+)*\s*")


def _no_digits(text: Any) -> str:
    return " ".join(_DIGITS.sub(" ", str(text or "")).split())


def normalize_card(card: dict, forbidden: list[str]) -> dict:
    """The card with digits removed from every paintable string, contribution modules
    the largest and areas summing to one."""
    modules = []
    for i, m in enumerate(card.get("modules") or []):
        if not isinstance(m, dict):
            continue
        label = _no_digits(m.get("label") or m.get("id"))
        if not label:
            continue
        try:
            area = max(float(m.get("area", 1.0)), 0.05)
        except (TypeError, ValueError):
            area = 1.0
        modules.append({"id": str(m.get("id") or f"m{i}"), "label": label,
                        "role": str(m.get("role") or "process"), "area": area,
                        "glyph": _no_digits(m.get("glyph")) or "panel"})
    ids = {m["id"] for m in modules}
    # Same rule as PaperBanana's ensure_contribution_area: an equal-width row argues nothing.
    if len(modules) > 1:
        contrib = [m for m in modules if m["role"] == "contribution"] or [max(modules, key=lambda m: m["area"])]
        for m in contrib:
            m["role"] = "contribution"
        others = max((m["area"] for m in modules if m not in contrib), default=0.0)
        for m in contrib:
            m["area"] = max(m["area"], others * 1.6)
    total = sum(m["area"] for m in modules) or 1.0
    for m in modules:
        m["area"] = round(m["area"] / total, 3)
    flow = str(card.get("flow") or "LR").upper()
    return {
        "claim": _no_digits(card.get("claim")),
        "flow": flow if flow in _FLOWS else "LR",
        "modules": modules,
        "edges": [{"from": str(e.get("from")), "to": str(e.get("to")),
                   "style": "dashed" if e.get("style") == "dashed" else "solid", "label": _no_digits(e.get("label"))}
                  for e in card.get("edges") or [] if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids],
        "zones": [{"id": _no_digits(z.get("id")), "tint": str(z.get("tint") or ""),
                   "children": [c for c in z.get("children") or [] if c in ids]}
                  for z in card.get("zones") or [] if isinstance(z, dict) and z.get("id")],
        "forbidden": list(dict.fromkeys([str(f) for f in card.get("forbidden") or []] + forbidden)),
    }


def card_to_prompt(card: dict) -> str:
    """The paint instruction for a card (PaperBanana's card_to_prompt, with numerals forbidden)."""
    lines = [
        "Render this scientific layout card exactly as a clean, flat, publication-quality method overview "
        "diagram. Do not invent modules, labels or a layout of your own.",
        f"Claim: {card['claim'] or 'the method pipeline'}",
        f"Flow: {_FLOWS[card['flow']]}.",
        "Box size is a contribution claim: draw each module's box in proportion to its area; contribution "
        "modules are visibly larger. Never a row of equal boxes.",
        "Each module holds a small ink-line glyph (about an eighth of its box), drawn as line art.",
        "Modules:",
    ]
    lines += [f"- {m['id']} label=\"{m['label']}\" role={m['role']} area={m['area']} glyph={m['glyph']}"
              for m in card["modules"]]
    lines.append("Edges:")
    lines += [f"- {e['from']} -> {e['to']} ({e['style']})" + (f" label=\"{e['label']}\"" if e["label"] else "")
              for e in card["edges"]] or ["- (none)"]
    if card["zones"]:
        lines.append("Zones:")
        lines += [f"- {z['id']} tint={z['tint'] or 'none'} children={', '.join(z['children'])}" for z in card["zones"]]
    if card["forbidden"]:
        lines.append("Never paint any of these strings: " + "; ".join(card["forbidden"]))
    lines.append("NUMERALS ARE FORBIDDEN: no digits or numbers anywhere in the image, no step numbers, "
                 "no counts, no percentages, no axis ticks, no version numbers.")
    lines.append("Short labels only, exactly as given. No figure title, no caption, no legend. White background.")
    return "\n".join(lines)


def _json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def has_alpha(png: bytes) -> bool:
    """Whether a PNG can be transparent, read from its header (no Pillow needed)."""
    return len(png) > 25 and (png[25] in (4, 6) or b"tRNS" in png[:4096])


def on_white(png: bytes) -> bytes:
    """The painting composited on white. Image models return transparent backgrounds,
    which a viewer shows as black. Unchanged without transparency or without Pillow."""
    if not has_alpha(png):
        return png
    try:
        import io

        from PIL import Image
    except ImportError:
        return png
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    canvas = Image.new("RGBA", img.size, "white")
    canvas.alpha_composite(img)
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def trim_whitespace(png: bytes, pad: int = 24) -> bytes:
    """The painting cropped to its content plus `pad` pixels: image models centre a wide
    diagram on a taller canvas. Unchanged when Pillow is not installed."""
    try:
        import io

        from PIL import Image, ImageChops
    except ImportError:
        return png
    img = Image.open(io.BytesIO(on_white(png))).convert("RGB")
    # Near-white counts as background: the paintings are not pure white.
    box = ImageChops.difference(img, Image.new("RGB", img.size, "white")).convert("L").point(
        lambda v: 255 if v > 8 else 0).getbbox()
    if not box:
        return png
    left, top, right, bottom = box
    img = img.crop((max(left - pad, 0), max(top - pad, 0), min(right + pad, img.width), min(bottom + pad, img.height)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def generate(method: str, caption: str, gateway, rounds: int = 3) -> dict:
    """Plan, paint and critique; the record of every round.

    A visible numeral rejects a painting outright; the critic's other remarks
    revise the card for the next painting. `accepted` is the last painting with
    no visible numeral, or None when every painting showed one (the paper then
    goes without the figure).
    """
    forbidden = [caption, "Figure"]
    user = f"Method description:\n{method}\n\nFigure caption:\n{caption}"
    card = _json_object(gateway.chat("method_figure_plan", PLANNER_SYSTEM, user))
    if not card:
        return {"accepted": None, "rounds": [], "reason": "the planner answered with no layout card"}
    card = normalize_card(card, forbidden)
    log, accepted = [], None
    for r in range(rounds):
        png = gateway.paint(f"method_figure_paint_{r}", card_to_prompt(card))
        prompt = f"{user}\n\nLayout card:\n{json.dumps(card, ensure_ascii=False)}"
        # The critic sees the painting on white; the cache key names the painting as
        # painted, so a replay without Pillow asks the same question.
        verdict = _json_object(gateway.chat(
            f"method_figure_critique_{r}", CRITIC_SYSTEM, prompt, image=on_white(png),
            cache_as={"role": "user", "content": prompt, "image_sha256": hashlib.sha256(png).hexdigest(),
                      **({"on_white": True} if has_alpha(png) else {})})) or {}
        numerals = [str(n) for n in verdict.get("numerals_visible") or []]
        revised = verdict.get("revised_layout_card")
        image = gateway.image_dir.name + "/" + hashlib.sha256(png).hexdigest()[:16] + ".png"
        log.append({"card": card, "image": image, "critique": str(verdict.get("critic_suggestions", "")),
                    "numerals_visible": numerals})
        if not numerals:
            accepted = image
            if not isinstance(revised, dict):
                break
        if isinstance(revised, dict) and revised.get("modules"):
            card = normalize_card(revised, forbidden)
    return {"accepted": accepted, "rounds": log, "chat_model": gateway.chat_model,
            "image_model": gateway.image_model, "image_size": gateway.image_size}
