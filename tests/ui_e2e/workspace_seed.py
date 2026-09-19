"""Build the HOME a UI E2E run drives the app with.

The default is a throwaway directory seeded from the real workspace, not the real
workspace itself. A run starts the product for real: it creates sessions, writes state
and, with cloud documents configured, polls the ones under management. Pointing that
at the developer's own workspace makes a test a thing that edits the machine it runs
on, and the same assumption -- that the workspace already exists and is configured --
is what let these reports depend on a session that only a previously used workspace
would have.

Seeding is what makes the temporary home usable rather than merely safe. A fresh
workspace has no model credentials, so the agent cannot answer at all; three things
are copied or set here, each because leaving it out breaks the run in a way whose
cause is far from its symptom:

* ``.env`` -- the model endpoint. Without it the app does not report a missing
  configuration: ``models.enable_free_models`` defaults to true and it quietly
  switches to whatever free model it can reach, so a suite can pass against a model
  nobody chose. That flag is turned off here for the same reason.
* ``clouddoc`` -- disabled, with connections cleared. A seeded copy would otherwise
  carry real credentials into the run and start polling documents that belong to
  people, on a timer, from a test.
* ``setup_guide`` -- disabled. A workspace with no history opens on the first-run
  overlay, which sits above the composer and swallows the clicks a report needs.

Use ``--home`` to point a run at a real workspace deliberately; nothing here prevents
that, and confirming behaviour against real state is a legitimate thing to want.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

_SEEDED_FILES = (".env", "builtin_rules.yaml")


def seed_temp_home(real_home: Path | None = None) -> Path:
    """Create a temporary HOME seeded from ``real_home`` and return it.

    The directory is not cleaned up: a failed run's workspace is evidence, and the
    report already points at it. Callers that want it gone can remove it.
    """
    real = Path(real_home) if real_home is not None else Path.home()
    tmp = Path(tempfile.mkdtemp(prefix="jiuwenswarm-e2e-"))
    cfg_dir = tmp / ".jiuwenswarm" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    real_cfg = real / ".jiuwenswarm" / "config"
    for name in _SEEDED_FILES:
        src = real_cfg / name
        if src.is_file():
            shutil.copy2(src, cfg_dir / name)

    src_yaml = real_cfg / "config.yaml"
    if src_yaml.is_file():
        _seed_config(src_yaml, cfg_dir / "config.yaml")
    return tmp


def _seed_config(src: Path, dest: Path) -> None:
    """Copy config.yaml with the three settings a test run must not inherit."""
    from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip

    data = load_yaml_round_trip(src)
    if not isinstance(data, dict):
        return

    clouddoc = data.get("clouddoc")
    if isinstance(clouddoc, dict):
        clouddoc["enabled"] = False
        clouddoc["connections"] = []

    models = data.get("models")
    if isinstance(models, dict):
        models["enable_free_models"] = False

    guide = data.get("setup_guide")
    if isinstance(guide, dict):
        guide["enabled"] = False
    else:
        data["setup_guide"] = {"enabled": False}

    dump_yaml_round_trip(dest, data)
