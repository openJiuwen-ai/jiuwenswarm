# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The completion's own helpers (registry, design text), offline. Loaded by path."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "scripts/complete_research_run.py"
spec = importlib.util.spec_from_file_location("complete_research_run", SCRIPT)
crr = importlib.util.module_from_spec(spec)
sys.modules["complete_research_run"] = crr
spec.loader.exec_module(crr)


def test_registry_names_the_record_file_path_and_hash(tmp_path):
    (tmp_path / "audit_results.json").write_text(json.dumps({"recall": 0.857}))
    binding = {
        "bound": [{"literal": "0.857", "value": 0.857, "file": "main.tex", "line": 12,
                   "source": "audit_results.json.recall"}],
        "unbound": [{"literal": "0.231", "file": "appendix.tex", "line": 3}],
    }
    reg = crr.registry_from_binding(binding, tmp_path)
    entry = reg["entries"][0]
    assert (entry["record_file"], entry["record_path"]) == ("audit_results.json", "recall")
    assert entry["record_sha256"] == hashlib.sha256(
        (tmp_path / "audit_results.json").read_bytes()).hexdigest()
    assert reg["unbound"] == [{"literal": "0.231", "printed_at": "appendix.tex:3"}]


def test_latex_is_reduced_to_text_for_the_checklist():
    tex = r"We compare against \textbf{two baselines} \cite{a,b} (see Sec.~\ref{s}). % note"
    assert crr.latex_to_text(tex) == "We compare against two baselines (see Sec.~)."
