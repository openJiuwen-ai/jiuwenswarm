

def test_a_refusal_carries_its_reason_in_the_field_the_envelope_reports():
    """A guardrail that cannot say why it refused is a guardrail nobody can obey.

    Measured on a live turn (2026-09-10): a batch edit refused for containing ``##``
    reached the model as ``success=False error=''``. It had no reason to work with, so it
    reported that it had written the document and stopped -- the write silently did not
    happen and nothing on screen said so. The sentence existed the whole time, in
    ``detail``; the envelope reports ``error``.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import _fail

    out = _fail("这段内容含 markdown 记号")
    assert out["ok"] is False
    assert out["detail"] == "这段内容含 markdown 记号"
    assert out["error"] == out["detail"], "拒绝理由必须同时出现在信封读取的字段里"

    # An explicit error still wins: a few callers distinguish the two.
    out = _fail("给人看的话", error="machine_readable")
    assert out["detail"] == "给人看的话" and out["error"] == "machine_readable"
