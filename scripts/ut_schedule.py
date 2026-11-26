"""Keep one file per xdist work unit, or one ordered unit per declared file group."""

from collections import defaultdict
import json
from pathlib import Path

import pytest

from scripts.ut_imports import worker_groups


GROUPS = pytest.StashKey[dict[str, list[str]]]()
WORKER_LOST = pytest.StashKey[bool]()
RULES = Path(__file__).with_name('ut_selection.json')


def pytest_configure(config):
    try:
        config.stash[GROUPS] = worker_groups(json.loads(RULES.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise pytest.UsageError(f'Invalid UT worker groups: {exc}') from exc


def file_of(nodeid: str) -> str:
    return nodeid.split('::', 1)[0]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    groups = config.stash[GROUPS]
    owners = {path: name for name, paths in groups.items() for path in paths}
    if not owners:
        return
    by_file = defaultdict(list)
    for item in items:
        by_file[file_of(item.nodeid)].append(item)
    ordered_groups = {}
    for name, paths in groups.items():
        present = [path for path in paths if path in by_file]
        if not present:
            continue
        if len(present) != len(paths):
            missing = [path for path in paths if path not in by_file]
            raise pytest.UsageError(f'UT worker group {name!r} is incomplete. Missing: {", ".join(missing)}')
        ordered_groups[name] = [item for path in paths for item in by_file[path]]
    reordered = []
    for item in items:
        owner = owners.get(file_of(item.nodeid))
        if owner is None:
            reordered.append(item)
        else:
            reordered.extend(ordered_groups.pop(owner, []))
    items[:] = reordered


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    if error is not None:
        node.config.stash[WORKER_LOST] = True


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    groups = config.stash[GROUPS]
    if config.getoption('dist') != 'loadfile':
        if groups:
            raise pytest.UsageError('Ordered UT worker groups require --dist=loadfile')
        return None

    from xdist.scheduler import LoadFileScheduling

    owners = {path: name for name, paths in groups.items() for path in paths}

    class OrderedFileScheduling(LoadFileScheduling):
        def _split_scope(self, nodeid):
            filename = file_of(nodeid)
            return f'ordered:{owners[filename]}' if filename in owners else filename

        def _reschedule(self, node):
            # xdist can requeue a crashed worker's pending tests before it shuts down.
            if not config.stash.get(WORKER_LOST, False):
                super()._reschedule(node)

    return OrderedFileScheduling(config, log)
