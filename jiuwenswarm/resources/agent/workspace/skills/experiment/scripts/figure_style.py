"""Shared publication styling for deterministic Module 3 figures.

The module deliberately contains no scientific computation. It only keeps
typography, display labels and method colours stable across figure types.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from matplotlib import font_manager


_FONT_CANDIDATES = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "Noto Sans SC",
    "Source Han Sans SC",
    "SimHei",
    "Arial",
    "Helvetica",
    "DejaVu Sans",
    "Liberation Sans",
)

_METHOD_COLORS = {
    "randomforestclassifier": "#159D8E",
    "randomforest": "#159D8E",
    "randomforestmodel": "#159D8E",
    "logisticregression": "#4C78A8",
    "decisiontreeclassifier": "#A5ACB8",
    "decisiontree": "#A5ACB8",
    "xgboost": "#7A5AA6",
    "lightgbm": "#D98B3A",
    "catboost": "#B85C70",
    "svm": "#6F86A6",
    "supportvectormachine": "#6F86A6",
}

_FALLBACK_COLORS = (
    "#159D8E",
    "#4C78A8",
    "#A5ACB8",
    "#7A5AA6",
    "#D98B3A",
    "#B85C70",
)

_DISPLAY_LABELS = {
    "accuracy": "Accuracy",
    "macro_f1": "Macro-F1",
    "micro_f1": "Micro-F1",
    "weighted_f1": "Weighted F1",
    "roc_auc": "ROC AUC",
    "pr_auc": "PR AUC",
    "train_time_seconds": "Training time (s)",
    "inference_time_seconds": "Inference time (s)",
    "total_time_seconds": "Total time (s)",
    "randomforestclassifier": "Random Forest",
    "logisticregression": "Logistic Regression",
    "decisiontreeclassifier": "Decision Tree",
}


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


@lru_cache(maxsize=1)
def publication_font_stack() -> list[str]:
    """Return an installed CJK-capable, publication-safe fallback stack."""

    _register_bundled_fonts()
    installed = {item.name for item in font_manager.fontManager.ttflist}
    selected = [name for name in _FONT_CANDIDATES if name in installed]
    if "DejaVu Sans" not in selected:
        selected.append("DejaVu Sans")
    return selected


def _register_bundled_fonts() -> None:
    """Register optional redistributable fonts shipped inside the skill."""

    font_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    if not font_dir.is_dir():
        return
    for font_path in sorted(font_dir.iterdir()):
        if font_path.suffix.casefold() not in {".ttf", ".otf", ".ttc"}:
            continue
        try:
            font_manager.fontManager.addfont(str(font_path))
        except (OSError, RuntimeError, ValueError):
            continue


def has_cjk_font() -> bool:
    return any(
        name in publication_font_stack()
        for name in (
            "Noto Sans SC",
            "Microsoft YaHei",
            "Microsoft YaHei UI",
            "Source Han Sans SC",
            "SimHei",
        )
    )


def publication_rc(*, font_size: float = 8.0) -> dict[str, object]:
    """Matplotlib rcParams shared by every module-three plot."""

    if not has_cjk_font():
        raise RuntimeError(
            "No Chinese-capable font is available. Install Microsoft YaHei/Noto Sans SC "
            "or place a redistributable CJK .ttf/.otf/.ttc file in "
            "workspace/skills/experiment/assets/fonts before rendering."
        )

    return {
        "font.family": "sans-serif",
        "font.sans-serif": publication_font_stack(),
        "font.size": font_size,
        "axes.titlesize": 10.5,
        "axes.titleweight": "bold",
        "axes.titlecolor": "#17242D",
        "axes.labelsize": 8.5,
        "axes.labelcolor": "#263238",
        "axes.edgecolor": "#87929D",
        "axes.linewidth": 0.75,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.labelsize": 7.4,
        "ytick.labelsize": 7.4,
        "xtick.color": "#46515B",
        "ytick.color": "#46515B",
        "legend.fontsize": 7.2,
        "legend.frameon": False,
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "svg.hashsalt": "jiuwenswarm-experiment-module3",
    }


def method_color(label: str, index: int = 0) -> str:
    """Give a method the same colour in every figure."""

    method_name = label.split("(", 1)[0].strip()
    return _METHOD_COLORS.get(
        _normalized(method_name),
        _FALLBACK_COLORS[index % len(_FALLBACK_COLORS)],
    )


#: 刻度标签长度上限（字符）。超过该长度的标签只保留方法主名，见 compact_label。
TICK_LABEL_LIMIT = 24


def compact_label(value: str, limit: int = TICK_LABEL_LIMIT) -> str:
    """刻度标签过长时只保留方法主名，丢掉 ``(参数=值,...)`` 后缀。

    ``BM25 Sparse (fusion=sparse Only,token Budget=2048,top K=10)`` 这类标签在
    旋转后会互相压住（导出 QA 的 pdf_audit 判定 FAIL），也会把坐标轴刻度挤成
    一团、图很不美观。参数后缀在图注/正文另有交代，刻度上留主名即可。
    """

    text = value.strip()
    if len(text) <= limit:
        return text
    method, separator, _ = text.partition(" (")
    if separator and method.strip():
        return method.strip()
    return text[: limit - 1].rstrip() + "…"


def display_label(value: str) -> str:
    """Human-readable labels without damaging canonical capitalization."""

    raw = value.strip()
    exact = _DISPLAY_LABELS.get(raw.casefold())
    if exact is not None:
        return exact

    method, separator, parameters = raw.partition(" (")
    method_exact = _DISPLAY_LABELS.get(_normalized(method))
    if method_exact is not None:
        return method_exact + ((" (" + parameters) if separator else "")

    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    raw = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", raw)
    replacements = {
        "f1": "F1",
        "auc": "AUC",
        "svm": "SVM",
        "xgboost": "XGBoost",
        "lightgbm": "LightGBM",
        "catboost": "CatBoost",
        "roc": "ROC",
        "pr": "PR",
        "hf": "HF",
    }
    return " ".join(
        replacements.get(word.casefold(), word[:1].upper() + word[1:])
        for word in raw.replace("_", " ").replace("-", " ").split()
    )


def polish_axis(axis, *, grid_axis: str = "y") -> None:
    """Apply restrained Nature-like geometry without hiding the scale."""

    axis.set_axisbelow(True)
    axis.grid(
        visible=True,
        axis=grid_axis,
        color="#E7EBEF",
        linewidth=0.7,
        alpha=0.9,
    )
    axis.spines["left"].set_color("#9AA4AE")
    axis.spines["bottom"].set_color("#9AA4AE")
    axis.tick_params(length=3.0, width=0.65)
