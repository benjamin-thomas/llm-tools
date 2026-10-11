#!/usr/bin/python3
"""The native messaging host behind the Chrome extension.

Mistral serves the Vibe budget only to a logged-in browser behind Cloudflare,
so the extension (quota-gauges-chrome/) asks from inside Chrome and hands the
answer to this host, which Chrome starts for each message. The host keeps it
where token-recap and quota-gauges read it. The session cookie never leaves
Chrome: only the budget crosses over.

Chrome's framing: a 4-byte native-order length (little-endian here), then that
many bytes of JSON, in both directions.

Chrome runs it outside any sandbox, so it is never run from the repo:
`quota-gauges install` copies this one file to ~/.local/lib/quota-gauges/,
which no sandbox may write. Hence the standard library only, and no import
from this repo — the copy has to stand alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

# Where token-recap and quota-gauges read it (token_recap.mistral_usage).
READING = Path.home() / ".cache" / "quota-gauges" / "mistral.json"
HOST_NAME = "dev.benjamin.quota_gauges"
# A budget is a few hundred bytes; anything near this is not one.
MAX_MESSAGE = 64 * 1024
_BUDGET_FIELDS = ("usage_percentage", "initial_budget", "currency", "reset_at", "payg_enabled")
_MAX_ERROR = 200


def read_message(stream: BinaryIO) -> Any:
    header = stream.read(4)
    if len(header) != 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE:
        return None
    try:
        return json.loads(stream.read(length))
    except (ValueError, UnicodeDecodeError):
        return None


def write_message(stream: BinaryIO, message: Any) -> None:
    body = json.dumps(message).encode()
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


def merge(kept: dict[str, Any] | None, message: Any) -> dict[str, Any]:
    """The reading to keep after `message`.

    A fresh budget replaces everything. A failure keeps the last good budget,
    with its own age, and records what went wrong — an expired session should
    leave the gauge saying how old its number is, not blank it.
    """
    kept = dict(kept or {})
    if not isinstance(message, dict):
        return kept
    stamp = str(message.get("fetched_at") or "")[:40]
    budget = message.get("budget")
    if isinstance(budget, dict):
        return {
            "budget": {k: budget[k] for k in _BUDGET_FIELDS if k in budget},
            "fetched_at": stamp,
        }
    error = message.get("error")
    if isinstance(error, str):
        kept["error"] = error[:_MAX_ERROR]
        kept["error_at"] = stamp
    return kept


def _write_atomically(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(blob))
    tmp.replace(path)  # a reader never sees a torn file


def serve(stdin: BinaryIO, stdout: BinaryIO, reading: Path = READING) -> None:
    message = read_message(stdin)
    try:
        kept: Any = json.loads(reading.read_text())
    except (OSError, ValueError):
        kept = None
    _write_atomically(reading, merge(kept if isinstance(kept, dict) else None, message))
    write_message(stdout, {"ok": True})


def extension_id(public_key_b64: str) -> str:
    """Chrome's id for an extension: the SHA-256 of its public key, first 16
    bytes, each hex digit spelled with a letter from a to p."""
    digest = hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def host_manifest(extension: str, host_path: Path) -> dict[str, Any]:
    return {
        "name": HOST_NAME,
        "description": "Hands the Mistral Vibe budget to quota-gauges and token-recap.",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension}/"],
    }


def main() -> int:
    serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
