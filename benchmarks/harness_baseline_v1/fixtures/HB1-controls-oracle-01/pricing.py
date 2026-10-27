"""Public pricing API: all amounts are integer minor units."""
def discounted(amount_minor, discount_bp):
    return amount_minor * (10000 - discount_bp) // 10000
