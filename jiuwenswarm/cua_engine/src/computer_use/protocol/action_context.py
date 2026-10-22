from .primitives import frozen
from .schema import identity, obj, ref

_schema = obj(dict(after=identity, scope=identity, window=ref.nullable()))


def decode_action_context(value):
    return frozen(_schema.parse(value))
