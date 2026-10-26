# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for enterprise system tests."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Generator

import pytest
from dotenv import load_dotenv

ENTERPRISE_DIR = Path(__file__).resolve().parent
if str(ENTERPRISE_DIR) not in sys.path:
    sys.path.insert(0, str(ENTERPRISE_DIR))
REPO_ROOT = ENTERPRISE_DIR.parents[2]
ENV_FILE = ENTERPRISE_DIR / ".env"
ENV_EXAMPLE_FILE = ENTERPRISE_DIR / ".env.example"
RUNS_DIR = ENTERPRISE_DIR / ".runs"


def load_enterprise_dotenv_file(*, override: bool = False) -> Path:
    """Load enterprise/.env, falling back to .env.example when .env is absent."""
    dotenv_path = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE_FILE
    load_dotenv(dotenv_path=dotenv_path, override=override)
    return dotenv_path


def _make_run_home_dir() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_home = RUNS_DIR / stamp
    run_home.mkdir(parents=True, exist_ok=False)
    (run_home / "gateway").mkdir()
    (run_home / "server").mkdir()
    return run_home


@pytest.fixture
def enterprise_run_home() -> Generator[Path, None, None]:
    """Create a timestamped run dir under enterprise/.runs/ and keep it after the test."""
    home = _make_run_home_dir()
    yield home


@pytest.fixture
def enterprise_run_dirs(enterprise_run_home: Path) -> tuple[Path, Path, Path]:
    """Return (run_home, gateway_home, server_home) for one E2E run."""
    run_home = enterprise_run_home
    return run_home, run_home / "gateway", run_home / "server"


@pytest.fixture
def enterprise_env_file() -> Path:
    return ENV_FILE


@pytest.fixture
def load_enterprise_dotenv(enterprise_env_file: Path) -> None:
    load_enterprise_dotenv_file()
