"""Shared fixtures for integration tests against a real Google Doc.

The split with the offline unit tests: those run recorded JSON and never touch the
network, while this directory **calls the real API** to check whether our assumptions
about the platform's behaviour still hold. Platforms change, and this layer is the
change detector.

It runs only when both of these are set; missing either skips the directory rather
than failing:

    CO_SCRIBE_CREDENTIALS   path to the service account's JSON key
    CO_SCRIBE_TEST_DOC      test document id or link, with the account as an editor

Every case cleans up after itself by working inside a sandbox it carves out of the
document and then deletes, so nothing depends on external state.
"""

from __future__ import annotations

import os
import uuid

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
    GoogleDocsProvider,
)

_CRED = os.environ.get("CO_SCRIBE_CREDENTIALS")
_DOC = os.environ.get("CO_SCRIBE_TEST_DOC")

pytestmark = pytest.mark.integration

# httplib2 does not close its sockets, so exit prints a run of ResourceWarnings.
# They say nothing about the behaviour under test but do bury real failures, so they
# are silenced within this directory.
_SOCKET_NOISE = "ignore::ResourceWarning"


def _missing() -> str | None:
    if not _CRED:
        return "CO_SCRIBE_CREDENTIALS 未设置"
    if not os.path.isfile(_CRED):
        return f"凭证文件不存在：{_CRED}"
    if not _DOC:
        return "CO_SCRIBE_TEST_DOC 未设置"
    try:
        import googleapiclient  # noqa: F401
    except ImportError:
        return "未安装 google extras"
    return None


def pytest_collection_modifyitems(config, items):
    """Skip the cases in *this* directory when the credentials are missing.

    The hook is global: pytest hands it every collected item, not just the ones under
    this conftest. Marking all of them would skip the offline unit tests too — they
    read recorded JSON and need no credentials — so the whole clouddoc suite would go
    silently green-by-skip. Only items whose path lies under this directory are ours.
    """
    reason = _missing()
    if not reason:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    skip = pytest.mark.skip(reason=f"跳过真实 Doc 集成测试：{reason}")
    for item in items:
        if os.path.abspath(str(item.path)).startswith(here + os.sep):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def provider() -> GoogleDocsProvider:
    return GoogleDocsProvider(_CRED)


@pytest.fixture(scope="session")
def doc_id(provider) -> str:
    return provider.parse_doc_ref(_DOC)


@pytest.fixture
async def sandbox(provider, doc_id):
    """Carve a uniquely marked sandbox at the end of the document, then delete it along
    with its marker.

    The marker carries a uuid, so concurrent runs cannot step on each other; every
    assertion locates its own text through its own marker.
    """
    marker = f"@@IT-{uuid.uuid4().hex[:8]}@@"
    docs, _ = provider._clients()

    def _end() -> int:
        d = docs.documents().get(documentId=doc_id).execute()
        return d["body"]["content"][-1]["endIndex"] - 1

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": _end()}, "text": f"\n{marker}\n"}}]},
    ).execute()

    class Sandbox:
        def __init__(self) -> None:
            self.marker = marker

        def append(self, text: str) -> None:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": _end()}, "text": text}}]},
            ).execute()

    yield Sandbox()

    # Clean up: delete from the marker to the end of the document
    snap = await provider.read(doc_id)
    start_char = snap.text.find(marker)
    if start_char >= 0:
        seg_start = None
        for seg in snap.segments:
            if seg.char_start <= start_char < seg.char_end:
                seg_start = seg.index_start + (start_char - seg.char_start)
                break
        if seg_start is not None:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={
                    "requests": [
                        {
                            "deleteContentRange": {
                                "range": {"startIndex": seg_start - 1, "endIndex": _end()}
                            }
                        }
                    ]
                },
            ).execute()
