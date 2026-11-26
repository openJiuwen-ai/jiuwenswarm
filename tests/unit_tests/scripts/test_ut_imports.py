import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys

import pytest

from scripts.ut_imports import import_test_hits


def test_import_selection_follows_at_most_two_edges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {
        'jiuwenswarm/example/changed.py': '',
        'jiuwenswarm/example/wrapper.py': 'from . import changed',
        'jiuwenswarm/bridge.py': 'from .example import wrapper',
        'tests/support.py': 'import jiuwenswarm.example.changed as changed',
        'tests/unit_tests/test_direct.py': 'from jiuwenswarm.example import changed',
        'tests/unit_tests/test_via_source.py': 'import jiuwenswarm.example.wrapper',
        'tests/unit_tests/test_via_helper.py': 'from .. import support',
        'tests/unit_tests/test_too_far.py': 'from jiuwenswarm import bridge',
        'tests/unit_tests/test_unrelated.py': 'import json',
        'tests/system_tests/conftest.py': '    unrelated invalid syntax',
    }
    for name, body in files.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')

    source = 'jiuwenswarm/example/changed.py'
    direct, chained = import_test_hits([source], list(files))[source]

    assert direct == {'tests/unit_tests/test_direct.py'}
    assert chained == {'tests/unit_tests/test_via_source.py', 'tests/unit_tests/test_via_helper.py'}


@pytest.mark.parametrize(
    ('changes', 'expected'),
    [
        (('jiuwenswarm/leaf.py',), 'impacted'),
        (('jiuwenswarm/leaf.py',), 'ordered'),
        (('jiuwenswarm/mirrored.py',), 'mirrored'),
        (('jiuwenswarm/leaf.py', 'jiuwenswarm/uncovered.py'), 'full'),
        (('jiuwenswarm/common/config.py',), 'full'),
        (('tests/unit_tests/conftest.py',), 'full'),
        (('.gitcode/ISSUE_TEMPLATE.zh/001-bug-report.yml',), 'baseline'),
        (('jiuwenswarm/leaf.py',), 'diverged'),
    ],
)
def test_selector_requires_evidence_for_each_unmapped_source(tmp_path, changes, expected):
    repo_root = Path(__file__).resolve().parents[3]
    baseline = 'tests/unit_tests/test_base.py'
    impacted = 'tests/unit_tests/test_leaf_consumer.py'
    mirrored = 'tests/unit_tests/test_mirrored.py'
    peer = 'tests/unit_tests/test_leaf_setup.py'
    initial_files = {
        baseline: '',
        impacted: 'from jiuwenswarm import leaf',
        mirrored: '',
        peer: '',
        'tests/unit_tests/test_shared.py': 'from jiuwenswarm.common import config',
        'jiuwenswarm/leaf.py': '',
        'jiuwenswarm/mirrored.py': '',
        'jiuwenswarm/uncovered.py': '',
        'jiuwenswarm/common/config.py': '',
    }
    for name, content in initial_files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ('select_ut.py', 'ut_imports.py'):
        copyfile(repo_root / 'scripts' / name, scripts / name)
    (scripts / 'ut_selection.json').write_text(
        json.dumps({
            'baseline': [baseline],
            'features': {},
            'full_run': ['jiuwenswarm/common/config.py'],
            'worker_groups': {'leaf': [peer, impacted]} if expected == 'ordered' else {},
        }),
        encoding='utf-8',
    )

    def git(*args):
        return subprocess.check_output(
            ['git', '-c', 'user.name=UT', '-c', 'user.email=ut@example.invalid', *args],
            cwd=tmp_path, text=True,
        ).strip()

    git('init', '-q', '--template=')
    git('add', '.')
    git('commit', '--no-gpg-sign', '-qm', 'base')
    for name in changes:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# changed', encoding='utf-8')
    git('add', '.')
    git('commit', '--no-gpg-sign', '-qm', 'change')

    base_ref = 'HEAD^'
    if expected == 'diverged':
        base_ref = git('commit-tree', '--no-gpg-sign', 'HEAD^{tree}', '-p', 'HEAD^', '-m', 'comparison')

    result = subprocess.run(
        [sys.executable, 'scripts/select_ut.py', base_ref], cwd=tmp_path,
        check=True, capture_output=True, text=True,
    )
    expected_paths = {
        'impacted': {baseline, impacted},
        'ordered': {baseline, peer, impacted},
        'mirrored': {baseline, mirrored},
        'baseline': {baseline},
        'full': {'tests/unit_tests'},
        'diverged': {'tests/unit_tests'},
    }
    assert set(result.stdout.splitlines()) == expected_paths[expected]
    assert ('UT full suite:' in result.stderr) == (expected in {'full', 'diverged'})
    if expected == 'diverged':
        assert 'UT_BASE_SHA is not an ancestor of HEAD' in result.stderr
