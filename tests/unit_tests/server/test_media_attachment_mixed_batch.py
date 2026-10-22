# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Media-item batches may mix documents with images."""

from jiuwenswarm.server.runtime.attachments import media_attachments


def test_images_after_eight_documents_are_still_stored(monkeypatch):
    stored_requests = []

    def fake_store(item, **kwargs):
        stored_requests.append(item)
        return {"type": "image", "path": "/tmp/session/uploads/photo.png"}

    monkeypatch.setattr(media_attachments, "_store_image_item", fake_store)

    image = {"type": "image", "mime_type": "image/png", "filename": "photo.png"}
    params = {"media_items": [{"type": "document"}] * 8 + [image]}

    media_attachments.normalize_chat_media_attachments(params, session_id="sess-mixed")

    assert stored_requests == [image]
    assert params["media_items"] == [
        {"type": "image", "path": "/tmp/session/uploads/photo.png"}
    ]
    assert params["files"]["uploaded_images"][0]["path"] == "/tmp/session/uploads/photo.png"
