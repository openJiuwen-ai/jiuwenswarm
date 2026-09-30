# Research Evidence Offline

Local JiuwenSwarm/openJiuwen native plugin. Requires this competition fork and
an explicit RESEARCH_WORKSPACE environment variable (absolute research directory).
Tool research_evidence accepts {"action":"audit"}, {"action":"prepare"}, or {"action":"draft"}.
It verifies the frozen experiment and writes a new pipeline/native/<id> report.
No network, model, shell, score inference, experiment rerun, or submission operation.
The plugin alone does not disable other tools or paid chat inference in the host.
No final research findings are allowed while statistics.status != scored.


The research_evidence tool also accepts {"action":"draft"}. It assembles an English
development manuscript and evidence manifest in a new research/paper-native-<id> directory.
Missing scores stay pending; completed scores use the fixed renderer, including negative effects.
This action makes no model or compiler calls. Compile later with that directory's build.py
and --research <research-directory>, then verify before author review. It is not final approval.
