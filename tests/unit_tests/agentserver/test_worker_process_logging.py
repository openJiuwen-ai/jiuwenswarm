# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker stderr mirroring preserves the level of standard Python log records."""

import logging
import subprocess
import sys

from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


def test_mirror_stderr_preserves_standard_log_levels(tmp_path, caplog) -> None:
    output_path = tmp_path / "worker.stderr.log"
    script = (
        "import logging\n"
        "import sys\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "logger = logging.getLogger('common')\n"
        "logger.info('Registered parser ProbeParser')\n"
        "logger.error('Worker failed')\n"
        "print('Native diagnostic', file=sys.stderr)\n"
    )
    with subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE) as child:
        with caplog.at_level(logging.DEBUG):
            WorkerProcess._drain_output(child.stderr, output_path, child.pid, "stderr")
        assert child.wait(timeout=5) == 0

    worker_records = [
        record for record in caplog.records if record.name == "jiuwenswarm.server.supervisor.worker_process"
    ]
    assert [record.levelno for record in worker_records] == [
        logging.INFO,
        logging.ERROR,
        logging.WARNING,
    ]
    assert output_path.read_bytes() == (
        b"INFO:common:Registered parser ProbeParser\n"
        b"ERROR:common:Worker failed\n"
        b"Native diagnostic\n"
    )
