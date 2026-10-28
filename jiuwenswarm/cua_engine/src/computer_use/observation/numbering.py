"""Observation-local numbers, assigned after the full display projection.

Native tokens and captured facts never change. Main-tree rows get consecutive
numbers in display order; folded/uncollected objects follow them and remain
addressable through details, actions and continuation hints.
"""
from dataclasses import dataclass

from .native_facts import normalize_facts


@dataclass(frozen=True)
class _References:
    session: str
    aliases: dict

    def reference(self, native):
        return self.aliases[native]


def number_snapshots(snapshots):
    """Number all parts of one result together, without merging their scopes.

    A native object can occur in two separately bound parts (window/menu).
    Each part keeps its own alias so an action retains the exact observed
    binding. Re-normalizing facts rewrites structured references and metadata;
    replacing digits in rendered text would also corrupt ordinary UI values.
    """
    snapshots = tuple(snapshots)
    aliases = [dict() for _ in snapshots]
    count = 0
    for snapshot, mapping in zip(snapshots, aliases):
        for projection in snapshot.render(compact=True)['projections']:
            for ref in projection['refs']:
                native = snapshot.refs[ref]
                if native not in mapping:
                    mapping[native] = str(count)
                    count += 1
    for snapshot, mapping in zip(snapshots, aliases):
        for native in snapshot.refs.values():
            if native not in mapping:
                mapping[native] = str(count)
                count += 1
    return tuple(
        snapshot if snapshot.refs == {ref: native
                                      for native, ref in mapping.items()} else
        normalize_facts(snapshot.native, _References(snapshot.native['session'], mapping), driver_features=snapshot.
                        driver_features, scope_kind=snapshot.scope_kind)
        for snapshot, mapping in zip(snapshots, aliases))
