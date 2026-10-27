import io
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common import obs_files
from jiuwenswarm.common.obs_files import AttachmentStore, object_prefix, download_object_key
from jiuwenswarm.server.runtime.attachments.obs_attachments import _open_uploads


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_directory_walk_does_not_close_reused_descriptor(
    tmp_path, monkeypatch, interruption
):
    real_open, real_close = os.open, os.close
    children, closed, reused = [], [], []

    def opened(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        if "dir_fd" in kwargs:
            children.append(descriptor)
        return descriptor

    def interrupted_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)
        if len(closed) == 1:
            reused.append(real_open(tmp_path, os.O_RDONLY))
            assert reused[0] == descriptor
            raise interruption()

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", interrupted_close)
    try:
        with pytest.raises(interruption):
            _open_uploads(tmp_path, tmp_path / "sessions" / "uploads")
        assert closed == [reused[0], children[0]]
        os.fstat(reused[0])  # The descriptor opened by another owner remains live.
        with pytest.raises(OSError):
            os.fstat(children[0])
    finally:
        for descriptor in reused:
            real_close(descriptor)


@pytest.mark.parametrize("size", [0, 4, 5])
def test_workspace_upload_enforces_limit_before_hash_or_put(
    tmp_path, monkeypatch, size
):
    monkeypatch.setattr(obs_files, "MAX_FILE_BYTES", 4)
    source = tmp_path / "file"
    source.write_bytes(b"x" * size)
    client = Mock()
    store = AttachmentStore(client, "bucket", "http://obs")
    if size > 4:
        with pytest.raises(ValueError, match="exceeds"):
            store.upload_file(source, "key")
        client.put_object.assert_not_called()
    else:
        client.put_object.side_effect = lambda bucket, key, reader, length, **kw: (
            reader.read(),
            SimpleNamespace(etag="etag"),
        )[1]
        assert store.upload_file(source, "key")["size"] == size




@pytest.mark.parametrize("operation", ["download", "read", "request"])
def test_connections_and_pools_release_even_if_close_or_request_raises(
    tmp_path, monkeypatch, operation
):
    response = Mock(status=200)
    response.read.side_effect = [b"data", b""]
    response.close.side_effect = OSError("close failed")
    transport = Mock(request=Mock(return_value=response))
    if operation == "request":
        transport.request.side_effect = OSError("request failed")
    monkeypatch.setattr(obs_files, "PoolManager", lambda **kw: transport)
    store = AttachmentStore(
        Mock(get_object=Mock(return_value=response)), "bucket", "http://obs"
    )
    prefix = object_prefix("g", "b", "u")
    with pytest.raises(OSError):
        if operation == "download":
            key = download_object_key("tenant", "a" * 32, "file")
            store.read_download(f"http://obs/bucket/{key}", "tenant")
        else:
            store.read(f"http://obs/bucket/{prefix}{'a' * 32}/file", prefix)
    if operation != "request":
        response.release_conn.assert_called_once()
    transport.clear.assert_called_once()


def test_presign_failure_rolls_back_uploaded_object():
    client = Mock()
    client.presigned_get_object.side_effect = RuntimeError("sign failed")
    with pytest.raises(RuntimeError, match="sign failed"):
        AttachmentStore(client, "bucket", "http://obs").upload(
            b"data", "a.txt", object_prefix("g", "b", "u")
        )
    bucket, key = client.put_object.call_args.args[:2]
    client.remove_object.assert_called_once_with(bucket, key)
