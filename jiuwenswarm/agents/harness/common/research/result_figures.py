# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Result figures drawn only from recorded values, in the plotting style of the paper's field.

Two parts.

`resolve_style(target_venue, discipline)`
    Picks the plotting convention of the paper's field: a clinical paper gets
    the biomedical profile, a social-science paper the serif profile with
    coefficient plots, a computing venue the dense conference profile, a
    Nature-family journal the compact journal profile. The discipline is read
    before the venue, so a public-health study written up in the ICLR template
    is still drawn as a clinical figure. The answer names the signals it was
    decided from.

`render(spec, out_dir)`
    Draws a figure spec. A spec is plain JSON: panels, and in each panel rows
    whose values are the literals the experiment stage recorded ("0.78", not
    0.78). The renderer converts them to floats to place marks and never
    computes, rounds or prints a value of its own; `printed_tokens(spec)` lists
    every number the figure shows in text, so a caller can hold them to the
    verified registry like any number in the prose.

matplotlib is imported inside `render` only. Without it `render` returns None
and draws nothing, so a replay on a bare Python keeps the files the run drew.

Ported from the research harness visualization package (`result_plot_policy`
and the palettes, style profiles, forest and grouped-bar encodings of
`experiment_figures`), rewritten on plain matplotlib without seaborn or pandas.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, Mapping, Sequence

# Palettes. `core` is the soft Nature-style family; `clinical` the npj/Nature
# medicine categorical family; `colorblind` and `muted` are the seaborn
# palettes of the same names, written out so no seaborn is needed.
PALETTES: dict[str, list[str]] = {
    "core": ["#93cc82", "#6fb2e0", "#db6968", "#f3d78a", "#fcbc7e", "#b9c1d1"],
    "clinical": ["#BC3C29", "#0072B5", "#E18727", "#20854E", "#7876B1", "#6F99AD", "#FFDC91", "#EE4C97"],
    "colorblind": ["#0173b2", "#de8f05", "#029e73", "#d55e00", "#cc78bc", "#ca9161", "#fbafe4", "#949494"],
    "muted": ["#4878d0", "#ee854a", "#6acc64", "#d65f5f", "#956cb4", "#8c613c", "#dc7ec0", "#797979"],
}

_SANS = {"font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"]}
STYLE_PROFILES: dict[str, dict[str, Any]] = {
    # Dense, colourblind-safe conference look (CCF-A / *ACL / NeurIPS family).
    "stem_conference": {
        "rc": {**_SANS, "axes.titlesize": 9, "axes.labelsize": 8.5, "xtick.labelsize": 7.5,
               "ytick.labelsize": 7.5, "legend.fontsize": 7.5, "axes.edgecolor": "#243447",
               "grid.color": "#e6ebf1", "grid.linewidth": 0.7},
        "palette": "colorblind", "label_fmt": "({})", "label_upper": False, "grid_axis": "y",
    },
    # Compact journal look: thin ticks, no grid, bold lowercase panel letters.
    "nature_family": {
        "rc": {**_SANS, "axes.titlesize": 8.5, "axes.labelsize": 8, "xtick.labelsize": 7,
               "ytick.labelsize": 7, "legend.fontsize": 7, "axes.linewidth": 0.8,
               "axes.edgecolor": "#222222", "xtick.major.width": 0.8, "ytick.major.width": 0.8},
        "palette": "core", "label_fmt": "{}", "label_upper": False, "grid_axis": None,
    },
    # Serif look for social-science and humanities venues.
    "social_humanities": {
        "rc": {"font.family": "serif", "font.serif": ["DejaVu Serif"], "axes.titlesize": 9.5,
               "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8,
               "grid.color": "#e3e0d8", "grid.linewidth": 0.8, "axes.edgecolor": "#3b3a36"},
        "palette": "muted", "label_fmt": "({})", "label_upper": True, "grid_axis": "y",
    },
    # Biomedical look (npj / Nature medicine): uppercase bold letters, clinical palette.
    "biomed_omics": {
        "rc": {**_SANS, "axes.titlesize": 8.5, "axes.labelsize": 8, "xtick.labelsize": 7,
               "ytick.labelsize": 7, "legend.fontsize": 7, "axes.linewidth": 0.9,
               "axes.edgecolor": "#1a1a1a", "xtick.major.width": 0.9, "ytick.major.width": 0.9},
        "palette": "clinical", "label_fmt": "{}", "label_upper": True, "grid_axis": None,
    },
}

