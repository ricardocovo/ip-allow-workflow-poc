#!/usr/bin/env python3
"""Process Azure Service Tags into an IP allow-list delta."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


SERVICE_TAG_NAME = "PowerBI.CanadaCentral"
DOWNLOAD_PATTERN = re.compile(
    r"ServiceTags_Public_(?P<date>\d{8})\.json", re.IGNORECASE
)


class ServiceTagsError(RuntimeError):
    """Raised when Service Tags data cannot be safely processed."""


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


def source_date(source_filename: str) -> str:
    match = DOWNLOAD_PATTERN.fullmatch(source_filename)
    if match is None:
        raise ServiceTagsError(
            f"Source filename does not match ServiceTags_Public_YYYYMMDD.json: "
            f"{source_filename}"
        )
    return datetime.strptime(match.group("date"), "%Y%m%d").date().isoformat()


def build_snapshot(prefixes: Sequence[str], source_filename: str) -> dict[str, Any]:
    return {
        "serviceTag": SERVICE_TAG_NAME,
        "sourceFile": source_filename,
        "sourceDate": source_date(source_filename),
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
    _write_bytes(path, (json.dumps(document, indent=2) + "\n").encode("utf-8"))


def write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


def copy_file(source: Path, destination: Path) -> None:
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise ServiceTagsError(f"Unable to read {source}: {exc}") from exc
    _write_bytes(destination, content)


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
    action_baseline_path: Path | None = None,
) -> dict[str, str]:
    current = extract_prefixes(load_json(source))
    previous = load_snapshot(snapshot_path)
    observed_added, observed_removed = compare_prefixes(current, previous)

    action_baseline = (
        load_snapshot(action_baseline_path) if action_baseline_path else previous
    )
    added, removed = compare_prefixes(current, action_baseline)

    has_delta = bool(added or removed)
    changed = bool((observed_added or observed_removed) and has_delta)

    write_text(summary_path, build_summary(added, removed))
    if has_delta:
        write_json(actions_path, build_actions(added, removed, source_date(source.name)))
        write_json(snapshot_path, build_snapshot(current, source.name))
    elif action_baseline_path is not None:
        copy_file(action_baseline_path, snapshot_path)

    return {
        "changed": str(changed).lower(),
        "has_delta": str(has_delta).lower(),
        "added_count": str(len(added)),
        "removed_count": str(len(removed)),
        "summary_path": str(summary_path),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--action-baseline", type=Path)
    parser.add_argument("--github-output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        outputs = process_file(
            args.source,
            args.snapshot,
            args.actions,
            args.summary,
            args.action_baseline,
        )
        write_github_outputs(
            Path(args.github_output) if args.github_output else None, outputs
        )
        print(json.dumps(outputs))
    except ServiceTagsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
