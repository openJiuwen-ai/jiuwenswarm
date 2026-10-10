import os

_WINDOWS_CJK_FONT_PATHS = (
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
)


def _try_register_custom_font(pdf, font_path: str) -> bool:
    try:
        pdf.add_font("custom", "", font_path, uni=True)
    except Exception:
        return False
    return True


def select_pdf_font(pdf, fallback: str = "Helvetica") -> str:
    """Return a font name registered on pdf, preferring CJK fonts when available."""
    for font_path in _WINDOWS_CJK_FONT_PATHS:
        if os.path.isfile(font_path) and _try_register_custom_font(pdf, font_path):
            return "custom"
    return fallback