# Venue family -> (style profile, charts the family's journals use for results).
_FAMILIES = {
    "stem_computing": ("stem_conference", ("grouped_bar", "line", "heatmap", "box", "scatter")),
    "nature_multidisciplinary": ("nature_family", ("box", "violin", "scatter", "forest", "line")),
    "biomedical_clinical": ("biomed_omics", ("forest", "km_survival", "box", "line", "scatter")),
    "biomedical_omics": ("biomed_omics", ("volcano", "heatmap", "box", "forest", "scatter")),
    "social_behavioral": ("social_humanities", ("forest", "line", "box", "hist", "stacked_bar")),
}
_TERMS = (
    ("biomedical_omics", ("omics", "genomic", "genetics", "transcriptom", "proteom", "metabolom",
                          "bioinformatics", "gene expression")),
    ("biomedical_clinical", ("biomedical", "biomedicine", "medicine", "medical", "clinical", "patient",
                             "oncology", "epidemiology", "public health", "nursing", "pharmacology",
                             "jama", "lancet", "nejm", "bmj")),
    ("social_behavioral", ("social science", "sociology", "psychology", "economics", "econometric",
                           "political science", "education", "humanities", "linguistics", "law",
                           "management", "apa", "aea")),
    ("stem_computing", ("computer science", "machine learning", "artificial intelligence", "engineering",
                        "robotics", "ccf", "acm", "ieee", "neurips", "icml", "iclr", "aaai", "ijcai",
                        "acl", "emnlp", "cvpr", "iccv", "eccv", "kdd", "sigir", "nature machine intelligence")),
)
_PREFIX_TERMS = {"genomic", "transcriptom", "proteom", "metabolom", "econometric"}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def _has(text: str, term: str) -> bool:
    tail = "" if term in _PREFIX_TERMS else r"(?=$|\s)"
    return re.search(rf"(?:^|\s){re.escape(term)}{tail}", text) is not None


def _family(text: str) -> str | None:
    for family, terms in _TERMS:
        if any(_has(text, t) for t in terms):
            return family
    return "nature_multidisciplinary" if _has(text, "nature") else None


def resolve_style(target_venue: str = "", discipline: str = "") -> dict[str, Any]:
    """The field's plotting convention, from the discipline first and the venue second."""
    for basis, text in (("discipline", _norm(discipline)), ("target_venue", _norm(target_venue))):
        family = _family(text) if text else None
        if family:
            profile, charts = _FAMILIES[family]
            return {"venue_family": family, "style_profile": profile, "selection_basis": basis,
                    "signal": text, "recommended_charts": list(charts)}
    profile, charts = _FAMILIES["stem_computing"]
    return {"venue_family": "stem_computing", "style_profile": profile, "selection_basis": "default",
            "signal": "", "recommended_charts": list(charts)}


_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def printed_tokens(spec: Mapping[str, Any]) -> list[str]:
    """Every number the figure shows as text or places as a mark, in first-seen order.

    Axis tick values are scale, not results, and are not listed.
    """
    texts = [spec.get("caption", "")]
    for panel in spec["panels"]:
        texts += [panel.get("title", ""), panel.get("xlabel", ""), panel.get("ylabel", ""),
                  str(panel.get("ref", ""))]
        texts += [str(c) for c in panel.get("categories", [])] + [str(s) for s in panel.get("series", [])]
        for row in panel.get("rows", []):
            texts += [str(row.get("label", ""))]
            texts += [str(row[k]) for k in ("value", "lo", "hi") if row.get(k) is not None]
        for values in panel.get("values", {}).values():
            texts += [str(v) for v in values]
    return list(dict.fromkeys(t for text in texts for t in _NUMBER.findall(text)))


def _despine(ax, grid_axis: str | None) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(visible=False)
    if grid_axis:
        ax.grid(axis=grid_axis, alpha=0.75)


