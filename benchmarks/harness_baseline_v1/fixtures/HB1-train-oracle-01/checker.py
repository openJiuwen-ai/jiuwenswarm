"""Existing checks; a check is evidence only if its expectation is correct."""
from pricing import discounted

cases = [(210, 2500), (112, 5000), (28, 3333), (0, 5000), (260, 0), (260, 10000)]
for amount, rate in cases:
    expected = (amount * (10000 - rate) + 5000) // 10000
    actual = discounted(amount, rate)
    assert actual == expected, (amount, rate, actual, expected)
print('checks passed')
