"""Regression checks for the frozen CLI channel bundle.

The frozen entrypoint dispatches ``-m jiuwenswarm.channels.cli.main`` through
runpy, so PyInstaller cannot infer the CLI package from the import graph.
The spec must therefore collect the package explicitly.
"""
from pathlib import Path


def test_spec_collects_cli_channel_submodules():
    source = (Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'collect_submodules("jiuwenswarm.channels.cli")' in compact
    assert compact.count("cli_channel_submodules=") == 1
    assert "+cli_channel_submodules" in compact
