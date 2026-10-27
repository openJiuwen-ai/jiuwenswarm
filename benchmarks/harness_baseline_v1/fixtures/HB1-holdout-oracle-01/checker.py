"""Existing checks; a check is evidence only if its expectation is correct."""
from pricing import discounted

cases = [(246, 2500), (148, 5000), (64, 3333), (0, 5000), (260, 0), (260, 10000)]
for amount, rate in cases:
    expected = max(1, amount * (10000 - rate) // 10000)
    actual = discounted(amount, rate)
    assert actual == expected, (amount, rate, actual, expected)
print('checks passed')
