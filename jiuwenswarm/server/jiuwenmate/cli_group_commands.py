"""Recognize sensitive shell text and extract a single bounded readonly CLI plan."""

from dataclasses import dataclass
import base64
import binascii
import json
import re
import shlex
from urllib.parse import parse_qsl, unquote, urlsplit


class UnsafeGroupRead(ValueError):
    """An apparent message read cannot safely execute as an arbitrary shell command."""


_HINT = re.compile(
    r"(?:[+](?:chat-messages|messages|threads-messages|pins)|[/\\]im[/\\]v\d|[/\\]search[/\\]v\d.*message"
    r"|\bim\s+(?:messages?|threads?|pins|files|images)\b|\bim\s+\+[\w-]*message[\w-]*|\.im\.v\d+\.)", re.I,
)
_CLI = re.compile(r"(?:^|[/\\])lark-cli(?:\.(?:cmd|exe|ps1|bat))?$", re.I)
_BOOL = {"--json", "--no-reactions", "--page-all", "--help", "-h", "--dry-run"}
_VALUES = {"--as", "--chat-id", "--message-id", "--message-ids", "--file-key", "--type", "--output",
           "--thread", "--query", "--start", "--end", "--order", "--page-size", "--page-limit",
           "--page-token", "--format", "--params", "--data", "--path", "--sender", "--sender-type",
           "--exclude-sender-type", "--chat-type", "--include-attachment-type", "--at-chatter-ids"}
_SHORTCUTS = {"+chat-messages-list": "list", "+messages-search": "search", "+messages-mget": "get",
              "+messages-resources-download": "download", "+threads-messages-list": "thread"}


@dataclass(frozen=True)
class CliReadPlan:
    operation: str
    identity: str = "bot"
    chat_ids: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    file_key: str = ""
    resource_type: str = "file"
    thread_id: str = ""
    query: str = ""
    start: str = ""
    end: str = ""
    order: str = "desc"
    page_size: int = 50
    page_limit: int = 1
    page_token: str = ""
    sender: str = ""

    def argv(self, *, allowed_chats: tuple[str, ...] = ()) -> list[str]:
        """Build trusted readonly arguments; never forward arbitrary shell text or download flags."""
        names = {value: key for key, value in _SHORTCUTS.items()}
        args = ["im", names[self.operation], "--as", self.identity, "--format", "json"]
        if self.operation in {"list", "search"}:
            chats = self.chat_ids or allowed_chats
            if chats:
                args += ["--chat-id", ",".join(chats)]
        if self.operation == "get":
            args += ["--message-ids", ",".join(self.message_ids)]
        if self.operation == "thread":
            args += ["--thread", self.thread_id]
        if self.operation == "download":
            return ["im", "+messages-mget", "--message-ids", ",".join(self.message_ids),
                    "--as", self.identity, "--format", "json", "--no-reactions"]
        if self.operation in {"list", "search", "thread"}:
            args += ["--page-size", str(self.page_size)]
            if self.page_limit > 1:
                args += ["--page-all", "--page-limit", str(self.page_limit)]
            if self.page_token:
                args += ["--page-token", self.page_token]
        if self.operation in {"list", "thread"}:
            args += ["--order", self.order]
        for flag, value in (("--query", self.query), ("--start", self.start), ("--end", self.end),
                            ("--sender", self.sender)):
            if value:
                args += [flag, value]
        if self.operation != "thread":
            args += ["--no-reactions"]
        return args


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
    lexer.escape = ""
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _options(words: list[str]) -> tuple[list[str], dict]:
    positional, options = [], {}
    index = 0
    while index < len(words):
        word = words[index]
        if word in _BOOL:
            if word in options:
                raise UnsafeGroupRead("duplicate CLI option")
            options[word] = True
        elif word.startswith("--"):
            flag, equal, value = word.partition("=")
            if flag not in _VALUES or flag in options:
                raise UnsafeGroupRead("unsupported or duplicate CLI option")
            if not equal:
                index += 1
                if index >= len(words):
                    raise UnsafeGroupRead("missing CLI option value")
                value = words[index]
            if not value or value.startswith("@") or value == "-" or any(char in value for char in "$%`\x00\r\n"):
                raise UnsafeGroupRead("dynamic or file-backed CLI parameters are not supported")
            options[flag] = value
        elif word.startswith("-"):
            raise UnsafeGroupRead("unsupported CLI option")
        else:
            positional.append(word)
        index += 1
    return positional, options


