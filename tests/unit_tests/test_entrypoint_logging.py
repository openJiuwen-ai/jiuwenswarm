# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify entrypoint log routing without starting services or touching user data."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("entrypoint", ["server/app_agentserver.py", "gateway/app_gateway.py"])
@pytest.mark.parametrize("layout", ["standalone", "desktop", "desktop_outer_date"])
@pytest.mark.parametrize("explicit_config", [False, True])
def test_agent_core_warning_and_error_files(tmp_path, entrypoint, layout, explicit_config):
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for key in (
        "JIUWENSWARM_LOG_DIR", "JIUWENSWARM_CORE_LOG_DIR",
        "JIUWENSWARM_LOG_DATE_ROOT", "LOG_LEVEL",
    ):
        env.pop(key, None)
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path)
    if layout != "standalone":
        logs_dir = tmp_path / "swarm"
        if layout == "desktop_outer_date":
            env["JIUWENSWARM_LOG_DATE_ROOT"] = str(tmp_path / "logs")
            logs_dir = tmp_path / "logs" / "jiuwenswarm" / "test-user"
        env["JIUWENSWARM_LOG_DIR"] = str(logs_dir)
        env["JIUWENSWARM_CORE_LOG_DIR"] = str(logs_dir / "core")

    # Run the actual logging branch in a child: importing the whole entrypoint
    # also initializes workspaces, installs skills, and imports service runtimes.
    script = textwrap.dedent(r"""
        import ast
        import logging
        import os
        from pathlib import Path
        import sys

        repo, root = map(Path, sys.argv[1:3])
        sys.path.insert(0, str(repo))
        config = root / 'config' / 'logging.yaml'
        config.parent.mkdir()
        explicit = sys.argv[4] == 'True'
        if explicit:
            config.write_text(
                'logging:\n  level: ERROR\n  output: [file]\n  log_path: '
                + (root / 'custom-core').as_posix() + '\n', encoding='utf-8')

        from jiuwenswarm.common import utils
        from openjiuwen.core.common.logging import LogManager

        utils.configure_agent_core_log_dir()
        LogManager.get_logger('interface')  # Registered before the fallback.
        path = repo / 'jiuwenswarm' / sys.argv[3]
        tree = ast.parse(path.read_text(encoding='utf-8'))
        branch = next(node for node in tree.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == '_logging_yaml.exists()')
        exec(compile(ast.Module(body=[branch], type_ignores=[]), str(path), 'exec'),
             {'_logging_yaml': config, 'logging': logging, 'LogManager': LogManager})
        utils.configure_agent_core_log_dir()

        names = ('common', 'interface', 'prompt_builder', 'performance')
        for name in names:
            logger = LogManager.get_logger(name)
            logger.warning('regression-warning-' + name)
            logger.error('regression-error-' + name)
        if not explicit and sys.argv[3].startswith('server/'):
            LogManager.get_logger('common').info('[PermissionEngine] regression-permission')
        logging.shutdown()

        files = list(root.rglob('*.log'))
        core_dir = ('core' if os.environ.get('JIUWENSWARM_CORE_LOG_DIR')
                    else ('custom-core' if explicit else 'logs'))
        core_files = [p for p in files if core_dir in p.relative_to(root).parts]
        content = '\n'.join(p.read_text(encoding='utf-8') for p in core_files)
        for name in names:
            assert ('regression-warning-' + name in content) == (not explicit), name
            assert 'regression-error-' + name in content, name
        if not explicit and sys.argv[3].startswith('server/'):
            assert any('regression-permission' in p.read_text(encoding='utf-8')
                       for p in files if p.name == 'permissions.log')
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(tmp_path), entrypoint, str(explicit_config)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
