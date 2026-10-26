import jiuwenswarm.symphony.evaluation as evaluation


def test_public_api_exports_unified_evaluation_contract() -> None:
    expected = {
        "AccuracyEvaluator",
        "ArtifactSpec",
        "AssertionResult",
        "ComplianceEvaluator",
        "ComplianceRule",
        "CompletenessEvaluator",
        "EvaluationAccumulator",
        "EvaluationCase",
        "EvaluationLLM",
        "EvaluationSuite",
        "Fingerprint",
        "Latency",
        "LatencyEvaluator",
        "MetricAccumulator",
        "MetricResult",
        "ParameterSpec",
        "QualityResult",
        "SuccessRateEvaluator",
        "Window",
        "write_quality_report",
        "to_json_safe",
    }

    assert expected <= set(evaluation.__all__)
    assert not hasattr(evaluation, "SkillStaticEvaluator")
    assert not hasattr(evaluation, "SkillDynamicEvaluator")
    assert not hasattr(evaluation, "EvaluationSubject")
    assert not hasattr(evaluation, "TraceEvent")
    assert not hasattr(evaluation, "FluencyEvaluator")
