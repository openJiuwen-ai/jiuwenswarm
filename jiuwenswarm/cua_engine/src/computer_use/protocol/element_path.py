from .primitives import frozen, trim, utf16_length
from .schema import role


def element_path(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("find expects a path of 1–16 name strings or { role?, name?, identifier? } segments")
    result = []
    for segment in value:
        item = {"name": segment} if isinstance(segment, str) else segment
        invalid = (not isinstance(item, dict) or not item or any(k not in ("role", "name", "identifier") for k in item)
                   or any(not isinstance(text, str) or not trim(text) or utf16_length(text) > 256
                          for text in item.values()))
        if not invalid and "role" in item:
            try:
                role.parse(item["role"])
            except ValueError:
                invalid = True
        if invalid:
            raise ValueError(('find segments use nonempty name/identifier and canonical role (for '
                              'example button or textField), up to 256 characters '
                              'each'))
        result.append(frozen(item))
    return tuple(result)