def _json_option(options: dict, name: str) -> dict:
    try:
        value = json.loads(options.get(name, "{}"))
    except (TypeError, ValueError):
        raise UnsafeGroupRead("invalid JSON CLI parameters") from None
    if not isinstance(value, dict):
        raise UnsafeGroupRead("CLI parameters must be a JSON object")
    return value


def _ids(value, prefix: str, limit: int) -> tuple[str, ...]:
    values = value.split(",") if isinstance(value, str) else value or ()
    if not isinstance(values, (list, tuple)) or len(values) > limit or any(
        not isinstance(item, str) or re.fullmatch(prefix + r"[A-Za-z0-9_-]+", item) is None for item in values
    ):
        raise UnsafeGroupRead("invalid or excessive CLI resource IDs")
    return tuple(dict.fromkeys(values))


def parse_group_read(command: str) -> CliReadPlan | None:
    """Return None for unrelated commands; apparent unsafe reads must never fall through."""
    if not isinstance(command, str):
        return None
    text = re.sub(r"[\\^`]\r?\n", "", command).replace("^", "").replace("`", "")
    text = unquote(unquote(text))
    detection = re.sub(r"\\(?:x([a-fA-F0-9]{2})|u([a-fA-F0-9]{4}))",
                       lambda match: chr(int(match.group(1) or match.group(2), 16)), text)
    if detection != text and _HINT.search(detection):
        raise UnsafeGroupRead("encoded API or message arguments require an explicit CLI read")
    for token in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", text):
        if len(token) > 128 * 1024:
            continue
        try:
            decoded = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error):
            continue
        if any(_HINT.search(decoded.decode(encoding, errors="ignore"))
               for encoding in ("utf-8", "utf-16-le")):
            raise UnsafeGroupRead("encoded message scripts require an explicit CLI read")
    hint = bool(_HINT.search(text))
    if not hint and not re.search(r'''lark-cli\S*\s+api\s+\S+\s+["']?[$%]''', text, re.I):
        return None
    try:
        words = _tokens(text)
        for _ in range(3):
            if words and words[0].lower().removesuffix(".exe") in {"cmd", "powershell", "pwsh", "bash", "sh"}:
                marker = next((i for i, word in enumerate(words) if word.lower() in {"/c", "/s", "-c", "-command"}), None)
                if marker is None or marker + 1 >= len(words):
                    raise UnsafeGroupRead("opaque shell wrapper")
                rest = words[marker + 1:]
                words = _tokens(rest[0]) if len(rest) == 1 else rest
            else:
                break
        if words and words[0] == "&":
            words = words[1:]
        while "&&" in words:
            boundary = words.index("&&")
            prefix = words[:boundary]
            if not prefix or prefix[0].lower() not in {"cd", "set"}:
                raise UnsafeGroupRead("a CLI read cannot replay shell side effects")
            if prefix[0].lower() == "set" and not any(
                item.startswith(("LARKSUITE_CLI_NO_UPDATE_NOTIFIER=", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER="))
                for item in prefix[1:]
            ):
                raise UnsafeGroupRead("unsupported environment mutation")
            words = words[boundary + 1:]
        if not words or not _CLI.search(words[0]) or any(word in {";", "&&", "||", "|", ">", ">>", "<", "(", ")", "&"} for word in words):
            raise UnsafeGroupRead("use one explicit CLI read without a pipeline, redirection or script")
        if (len(words) >= 3 and words[1] == "im"
                and words[2] in {"+messages-send", "+messages-reply", "+messages-update", "+messages-delete"}
                and not _HINT.search(" ".join(words[3:]))):
            return None
        positional, opts = _options(words[1:])
        if "--help" in opts or "-h" in opts or "--dry-run" in opts:
            return None
        identity = opts.get("--as", "bot")
        if identity not in {"bot", "user"} or opts.get("--format", "json") != "json":
            raise UnsafeGroupRead("only explicit bot/user JSON reads are supported")
        if not positional:
            raise UnsafeGroupRead("missing message operation")
        data = {}
        for name in ("--params", "--data", "--path"):
            values = _json_option(opts, name)
            if set(data).intersection(values):
                raise UnsafeGroupRead("ambiguous duplicate CLI parameters")
            data.update(values)
        operation = None
        if positional[0] == "im":
            if len(positional) == 2:
                operation = _SHORTCUTS.get(positional[1])
            elif len(positional) == 3 and positional[1] in {"message", "messages"}:
                operation = {"get": "get", "list": "list", "search": "search"}.get(positional[2])
        elif positional[0] == "api" and len(positional) == 3:
            method, url = positional[1].upper(), urlsplit(positional[2])
            path = url.path
            for key, value in parse_qsl(url.query, keep_blank_values=True):
                if key in data:
                    raise UnsafeGroupRead("ambiguous duplicate URL parameters")
                data[key] = value
            if re.fullmatch(r"/(?:open-apis/)?search/v\d+/message", path) and method == "POST":
                operation = "search"
            elif method == "GET":
                match = re.fullmatch(r"/(?:open-apis/)?im/v\d+/messages(?:/(om_[\w-]+)(?:/resources/([\w-]+))?)?", path)
                if match:
                    message_id, key = match.groups()
                    operation = "download" if key else "get" if message_id else "list"
                    data |= {key: value for key, value in {"message_id": message_id, "file_key": key}.items() if value}
        if operation is None:
            raise UnsafeGroupRead("unrecognized message operation")
        allowed_data = {"chat_ids", "container_id", "container_id_type", "message_id", "file_key", "type",
                        "query", "start_time", "end_time", "page_size", "page_token", "sort_type"}
        if set(data) - allowed_data or data.get("container_id_type", "chat") != "chat":
            raise UnsafeGroupRead("unsupported CLI source or query filter")
        if any(key in opts for key in ("--sender-type", "--exclude-sender-type", "--chat-type",
                                       "--include-attachment-type", "--at-chatter-ids")):
            raise UnsafeGroupRead("unsupported CLI query filter; it cannot be discarded")
        chat_ids = _ids(opts.get("--chat-id", data.get("chat_ids", data.get("container_id"))), "oc_", 128)
        message_ids = _ids(opts.get("--message-ids", opts.get("--message-id", data.get("message_id"))), "om_", 50)
        if operation == "list" and len(chat_ids) != 1 or operation in {"get", "download"} and not message_ids:
            raise UnsafeGroupRead("missing exact source resource")
        file_key, resource_type = opts.get("--file-key", data.get("file_key", "")), opts.get("--type", data.get("type", "file"))
        thread = opts.get("--thread", "")
        if operation == "download" and (len(message_ids) != 1 or not re.fullmatch(r"(?:file|img)_[\w-]+", file_key) or resource_type not in {"file", "image"}):
            raise UnsafeGroupRead("missing message attachment identity")
        if operation == "thread" and re.fullmatch(r"omt_[\w-]+", thread) is None:
            raise UnsafeGroupRead("missing thread identity")
        page_size = int(opts.get("--page-size", data.get("page_size", 50)))
        pages = int(opts.get("--page-limit", 10 if "--page-all" in opts else 1))
        if not 1 <= page_size <= 50 or not 1 <= pages <= 10 or page_size * pages > 500:
            raise UnsafeGroupRead("CLI preview exceeds the approval limit")
        order = opts.get("--order", {"ByCreateTimeAsc": "asc", "ByCreateTimeDesc": "desc"}.get(
            data.get("sort_type", "ByCreateTimeDesc")))
        if order not in {"asc", "desc"}:
            raise UnsafeGroupRead("unsupported CLI sort order")
        return CliReadPlan(operation, identity, chat_ids, message_ids, file_key, resource_type, thread,
                           opts.get("--query", str(data.get("query", ""))), str(opts.get("--start", data.get("start_time", ""))),
                           str(opts.get("--end", data.get("end_time", ""))), order, page_size, pages,
                           str(opts.get("--page-token", data.get("page_token", ""))), opts.get("--sender", ""))
    except (ValueError, TypeError, IndexError) as exc:
        if isinstance(exc, UnsafeGroupRead):
            raise
        raise UnsafeGroupRead("message command could not be safely parsed") from None
