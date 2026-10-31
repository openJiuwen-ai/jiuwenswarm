# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from minio.error import S3Error

from jiuwenswarm.common.obs_files import AttachmentStore


def s3_error(code):
    return S3Error(None, code, "test failure", "", "", "")


@pytest.mark.parametrize("operation", ["attachment", "delivery"])
@pytest.mark.parametrize(
    "state", ["exists", "missing", "owned_race", "exists_race", "still_missing", "denied", "network"]
)
def test_upload_initializes_bucket_without_hiding_storage_errors(tmp_path, operation, state):
    client = Mock()
    client.bucket_exists.return_value = state == "exists"
    client.put_object.side_effect = lambda bucket, key, reader, length, **kwargs: (
        reader.read(), SimpleNamespace(etag="etag")
    )[1]
    client.presigned_get_object.return_value = "http://obs/bucket/key"
    if state in {"owned_race", "exists_race", "still_missing"}:
        client.bucket_exists.side_effect = [False, state != "still_missing"]
        client.make_bucket.side_effect = s3_error(
            "BucketAlreadyOwnedByYou" if state == "owned_race" else "BucketAlreadyExists"
        )
    elif state == "denied":
        client.make_bucket.side_effect = s3_error("AccessDenied")
    elif state == "network":
        client.bucket_exists.side_effect = OSError("storage unavailable")
    source = tmp_path / "file.txt"
    source.write_bytes(b"data")
    store = AttachmentStore(client, "bucket", "http://obs")

    def upload():
        if operation == "attachment":
            return store.upload(b"data", "file.txt", "uploads/")
        return store.upload_file(source, "downloads/file.txt")

    if state in {"still_missing", "denied", "network"}:
        with pytest.raises((S3Error, OSError)):
            upload()
        client.put_object.assert_not_called()
    else:
        upload()
        client.put_object.assert_called_once()
        if state == "exists":
            client.make_bucket.assert_not_called()
        else:
            client.make_bucket.assert_called_once_with("bucket")
