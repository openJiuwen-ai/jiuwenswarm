def normalize_label(value):
    return '' if value is None else value.strip().casefold()

def display_label(value):
    return '[' + value + ']'
