#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "email_password",
    "authorization",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redact sensitive fields inside JSON files.")
    parser.add_argument("paths", nargs="*", default=["data"], help="Files or directories to scan.")
    parser.add_argument("--dry-run", action="store_true", help="Report files that would change without writing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    changed: list[Path] = []
    for root in args.paths:
        for path in iter_json_paths(Path(root)):
            if redact_file(path, dry_run=args.dry_run):
                changed.append(path)
    action = "Would redact" if args.dry_run else "Redacted"
    print(f"{action} {len(changed)} JSON file(s).")
    for path in changed:
        print(path)


def iter_json_paths(path: Path):
    if path.is_file() and path.suffix.lower() == ".json":
        yield path
    elif path.is_dir():
        yield from path.rglob("*.json")


def redact_file(path: Path, *, dry_run: bool) -> bool:
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    redacted, changed = redact_value(original)
    if changed and not dry_run:
        path.write_text(json.dumps(redacted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def redact_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        output: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                output[key] = "REDACTED"
                changed = changed or item != "REDACTED"
            else:
                redacted_item, item_changed = redact_value(item)
                output[key] = redacted_item
                changed = changed or item_changed
        return output, changed
    if isinstance(value, list):
        output = []
        changed = False
        for item in value:
            redacted_item, item_changed = redact_value(item)
            output.append(redacted_item)
            changed = changed or item_changed
        return output, changed
    if isinstance(value, str):
        redacted = redact_url_string(value)
        return redacted, redacted != value
    return value, False


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS


def redact_url_string(value: str) -> str:
    split = urlsplit(value)
    if not split.query:
        return value
    changed = False
    query = []
    for key, item in parse_qsl(split.query, keep_blank_values=True):
        if is_sensitive_key(key):
            query.append((key, "REDACTED"))
            changed = changed or item != "REDACTED"
        else:
            query.append((key, item))
    if not changed:
        return value
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query, doseq=True), split.fragment))


if __name__ == "__main__":
    main()
