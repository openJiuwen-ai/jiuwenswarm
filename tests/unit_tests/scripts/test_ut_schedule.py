import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys

import pytest


@pytest.mark.parametrize('crash', [False, True])
def test_xdist_runs_an_ordered_file_group_on_one_worker(tmp_path, crash):
    pytest.importorskip('xdist')
    root = Path(__file__).resolve().parents[3]
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for filename in ('ut_schedule.py', 'ut_imports.py'):
        copyfile(root / 'scripts' / filename, scripts / filename)

    unit = tmp_path / 'tests/unit_tests'
    unit.mkdir(parents=True)
    before = 'tests/unit_tests/test_z_producer.py'
    after = 'tests/unit_tests/test_a_consumer.py'
    (scripts / 'ut_selection.json').write_text(
        json.dumps({'worker_groups': {'sequence': [before, after]}}), encoding='utf-8',
    )
    (tmp_path / before).write_text(
        'import os\nfrom pathlib import Path\n'
        'def test_produce(worker_id):\n'
        '    Path("producer").write_text(worker_id)\n'
        + ('    os._exit(79)\n' if crash else ''),
        encoding='utf-8',
    )
    (tmp_path / after).write_text(
        'from pathlib import Path\n'
        'def test_consume(worker_id):\n'
        '    Path("consumer").write_text(worker_id)\n'
        '    assert Path("producer").read_text() == worker_id\n',
        encoding='utf-8',
    )
    (unit / 'test_other.py').write_text(
        'from pathlib import Path\n'
        'def test_other(worker_id):\n'
        '    Path("other").write_text(worker_id)\n',
        encoding='utf-8',
    )
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.ut_schedule',
         '-n', '2', '--dist=loadfile', '--max-worker-restart=0',
         after, 'tests/unit_tests/test_other.py', before],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert (tmp_path / 'producer').is_file(), result.stdout + result.stderr
    if crash:
        assert result.returncode != 0
        assert 'crashed while running' in result.stdout
        assert not (tmp_path / 'consumer').exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        producer = (tmp_path / 'producer').read_text()
        assert (tmp_path / 'consumer').read_text() == producer
        assert (tmp_path / 'other').read_text() != producer
