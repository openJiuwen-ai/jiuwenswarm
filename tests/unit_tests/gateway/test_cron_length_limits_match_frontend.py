"""The cron name/description caps are duplicated in TypeScript, unenforced.

`CronTaskDrawer.tsx` declares its own `CRON_NAME_MAX_LENGTH` /
`CRON_DESCRIPTION_MAX_LENGTH` with a comment saying they must match
``jiuwenswarm/gateway/cron/models.py``, and nothing checked that they did. Drift
fails in the wrong direction: the drawer feeds these numbers to `maxLength` on
the inputs, so whichever value is lower becomes the cap the user actually meets,
and a backend cap raised on its own simply never takes effect.

This makes raising one and forgetting the other a test failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DESCRIPTION_MAX_LENGTH,
    CRON_JOB_NAME_MAX_LENGTH,
)

_DRAWER = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx"
)


def _declared(const_name: str) -> int:
    source = _DRAWER.read_text(encoding="utf-8")
    match = re.search(rf"^const {const_name} = (\d+);$", source, re.MULTILINE)
    assert match is not None, f"{const_name} not found in {_DRAWER}"
    return int(match.group(1))


@pytest.mark.parametrize(
    ("ts_name", "python_value"),
    [
        ("CRON_NAME_MAX_LENGTH", CRON_JOB_NAME_MAX_LENGTH),
        ("CRON_DESCRIPTION_MAX_LENGTH", CRON_JOB_DESCRIPTION_MAX_LENGTH),
    ],
)
def test_drawer_limit_matches_backend(ts_name: str, python_value: int) -> None:
    assert _declared(ts_name) == python_value, (
        f"{ts_name} in CronTaskDrawer.tsx disagrees with models.py. The drawer "
        "feeds these to maxLength, so the lower of the two is the cap the user "
        "meets and the other one never takes effect."
    )
