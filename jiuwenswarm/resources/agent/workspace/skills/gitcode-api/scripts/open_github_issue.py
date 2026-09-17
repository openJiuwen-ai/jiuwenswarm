"""Create a GitHub issue (e.g. feedback for GitCode-API)."""

import argparse
from functools import partial
import os
import sys
from typing import Optional, Sequence

from githubkit import GitHub

DEFAULT_OWNER = "Trenza1ore"
DEFAULT_REPO = "GitCode-API"
API_VERSION = "2026-03-10"

output_content = partial(print)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Create a GitHub issue.")
    parser.add_argument("title", help="Issue title")
    parser.add_argument("--body", default="", required=True, help="Issue body")
    parser.add_argument(
        "--label",
        dest="labels",
        action="append",
        help="Label (repeatable)",
    )
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--api-key", default="", help="GitHub API Token")
    args = parser.parse_args(argv)

    token = os.getenv("GITHUB_ACCESS_TOKEN") or args.api_key
    if not token:
        output_content("GITHUB_ACCESS_TOKEN is not set", file=sys.stderr)
        return 1

    payload: dict = {
        "owner": args.owner,
        "repo": args.repo,
        "title": args.title,
        "body": args.body,
    }
    if args.labels:
        payload["labels"] = list(args.labels)

    gh = GitHub(auth=token)
    resp = gh.rest(version=API_VERSION).issues.create(**payload)
    output_content(resp.parsed_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
