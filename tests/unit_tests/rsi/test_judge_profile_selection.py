"""Explicit LLM judge selection survives API normalization and profile freezing."""

from pathlib import Path
import json
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import RsiPathInvalid, RsiUnsupportedParameter
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.services import _harness_profile_options


@pytest.mark.parametrize("method", ["script-based", "exact_match", "llm_as_judge"])
def test_evaluation_method_reaches_frozen_profile(tmp_path, method):
    options = _harness_profile_options({"training_options": {"evaluation_method": method}})
    materializer = RsiTaskMaterializer(tmp_path)
    models_dir = tmp_path / "task" / "models"
    models_dir.mkdir(parents=True)
    paths = {}
    for role in ("evaluation", "analysis", "member_optimization"):
        path = models_dir / f"{role}.yaml"
        path.write_text("model_client_config: {}\n", encoding="utf-8")
        paths[role] = str(path)
    result = materializer.materialize_validation_profile("task", paths, options=options)
    config = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    assert config["evaluator"]["evaluation_method"] == method
    from openjiuwen.rsi.harness_rsi.config import EvaluatorConfig
    from openjiuwen.rsi.harness_rsi.evaluator.judger import LlmAsJudgeJudger, build_judger
    judger = build_judger(EvaluatorConfig.from_dict(config["evaluator"]))
    assert isinstance(judger, LlmAsJudgeJudger) is (method == "llm_as_judge")
    if method == "llm_as_judge":
        assert config["evaluator"]["judge_model_config_ref"] == paths["analysis"]
        assert config["evaluator"]["judge_success_score"] == 0.8
    else:
        assert "judge_model_config_ref" not in config["evaluator"]
        assert "judge_success_score" not in config["evaluator"]


def test_unknown_method_is_rejected():
    with pytest.raises(RsiUnsupportedParameter, match="evaluation_method"):
        _harness_profile_options({"evaluation_method": "unknown"})
    from jiuwenswarm.agents.harness.common.rsi.materializer import _profile_options
    with pytest.raises(RsiPathInvalid, match="evaluation_method"):
        _profile_options({"evaluation_method": "unknown"})


@pytest.mark.asyncio
@pytest.mark.parametrize("score,passed", [(0.799, False), (0.8, True)])
async def test_selected_judger_evaluates_response_with_configured_threshold(tmp_path, monkeypatch, score, passed):
    from openjiuwen.rsi.harness_rsi.config import EvaluatorConfig
    from openjiuwen.rsi.harness_rsi.evaluator.case_backend import CaseExecutionResult
    from openjiuwen.rsi.harness_rsi.evaluator.case_runner import CaseRunner
    from openjiuwen.rsi.harness_rsi.evaluator.judger import build_judger, llm_as_judge

    options = _harness_profile_options({"evaluation_method": "llm_as_judge"})
    model = tmp_path / "probe" / "models" / "model.yaml"
    model.parent.mkdir(parents=True)
    model.write_text("model_client_config: {}\n", encoding="utf-8")
    refs = {role: str(model) for role in ("evaluation", "analysis", "member_optimization")}
    profile = RsiTaskMaterializer(tmp_path).materialize_validation_profile("probe", refs, options=options)
    config = yaml.safe_load(Path(profile["path"]).read_text(encoding="utf-8"))
    response = json.dumps({"status": "completed", "overall_reason": "Reviewed response", "behaviors": [
        {"id": "criterion", "score": score, "reason": "Observed", "evidence": "response.txt"},
    ], "forbidden_hits": []})
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(llm_as_judge, "run_judge_agent", invoke)
    runner = CaseRunner(backend=object(), judger=build_judger(EvaluatorConfig.from_dict(config["evaluator"])))
    result = await runner._judge(
        case={"case_id": "probe", "input": "Reply with the result", "reference": {
            "required_behaviors": [{"id": "criterion", "description": "Result is correct"}],
        }},
        execution_result=CaseExecutionResult(response="The result", execution_status="passed"),
        output_dir=str(tmp_path / "case"),
    )
    invoke.assert_awaited_once()
    assert result.method == "llm_as_judge"
    # openjiuwen 0.1.18 exposes the thresholded verdict as ``score`` and
    # preserves the continuous model score in optimization signals.
    assert result.score == float(passed)
    assert result.passed is passed
    assert result.metadata["optimization_signals"]["continuous_score"]["value"] == score
    assert result.metadata["pass_threshold"] == 0.8
