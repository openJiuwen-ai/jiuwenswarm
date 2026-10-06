"""Read-only dotenv diagnostics; never include config values in output."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def check(path: Path) -> dict:
    raw = path.read_bytes()
    values = dotenv_values(stream=io.StringIO(raw.decode("utf-8-sig")), interpolate=False)
    errors = []
    for key in ("API_BASE", "API_KEY", "MODEL_NAME"):
        value = (values.get(key) or "").strip()
        if not value:
            errors.append(f"{key}: missing or empty")
        elif value.lower() in {"replace-locally", "your-api-key", "your-model-name"}:
            errors.append(f"{key}: example placeholder")
        elif "${" in value:
            errors.append(f"{key}: interpolation requires runtime verification")
    base = (values.get("API_BASE") or "").strip()
    if base:
        try:
            url = urlsplit(base)
            valid = (
                url.scheme in {"http", "https"} and bool(url.hostname)
                and not any(char.isspace() for char in base)
                and not base.startswith("[")
                and not any(char in base for char in "*`")
                and url.username is None and url.password is None
            )
            # Access validates malformed/non-numeric ports without displaying them.
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            errors.append("API_BASE: expected a raw HTTP(S) URL without credentials or Markdown")
    return {"valid": not errors, "utf8_bom": raw.startswith(b"\xef\xbb\xbf"), "errors": errors}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotenv", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(args.dotenv)
    except (OSError, UnicodeError, ValueError):
        # Exception text may contain the path or file contents. Do not echo it.
        print(json.dumps({"valid": False, "errors": ["file cannot be read as UTF-8 dotenv"]}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
