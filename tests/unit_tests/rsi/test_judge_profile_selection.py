"""Explicit LLM judge selection survives API normalization and profile freezing."""

from pathlib import Path
import json
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import RsiPathInvalid, RsiUnsupportedParameter
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.services import _harness_profile_options




def test_unknown_method_is_rejected():
    with pytest.raises(RsiUnsupportedParameter, match="evaluation_method"):
        _harness_profile_options({"evaluation_method": "unknown"})
    from jiuwenswarm.agents.harness.common.rsi.materializer import _profile_options
    with pytest.raises(RsiPathInvalid, match="evaluation_method"):
        _profile_options({"evaluation_method": "unknown"})


