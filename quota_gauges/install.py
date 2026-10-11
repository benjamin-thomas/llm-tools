"""Install what runs on its own, outside any sandbox, out of the repo.

Chrome starts the native host every minute, and GNOME Shell runs the panel
extension, which runs `quota-gauges --json` every minute: none of it may run
code a sandboxed agent can edit. The repo is writable from a sandbox; the
places installed to are not (sandbox-lib.rb refuses to mount them writable,
see host_run_paths). So they run a copy, and the copy changes only when this
runs — after the changes have been read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from quota_gauges.native_host import HOST_NAME, extension_id, host_manifest

DEST = Path.home() / ".local" / "lib" / "quota-gauges"
CHROME_HOSTS = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"
GNOME_EXTENSIONS = Path.home() / ".local" / "share" / "gnome-shell" / "extensions"
# Run from the copy, so it reads the copied packages, not the repo's.
_LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec /usr/bin/python3 -m quota_gauges "$@"
"""


def install(
    root: Path,
    dest: Path = DEST,
    hosts: Path = CHROME_HOSTS,
    extensions: Path = GNOME_EXTENSIONS,
) -> list[str]:
    """Copy the tools, the Chrome extension and its host, and the GNOME
    extension; point Chrome at the copy; and say file by file what changed:
    new, changed or unchanged."""
    extension = root / "quota-gauges-chrome"
    host = dest / "native-host"
    key = json.loads((extension / "manifest.json").read_text())["key"]
    manifest = json.dumps(host_manifest(extension_id(key), host), indent=2) + "\n"
    gnome = root / "quota-gauges-gnome"
    uuid = json.loads((gnome / "metadata.json").read_text())["uuid"]
    plan = [
        (dest / "chrome" / "manifest.json", (extension / "manifest.json").read_bytes(), 0o644),
        (dest / "chrome" / "background.js", (extension / "background.js").read_bytes(), 0o644),
        (host, (root / "quota_gauges" / "native_host.py").read_bytes(), 0o755),
        (hosts / f"{HOST_NAME}.json", manifest.encode(), 0o644),
        (dest / "quota-gauges", _LAUNCHER.encode(), 0o755),
    ]
    for package in ("token_recap", "quota_gauges"):
        for src in sorted((root / package).glob("*.py")):
            plan.append((dest / package / src.name, src.read_bytes(), 0o644))
    for src in sorted(p for p in gnome.iterdir() if p.is_file()):
        plan.append((extensions / uuid / src.name, src.read_bytes(), 0o644))
    report: list[str] = []
    for path, content, mode in plan:
        before = path.read_bytes() if path.exists() else None
        status = "new" if before is None else "unchanged" if before == content else "changed"
        if status != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}")
            tmp.write_bytes(content)
            tmp.chmod(mode)
            tmp.replace(path)
        shown = path.relative_to(dest) if path.is_relative_to(dest) else path
        report.append(f"{status:<9} {shown}")
    return report
