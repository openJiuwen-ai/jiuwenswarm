def canonical_key(value):
    return '' if value is None else value.strip()

def display_label(value):
    return '[' + value + ']'