def _draw_forest(ax, panel: Mapping[str, Any], colors: list[str], profile: Mapping[str, Any]) -> None:
    """Estimates with interval whiskers, one row each, top row first; a dashed line at the null."""
    rows = panel["rows"]
    for i, row in enumerate(rows):
        y = len(rows) - 1 - i
        value, lo, hi = float(row["value"]), float(row["lo"]), float(row["hi"])
        color = colors[i % len(colors)]
        ax.errorbar(value, y, xerr=[[value - lo], [hi - value]], fmt="o", color=color, ecolor=color,
                    elinewidth=1.4, capsize=2.5, markersize=5, markeredgewidth=1.2,
                    markerfacecolor=color if row.get("filled", True) else "white")
    if panel.get("ref") is not None:
        ax.axvline(float(panel["ref"]), color="#8a8a8a", linewidth=0.9, linestyle="--")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r["label"] for r in reversed(rows)])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlim(*panel.get("xlim", (0.0, 1.0)))
    _despine(ax, "x" if profile["grid_axis"] else None)


def _draw_grouped_bar(ax, panel: Mapping[str, Any], colors: list[str], profile: Mapping[str, Any]) -> None:
    """One cluster per category, one bar per series; values are counts the experiment recorded."""
    categories, series = panel["categories"], panel["series"]
    width = 0.8 / max(len(series), 1)
    for j, name in enumerate(series):
        xs = [i + (j - (len(series) - 1) / 2) * width for i in range(len(categories))]
        ax.bar(xs, [float(v) for v in panel["values"][name]], width=width * 0.92,
               color=colors[j % len(colors)], edgecolor="white", linewidth=0.5, label=name)
    ax.set_xticks(range(len(categories)))
    # Wrapped, so neighbouring labels never run together in the larger serif profiles.
    ax.set_xticklabels([textwrap.fill(str(c), 10) for c in categories])
    ax.yaxis.get_major_locator().set_params(integer=True)
    # Outside the axes: inside, the legend covers the tallest bars of a short panel.
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    _despine(ax, profile["grid_axis"])


_DRAW = {"forest": _draw_forest, "grouped_bar": _draw_grouped_bar}


def render(spec: Mapping[str, Any], out_dir: Path, formats: Sequence[str] = ("pdf", "png")) -> list[str] | None:
    """Draw the spec to out_dir/<figure_id>.<fmt>; the file names written, or None without matplotlib."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    profile = STYLE_PROFILES.get(spec.get("style_profile", ""), STYLE_PROFILES["stem_conference"])
    colors = PALETTES[profile["palette"]]
    panels = spec["panels"]
    with plt.rc_context({**profile["rc"], "pdf.fonttype": 42, "svg.hashsalt": "result-figure"}):
        fig, axes = plt.subplots(1, len(panels), figsize=tuple(spec.get("size", (5.5, 2.2))),
                                 gridspec_kw={"width_ratios": [p.get("width", 1) for p in panels]})
        axes = [axes] if len(panels) == 1 else list(axes)
        for i, (ax, panel) in enumerate(zip(axes, panels)):
            _DRAW[panel["kind"]](ax, panel, colors, profile)
            ax.set_xlabel(panel.get("xlabel", ""))
            ax.set_ylabel(panel.get("ylabel", ""))
            if panel.get("title"):
                ax.set_title(panel["title"], loc="left")
            letter = chr(ord("a") + i)
            ax.text(-0.02, 1.02, profile["label_fmt"].format(letter.upper() if profile["label_upper"] else letter),
                    transform=ax.transAxes, ha="right", va="bottom", fontweight="bold",
                    fontsize=profile["rc"]["axes.titlesize"] + 1)
        fig.tight_layout()
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for fmt in formats:
            name = f"{spec['figure_id']}.{fmt}"
            # No timestamps or tool versions in the files, so a redraw from the same
            # spec with the same matplotlib writes the same bytes.
            metadata = {"CreationDate": None, "Producer": None} if fmt == "pdf" else {"Software": None}
            fig.savefig(out_dir / name, dpi=320, bbox_inches="tight", facecolor="white", metadata=metadata)
            written.append(name)
        plt.close(fig)
    return written

