# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the real SMTP and IMAP transports (E6 step 5)."""
from email.message import EmailMessage

import pytest

import jiuwenswarm.gateway.channel_manager.im_platforms.email.email_transport as transport


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.tls = False
        self.creds = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, mail):
        self.sent.append(mail)


class _FakeIMAP:
    def __init__(self, ids=b"1 2", search_status="OK"):
        self.selected = None
        self.logged_out = False
        self._ids = ids
        self._bodies = {b"1": b"raw-one", b"2": b"raw-two"}
        self._search_status = search_status

    def login(self, user, password):
        self.creds = (user, password)

    def select(self, mailbox):
        self.selected = mailbox

    def search(self, charset, criteria):
        return self._search_status, [self._ids]

    def fetch(self, message_id, spec):
        return "OK", [(b"header", self._bodies[message_id])]

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeSMTP.instances.clear()


def test_smtp_sender_uses_starttls_login_and_sends(monkeypatch):
    monkeypatch.setattr(transport.smtplib, "SMTP", _FakeSMTP)
    mail = EmailMessage()
    mail["To"] = "a@x.com"
    mail.set_content("hi")
    transport.SmtpSender("smtp.x.com", 587, "bot@x.com", "pw")(mail)
    server = _FakeSMTP.instances[0]
    assert (server.host, server.port) == ("smtp.x.com", 587)
    assert server.tls is True
    assert server.creds == ("bot@x.com", "pw")
    assert server.sent == [mail]


def test_imap_fetcher_returns_raw_bodies(monkeypatch):
    box = _FakeIMAP()
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    mails = transport.ImapFetcher("imap.x.com", "bot@x.com", "pw").fetch_unseen()
    assert mails == [b"raw-one", b"raw-two"]
    assert box.selected == "INBOX"
    assert box.logged_out is True


def test_imap_fetcher_honours_the_limit(monkeypatch):
    box = _FakeIMAP()
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    mails = transport.ImapFetcher("imap.x.com", "u", "p", limit=1).fetch_unseen()
    assert mails == [b"raw-two"]


def test_imap_fetcher_returns_empty_on_failed_search(monkeypatch):
    box = _FakeIMAP(search_status="NO")
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    assert transport.ImapFetcher("imap.x.com", "u", "p").fetch_unseen() == []


def test_imap_fetcher_returns_empty_when_no_unseen(monkeypatch):
    box = _FakeIMAP(ids=b"")
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    assert transport.ImapFetcher("imap.x.com", "u", "p").fetch_unseen() == []


def test_imap_fetcher_logs_out_even_on_error(monkeypatch):
    box = _FakeIMAP()

    def boom(*args, **kwargs):
        raise RuntimeError("login refused")

    box.login = boom
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    with pytest.raises(RuntimeError):
        transport.ImapFetcher("imap.x.com", "u", "p").fetch_unseen()
    assert box.logged_out is True


def test_imap_fetcher_uses_the_configured_mailbox(monkeypatch):
    box = _FakeIMAP()
    monkeypatch.setattr(transport.imaplib, "IMAP4_SSL", lambda host, port: box)
    transport.ImapFetcher("imap.x.com", "u", "p", mailbox="Archive").fetch_unseen()
    assert box.selected == "Archive"