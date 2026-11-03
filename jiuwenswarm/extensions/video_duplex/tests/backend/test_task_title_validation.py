import pytest


@pytest.mark.parametrize("title", [None, 42, "x" * 81, "first\nsecond"])
def test_task_service_rejects_invalid_titles(title):
    from jiuwenswarm.extensions.video_duplex.backend.tasks.service import _clean_title

    with pytest.raises(ValueError, match="title"):
        _clean_title(title)
    assert _clean_title("  修改打卡提醒  ") == "修改打卡提醒"
