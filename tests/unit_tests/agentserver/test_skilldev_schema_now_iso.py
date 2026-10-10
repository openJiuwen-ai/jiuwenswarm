"""SkillDev ``_now_iso`` must not use deprecated ``datetime.utcnow()``."""

from __future__ import annotations

import re
import warnings
from datetime import datetime, timezone

from jiuwenswarm.server.runtime.skill.skilldev.schema import _now_iso

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_now_iso_format_and_no_deprecation_warning() -> None:
    before = datetime.now(timezone.utc).replace(microsecond=0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        stamp = _now_iso()
    after = datetime.now(timezone.utc).replace(microsecond=0)

    assert _ISO_Z.match(stamp), stamp
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)

    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= parsed <= after
