#!/usr/bin/env python3
"""Discover, download, and process Azure Service Tags for Power BI."""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DOWNLOAD_PAGE_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=56519"
SERVICE_TAG_NAME = "PowerBI.CanadaCentral"
DOWNLOAD_PATTERN = re.compile(
    r"ServiceTags_Public_(?P<date>\d{8})\.json", re.IGNORECASE
)
URL_PATTERN = re.compile(
    r"https?://[^\"'<>\s]+ServiceTags_Public_\d{8}\.json", re.IGNORECASE
)


class ServiceTagsError(RuntimeError):
    """Raised when Service Tags data cannot be safely processed."""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src"} and value:
                self.links.append(value)


def _request_bytes(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": "ip-allow-list-workflow/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except OSError as exc:
        raise ServiceTagsError(f"Unable to retrieve {url}: {exc}") from exc


def find_download_urls(page: str, page_url: str = DOWNLOAD_PAGE_URL) -> list[str]:
    """Return dated Service Tags JSON links embedded in a download page."""
    normalized = html.unescape(page).replace("\\/", "/").replace("\\u002F", "/")
    normalized = normalized.replace("\\u002f", "/")
    parser = _LinkParser()
    parser.feed(normalized)

    candidates = [urljoin(page_url, link) for link in parser.links]
    candidates.extend(match.group(0) for match in URL_PATTERN.finditer(normalized))
    return sorted(
        {
            candidate.rstrip("\\")
            for candidate in candidates
            if DOWNLOAD_PATTERN.search(candidate)
        }
    )


def select_latest_download_url(candidates: Sequence[str]) -> str:
    if not candidates:
        raise ServiceTagsError("No ServiceTags_Public_YYYYMMDD.json link found")

    def source_date(candidate: str) -> str:
        match = DOWNLOAD_PATTERN.search(candidate)
        if match is None:
            raise ServiceTagsError(f"Unexpected Service Tags URL: {candidate}")
        return match.group("date")

    return max(candidates, key=source_date)


def discover_latest_download_url(page_url: str = DOWNLOAD_PAGE_URL) -> str:
    page = _request_bytes(page_url).decode("utf-8", errors="strict")
    candidates = find_download_urls(page, page_url)
    try:
        return select_latest_download_url(candidates)
    except ServiceTagsError as exc:
        raise ServiceTagsError(f"{exc} at {page_url}") from exc


def download_service_tags(download_dir: Path, page_url: str) -> Path:
    url = discover_latest_download_url(page_url)
    match = DOWNLOAD_PATTERN.search(url)
    if match is None:
        raise ServiceTagsError(f"Discovered URL has an unexpected filename: {url}")

    destination = download_dir / match.group(0)
    download_dir.mkdir(parents=True, exist_ok=True)
    payload = _request_bytes(url)
    try:
        json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceTagsError(f"Downloaded file is not valid JSON: {url}") from exc
    _write_bytes(destination, payload)
    return destination


def _sort_networks(prefixes: Iterable[str]) -> list[str]:
    networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for prefix in prefixes:
        if not isinstance(prefix, str):
            raise ServiceTagsError("Address prefixes must all be strings")
        try:
            networks.add(ipaddress.ip_network(prefix, strict=True))
        except ValueError as exc:
            raise ServiceTagsError(f"Invalid CIDR prefix {prefix!r}: {exc}") from exc
    return [
        str(network)
        for network in sorted(
            networks,
            key=lambda item: (item.version, int(item.network_address), item.prefixlen),
        )
    ]


def extract_prefixes(document: Any, tag_name: str = SERVICE_TAG_NAME) -> list[str]:
    if not isinstance(document, dict) or not isinstance(document.get("values"), list):
        raise ServiceTagsError("Service Tags document must contain a values array")

    matches = [
        value
        for value in document["values"]
        if isinstance(value, dict) and value.get("name") == tag_name
    ]
    if len(matches) != 1:
        raise ServiceTagsError(
            f"Expected exactly one {tag_name!r} entry; found {len(matches)}"
        )
    properties = matches[0].get("properties")
    if not isinstance(properties, dict) or not isinstance(
        properties.get("addressPrefixes"), list
    ):
        raise ServiceTagsError(f"{tag_name!r} must contain properties.addressPrefixes")
    if (
        properties.get("region") != "canadacentral"
        or properties.get("systemService") != "PowerBI"
    ):
        raise ServiceTagsError(
            f"{tag_name!r} has unexpected region or systemService metadata"
        )
    return _sort_networks(properties["addressPrefixes"])


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except OSError as exc:
        raise ServiceTagsError(f"Unable to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ServiceTagsError(f"Invalid JSON in {path}: {exc}") from exc


def load_snapshot(path: Path) -> list[str]:
    snapshot = load_json(path)
    if not isinstance(snapshot, dict):
        raise ServiceTagsError(f"Snapshot {path} must be a JSON object")
    if snapshot.get("serviceTag") != SERVICE_TAG_NAME:
        raise ServiceTagsError(
            f"Snapshot {path} must describe service tag {SERVICE_TAG_NAME!r}"
        )
    if not isinstance(snapshot.get("prefixes"), list):
        raise ServiceTagsError(f"Snapshot {path} must contain a prefixes array")
    return _sort_networks(snapshot["prefixes"])


def compare_prefixes(
    current: Sequence[str], previous: Sequence[str]
) -> tuple[list[str], list[str]]:
    return _sort_networks(set(current) - set(previous)), _sort_networks(
        set(previous) - set(current)
    )


def build_actions(added: Sequence[str], removed: Sequence[str], date: str) -> dict[str, Any]:
    return {
        "dateUpdated": date,
        "actions": {"remove": list(removed), "add": list(added)},
    }


def build_snapshot(prefixes: Sequence[str], source_filename: str) -> dict[str, Any]:
    match = DOWNLOAD_PATTERN.fullmatch(source_filename)
    if match is None:
        raise ServiceTagsError(
            f"Source filename does not match ServiceTags_Public_YYYYMMDD.json: "
            f"{source_filename}"
        )
    source_date = datetime.strptime(match.group("date"), "%Y%m%d").date().isoformat()
    return {
        "serviceTag": SERVICE_TAG_NAME,
        "sourceFile": source_filename,
        "sourceDate": source_date,
        "prefixes": list(prefixes),
    }


def build_summary(added: Sequence[str], removed: Sequence[str]) -> str:
    def section(title: str, values: Sequence[str]) -> list[str]:
        lines = [f"## {title} ({len(values)})", ""]
        lines.extend(f"- `{value}`" for value in values)
        if not values:
            lines.append("_None_")
        return lines

    lines = [
        "# PowerBI.CanadaCentral IP allow-list changes",
        "",
        *section("Added", added),
        "",
        *section("Removed", removed),
        "",
    ]
    return "\n".join(lines)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    temporary.replace(path)


def write_json(path: Path, document: Any) -> None:
    content = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    _write_bytes(path, content)


def write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


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


def process_file(
    source: Path,
    snapshot_path: Path,
    actions_path: Path,
    summary_path: Path,
    updated_date: str | None = None,
    action_baseline_path: Path | None = None,
) -> dict[str, str]:
    current = extract_prefixes(load_json(source))
    previous = load_snapshot(snapshot_path)
    observed_added, observed_removed = compare_prefixes(current, previous)

    action_baseline = (
        load_snapshot(action_baseline_path) if action_baseline_path else previous
    )
    added, removed = compare_prefixes(current, action_baseline)

    # Publish only when the live data moved since the last proposal *and* that
    # leaves a non-empty delta against the merged baseline. A publication that
    # reverts an unmerged proposal satisfies the first test but not the second,
    # and must not raise an empty pull request or SNOW entry.
    changed = bool((observed_added or observed_removed) and (added or removed))

    write_text(summary_path, build_summary(added, removed))
    if changed:
        date = updated_date or datetime.now(timezone.utc).date().isoformat()
        write_json(actions_path, build_actions(added, removed, date))
        write_json(snapshot_path, build_snapshot(current, source.name))

    return {
        "changed": str(changed).lower(),
        "added_count": str(len(added)),
        "removed_count": str(len(removed)),
        "summary_path": str(summary_path),
    }


def validate_actions_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("dateUpdated"), str):
        raise ServiceTagsError("SNOW payload must contain a string dateUpdated")
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        raise ServiceTagsError("SNOW payload must contain an actions object")
    for name in ("add", "remove"):
        values = actions.get(name)
        if not isinstance(values, list):
            raise ServiceTagsError(f"SNOW payload actions.{name} must be an array")
        _sort_networks(values)


def create_snow_entry(payload: Any) -> dict[str, Any]:
    """Mock the future SNOW API boundary while preserving its payload contract."""
    validate_actions_payload(payload)
    return {"status": "success", "mock": True}


def _github_output_argument(value: str | None) -> Path | None:
    return Path(value) if value else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="Download the latest Service Tags")
    download.add_argument("--download-dir", type=Path, required=True)
    download.add_argument("--download-page", default=DOWNLOAD_PAGE_URL)
    download.add_argument("--github-output")

    process = subparsers.add_parser("process", help="Build the IP allow-list delta")
    process.add_argument("--source", type=Path, required=True)
    process.add_argument("--snapshot", type=Path, required=True)
    process.add_argument("--actions", type=Path, required=True)
    process.add_argument("--summary", type=Path, required=True)
    process.add_argument("--updated-date")
    process.add_argument("--action-baseline", type=Path)
    process.add_argument("--github-output")

    snow = subparsers.add_parser("create-snow-entry", help="Call the mocked SNOW API")
    snow.add_argument("--actions", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "download":
            downloaded = download_service_tags(args.download_dir, args.download_page)
            date_match = DOWNLOAD_PATTERN.fullmatch(downloaded.name)
            if date_match is None:
                raise ServiceTagsError(
                    f"Downloaded file has an unexpected name: {downloaded.name}"
                )
            write_github_outputs(
                _github_output_argument(args.github_output),
                {
                    "download_path": str(downloaded),
                    "source_filename": downloaded.name,
                    "source_date": date_match.group("date"),
                },
            )
            print(downloaded)
        elif args.command == "process":
            outputs = process_file(
                args.source,
                args.snapshot,
                args.actions,
                args.summary,
                args.updated_date,
                args.action_baseline,
            )
            write_github_outputs(_github_output_argument(args.github_output), outputs)
            print(json.dumps(outputs))
        else:
            result = create_snow_entry(load_json(args.actions))
            print(json.dumps(result))
    except ServiceTagsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
