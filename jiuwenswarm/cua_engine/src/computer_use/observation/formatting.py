"""String/number formatting at the existing model-observation boundary."""
import json
import math
import re
from decimal import Decimal


def json_string(value):
    if re.search(r"[\ud800-\udfff]", value):
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return re.sub(r"[\ud800-\udfff]", lambda m: f"\\u{ord(m[0]):04x}", encoded)


def number_string(value):
    if value == 0:
        return "0"
    if not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, int):
        value = float(value)
    text = repr(value)
    if 1e-6 <= abs(value) < 1e21:
        return format(Decimal(text), "f").rstrip("0").rstrip(".") if "." in format(Decimal(text), "f") else format(
            Decimal(text), "f")
    mantissa, exponent = text.lower().split("e") if "e" in text.lower() else (text, "0")
    mantissa = mantissa.removesuffix(".0")
    number = int(exponent)
    return mantissa + "e" + ("+" if number >= 0 else "-") + str(abs(number))
