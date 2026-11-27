# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read selected Outlook MAPI fields without opening attachment or embedded-message payloads."""

import codecs
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from io import BytesIO
from locale import windows_locale

from olefile import DEFECT_INCORRECT, OleFileIO
from oxmsg.domain import constants as props
from oxmsg.domain import model
from oxmsg.domain.encodings import encoding_from_codepage
from oxmsg.properties import Properties

_MAX_DIRECTORY_ENTRIES = 8192
_MAX_PROPERTY_TABLE_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 1024 * 1024


def _encoding(code_page: int | None, default: str) -> str:
    if not code_page:
        return default
    try:
        return codecs.lookup(f"cp{code_page}").name
    except LookupError:
        return encoding_from_codepage(code_page)


@dataclass
class OutlookProperties:
    """An oxmsg property table with variable-length fields opened on demand."""

    ole: OleFileIO
    directory: str = ""
    default_encoding: str = "cp1252"

    def _path(self, name: str) -> list[str]:
        return [self.directory, name] if self.directory else [name]

    def _read(self, path: list[str], limit: int | None) -> bytes:
        size = self.ole.get_size(path)
        if size == 0:
            return b""
        if limit is not None and size > limit:
            raise ValueError("Outlook metadata exceeds the reading limit.")
        with self.ole.openstream(path) as stream:
            return stream.read()

    @cached_property
    def properties_stream_bytes(self) -> bytes:
        return self._read(self._path("__properties_version1.0"), _MAX_PROPERTY_TABLE_BYTES)

    def _property_path(self, pid: int, ptyp: int) -> list[str]:
        return self._path(f"__substg1.0_{pid:04X}{ptyp:04X}")

    def property_stream_bytes(self, pid: int, ptyp: int) -> bytes:
        limit = None if pid in {props.PID_BODY, props.PID_HTML} else _MAX_METADATA_BYTES
        return self._read(self._property_path(pid, ptyp), limit)

    @cached_property
    def properties(self) -> Properties:
        offset = model.ATTACH_HDR_OFFSET if self.directory else model.MSG_HDR_OFFSET
        return Properties(self, properties_header_offset=offset)

    @cached_property
    def _message_code_page(self) -> int | None:
        code_page = self.properties.int_prop_value(props.PID_MESSAGE_CODEPAGE)
        if code_page:
            return code_page
        # The HTML charset can differ from the ANSI code page of the Outlook client.
        locale_id = self.properties.int_prop_value(props.PID_MESSAGE_LOCALE_ID)
        language = windows_locale.get(locale_id or 0, "").split("_")[0]
        if language in {"da", "de", "en", "es", "fi", "fr", "is", "it", "nl", "no", "pt", "sv"}:
            return 1252
        return None

    @cached_property
    def _message_encoding(self) -> str:
        return _encoding(self._message_code_page, self.default_encoding)

    @cached_property
    def _internet_encoding(self) -> str:
        return _encoding(self.properties.int_prop_value(props.PID_INTERNET_CODEPAGE), self._message_encoding)

    def text(self, pid: int) -> str:
        types = [props.PTYP_STRING, props.PTYP_STRING8]
        if pid == props.PID_HTML:
            types.insert(0, props.PTYP_BINARY)
        for ptyp in types:
            if self.ole.exists(self._property_path(pid, ptyp)):
                raw = self.property_stream_bytes(pid, ptyp)
                if ptyp == props.PTYP_STRING:
                    return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
                if pid in {props.PID_BODY, props.PID_HTML} or not self._message_code_page:
                    try:
                        return raw.decode(self._internet_encoding).rstrip("\x00")
                    except UnicodeDecodeError:
                        pass  # Some Outlook exports keep the plain body in the message code page.
                return raw.decode(self._message_encoding, errors="replace").rstrip("\x00")
        return ""

    def attachment_names(self, limit: int) -> list[str]:
        names: list[str] = []
        for path in self.ole.listdir(streams=False, storages=True):
            if len(path) != 1 or not path[0].startswith("__attach_version1.0_#"):
                continue
            if len(names) == limit:
                break
            encoding = self._message_encoding if self._message_code_page else self._internet_encoding
            attachment = OutlookProperties(self.ole, path[0], encoding)
            names.append(
                attachment.text(props.PID_ATTACH_LONG_FILENAME)
                or attachment.text(props.PID_ATTACH_FILENAME)
                or attachment.text(props.PID_DISPLAY_NAME)
                or "(unnamed attachment)"
            )
        return names

    def has_rtf(self) -> bool:
        return self.ole.exists(self._property_path(props.PID_RTF_COMPRESSED, props.PTYP_BINARY))


@contextmanager
def open_outlook_message(data: bytes) -> Iterator[OutlookProperties]:
    """Keep the OLE container open while the caller reads selected properties."""
    with OleFileIO(BytesIO(data), raise_defects=DEFECT_INCORRECT) as ole:
        if len(ole.direntries) > _MAX_DIRECTORY_ENTRIES:
            raise ValueError("Outlook directory exceeds the reading limit.")
        yield OutlookProperties(ole)
