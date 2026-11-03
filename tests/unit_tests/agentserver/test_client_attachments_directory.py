# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The directory of attachments: one per socket and resource, each held by a lease its client renews."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.client_attachments import directory as directory_module
from jiuwenswarm.server.client_attachments.directory import LEASE_SECONDS, AttachmentDirectory
from tests.unit_tests.agentserver.client_attachments_support import Clock

RESOURCE = {"key": "office-deck", "display_name": "Roadmap.pptx",
            "kind": "presentation", "host_vendor": "microsoft"}
PLUGIN = "office-powerpoint"


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(directory_module, "clock", clock)
    return clock


def _attach(directory: AttachmentDirectory, *, ws_id: str = "ws-1", session_id: str = "s1", resource=RESOURCE):
    return directory.attach(channel_id="web", ws_id=ws_id, owner_id="u", plugin_id=PLUGIN,
                            resource=resource, session_id=session_id)


def test_the_directory_replaces_a_reattached_or_switched_client(clock) -> None:
    directory = AttachmentDirectory()
    first = _attach(directory, session_id="s1")
    _attach(directory, session_id="s2")
    assert [c.session_id for c in directory.connections()] == ["s2"]
    _attach(directory, ws_id="ws-2", session_id="s2")
    assert [c.ws_id for c in directory.connections()] == ["ws-2"]
    _attach(directory, ws_id="ws-2", session_id="s3", resource={**RESOURCE, "key": "other"})
    assert [c.attachment_id != first.attachment_id for c in directory.connections()] == [True]
    assert directory.connection_for_session("") is None


def test_an_attachment_lapses_unless_its_client_renews_it(clock) -> None:
    directory = AttachmentDirectory()
    renewed = _attach(directory, ws_id="ws-1", session_id="s1")
    lapsing = _attach(directory, ws_id="ws-2", session_id="s2", resource={**RESOURCE, "key": "other"})

    clock.advance(LEASE_SECONDS - 1)
    assert directory.expire(clock.now) == []
    assert directory.renew(channel_id="web", ws_id="ws-1", attachment_id=renewed.attachment_id, session_id="s1")
    clock.advance(1)

    assert directory.expire(clock.now) == [lapsing]
    assert [c.session_id for c in directory.connections()] == ["s1"]
    assert directory.expire(clock.now + LEASE_SECONDS - 2) == []


@pytest.mark.parametrize("names", ["another attachment", "another socket", "another conversation"])
def test_a_renew_that_names_another_attachment_than_the_socket_s_finds_none(clock, names) -> None:
    directory = AttachmentDirectory()
    attached = _attach(directory)
    renew = {"channel_id": "web", "ws_id": "ws-1", "attachment_id": attached.attachment_id, "session_id": "s1"}
    renew.update({"another attachment": {"attachment_id": "att_other"}, "another socket": {"ws_id": "ws-2"},
                  "another conversation": {"session_id": "s2"}}[names])
    clock.advance(LEASE_SECONDS - 1)

    assert directory.renew(**renew) is None
    assert directory.expire(clock.now + 1) == [attached]


def test_a_stopping_server_forgets_every_attachment(clock) -> None:
    directory = AttachmentDirectory()
    _attach(directory)
    directory.close()
    assert directory.connections() == ()
