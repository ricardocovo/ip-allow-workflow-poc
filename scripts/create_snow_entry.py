#!/usr/bin/env python3
"""Create a mocked SNOW entry from IP allow-list actions."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any, Sequence


class SnowEntryError(RuntimeError):
    """Raised when an IP allow-list action payload is invalid."""


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except OSError as exc:
        raise SnowEntryError(f"Unable to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnowEntryError(f"Invalid JSON in {path}: {exc}") from exc


def _validate_prefixes(prefixes: Any, field_name: str) -> None:
    if not isinstance(prefixes, list):
        raise SnowEntryError(f"SNOW payload {field_name} must be an array")
    for prefix in prefixes:
        if not isinstance(prefix, str):
            raise SnowEntryError(f"SNOW payload {field_name} must contain strings")
        try:
            ipaddress.ip_network(prefix, strict=True)
        except ValueError as exc:
            raise SnowEntryError(f"Invalid CIDR prefix {prefix!r}: {exc}") from exc


def validate_actions_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("dateUpdated"), str):
        raise SnowEntryError("SNOW payload must contain a string dateUpdated")
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        raise SnowEntryError("SNOW payload must contain an actions object")
    for name in ("add", "remove"):
        _validate_prefixes(actions.get(name), f"actions.{name}")


def create_snow_entry(payload: Any) -> dict[str, Any]:
    """Mock the future SNOW API boundary while preserving its payload contract."""
    validate_actions_payload(payload)
    return {"status": "success", "mock": True}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = create_snow_entry(load_json(args.actions))
        print(json.dumps(result))
    except SnowEntryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
