# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import infer_aspect_lock


def test_infer_aspect_lock_does_not_force_480_when_user_is_silent():
    lock = infer_aspect_lock("a short film about a baker")
    assert lock["ratio"] == "16:9"
    assert lock["video_resolution"] == ""


def test_infer_aspect_lock_honors_portrait_and_1080p():
    lock = infer_aspect_lock("请做竖屏 1080p 短视频")
    assert lock["ratio"] == "9:16"
    assert lock["video_size"] == "1080*1920"
    assert lock["video_resolution"] == "1080P"


def test_infer_aspect_lock_honors_square_720p():
    lock = infer_aspect_lock("1:1 square frame 720p")
    assert lock["ratio"] == "1:1"
    assert lock["video_size"] == "960*960"
    assert lock["video_resolution"] == "720P"


def test_infer_aspect_lock_honors_ultrawide():
    lock = infer_aspect_lock("cinemascope 21:9 1080p")
    assert lock["ratio"] == "21:9"
    assert lock["video_size"] == "2560*1080"
    assert lock["video_resolution"] == "1080P"
