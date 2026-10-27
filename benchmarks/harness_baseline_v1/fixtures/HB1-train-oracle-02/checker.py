"""Existing checks; a check is evidence only if its expectation is correct."""
from pricing import discounted

cases = [(228, 2500), (130, 5000), (46, 3333), (0, 5000), (260, 0), (260, 10000)]
for amount, rate in cases:
    expected = (amount * (10000 - rate) + 9999) // 10000
    actual = discounted(amount, rate)
    assert actual == expected, (amount, rate, actual, expected)
print('checks passed')
