#!/usr/bin/env python3
"""Download an Azure Public Service Tags publication."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DOWNLOAD_PATTERN = re.compile(
    r"ServiceTags_Public_(?P<date>\d{8})\.json", re.IGNORECASE
)


class ServiceTagsError(RuntimeError):
    """Raised when Service Tags data cannot be safely downloaded."""


class ServiceTagsNotFound(ServiceTagsError):
    """Raised when Microsoft has not published the requested dated file."""


def _request_bytes(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": "ip-allow-list-workflow/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code == 404:
            raise ServiceTagsNotFound(
                f"Service Tags publication was not found: {url}"
            ) from exc
        raise ServiceTagsError(
            f"Unable to retrieve {url}: HTTP {exc.code} {exc.reason}"
        ) from exc
    except OSError as exc:
        raise ServiceTagsError(f"Unable to retrieve {url}: {exc}") from exc


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    temporary.replace(path)


def write_github_outputs(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for name, value in values.items():
                if "\n" in value or "\r" in value:
                    raise ServiceTagsError(
                        f"GitHub output {name!r} contains an unexpected newline"
                    )
                stream.write(f"{name}={value}\n")
    except OSError as exc:
        raise ServiceTagsError(f"Unable to write GitHub outputs to {path}: {exc}") from exc


def download_service_tags(download_dir: Path, download_url: str) -> Path:
    parsed_url = urlparse(download_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or any(character.isspace() for character in download_url)
    ):
        raise ServiceTagsError(
            f"Download URL must be a valid HTTPS URL: {download_url}"
        )

    filename = Path(parsed_url.path).name
    match = DOWNLOAD_PATTERN.fullmatch(filename)
    if match is None:
        raise ServiceTagsError(
            f"Download URL has an unexpected filename: {download_url}"
        )

    destination = download_dir / match.group(0)
    payload = _request_bytes(download_url)
    try:
        json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceTagsError(
            f"Downloaded file is not valid JSON: {download_url}"
        ) from exc
    _write_bytes(destination, payload)
    return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--github-output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    github_output = Path(args.github_output) if args.github_output else None
    try:
        try:
            downloaded = download_service_tags(args.download_dir, args.download_url)
        except ServiceTagsNotFound as exc:
            write_github_outputs(github_output, {"available": "false"})
            print(f"notice: {exc}", file=sys.stderr)
            return 0

        date_match = DOWNLOAD_PATTERN.fullmatch(downloaded.name)
        if date_match is None:
            raise ServiceTagsError(
                f"Downloaded file has an unexpected name: {downloaded.name}"
            )
        write_github_outputs(
            github_output,
            {
                "available": "true",
                "download_path": str(downloaded),
                "source_filename": downloaded.name,
                "source_date": date_match.group("date"),
            },
        )
        print(downloaded)
    except ServiceTagsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
