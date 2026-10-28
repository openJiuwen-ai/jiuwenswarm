from .._identity import WeakIdentityMap
from ..protocol.primitives import frozen

_views = WeakIdentityMap()


# Explicit native/observation contract fields stay aligned with their caller.
# pylint: disable-next=huawei-too-many-arguments
def retain_observation_data(state, roots, refs, windows=(), instance=None, identity=None):
    # JS Set iteration preserves the observed order (notably window enums).
    data = dict(roots=roots, refs=frozen(dict.fromkeys(refs)).keys(), windows=frozen(dict.fromkeys(windows)).keys())
    if instance:
        data["instance"] = instance
    if identity is not None:
        data["identity"] = frozen({**identity, "windows": tuple(frozen(w) for w in identity["windows"])})
    _views.set(state, data)


def observation_data(state):
    return _views.get(state)
