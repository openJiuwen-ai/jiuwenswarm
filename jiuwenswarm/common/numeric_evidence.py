"""Conservative quantitative membership, not a semantic evidence judgment."""
import re
from decimal import Decimal, localcontext

# Boundaries exclude model identifiers (o1/v2), and prevent 10 matching 110.
NUMBER = re.compile(r"(?<![\w.,])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?)(?!\w|[.,]\d)")
UNIT = re.compile(r"\s*(%|percent\b|percentage\b|milliseconds?\b|seconds?\b|minutes?\b|hours?\b|mg\b|kg\b|g\b|ms\b|s\b|km\b|cm\b|m\b)", re.I)
UNITS = {"%": ("ratio", "0.01"), "percent": ("ratio", "0.01"), "percentage": ("ratio", "0.01"),
         "ms": ("time", "0.001"), "millisecond": ("time", "0.001"), "milliseconds": ("time", "0.001"),
         "s": ("time", "1"), "second": ("time", "1"), "seconds": ("time", "1"),
         "minute": ("time", "60"), "minutes": ("time", "60"), "hour": ("time", "3600"), "hours": ("time", "3600"),
         "mg": ("mass", "0.001"), "g": ("mass", "1"), "kg": ("mass", "1000"),
         "cm": ("length", "0.01"), "m": ("length", "1"), "km": ("length", "1000")}


def quantities(text):
    values = set()
    for match in NUMBER.finditer(text):
        value = Decimal(match[1].replace(",", ""))
        unit = UNIT.match(text, match.end())
        dimension, scale = UNITS[unit[1].lower()] if unit else ("scalar", "1")
        with localcontext() as context:
            context.prec = max(28, len(value.as_tuple().digits) + 8)
            values.add((dimension, value * Decimal(scale)))
    return values


def numeric_block(claim, source):
    # ponytail: membership ignores value roles; entailment must check which metric owns a value.
    return not quantities(claim).issubset(quantities(source))


def self_check():
    cases = [
        ("The score was 10", "The score was 110", True),
        ("The score was 1.0", "The score was 1", False),
        ("There were 1,000 runs", "There were 1000 runs", False),
        ("The gain was 50%", "The gain was 50 percent", False),
        ("The gain was 50%", "The score was 0.5", True),
        ("It took 1000 ms", "It took 1 second", False),
        ("It weighed 1 kg", "It took 1 second", True),
        ("The score was -2", "The score was 2", True),
        ("There were 1e3 runs", "There were 1000 runs", False),
        ("o1 and Scientist-v2 are models", "No numeric evidence", False),
        ("The score was 10.5", "The score was 110.5", True),
        ("The score was 10.", "The score was 110.", True),
        ("The score was 1.00.", "The score was 1.", False),
    ]
    for claim, source, expected in cases:
        assert numeric_block(claim, source) == expected, (claim, source, expected)
    return len(cases)


if __name__ == "__main__":
    print(f"OK: {self_check()} numeric boundary/unit checks")
