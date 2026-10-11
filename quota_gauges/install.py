"""Install the Mistral extension and its native host out of the repo.

Chrome runs the native host outside any sandbox, every minute, so it must not
run code a sandboxed agent can edit. The repo is writable from a sandbox;
~/.local/lib/quota-gauges is not (sandbox-lib.rb refuses to mount it
writable, see host_run_paths). So Chrome loads a copy, and the copy changes only when this runs —
after the changes have been read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from quota_gauges.native_host import HOST_NAME, extension_id, host_manifest

DEST = Path.home() / ".local" / "lib" / "quota-gauges"
CHROME_HOSTS = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"


def install(root: Path, dest: Path = DEST, hosts: Path = CHROME_HOSTS) -> list[str]:
    """Copy the extension and the host, point Chrome at the copy, and say
    file by file what changed: new, changed or unchanged."""
    extension = root / "quota-gauges-chrome"
    host = dest / "native-host"
    key = json.loads((extension / "manifest.json").read_text())["key"]
    manifest = json.dumps(host_manifest(extension_id(key), host), indent=2) + "\n"
    plan = [
        (dest / "chrome" / "manifest.json", (extension / "manifest.json").read_bytes(), 0o644),
        (dest / "chrome" / "background.js", (extension / "background.js").read_bytes(), 0o644),
        (host, (root / "quota_gauges" / "native_host.py").read_bytes(), 0o755),
        (hosts / f"{HOST_NAME}.json", manifest.encode(), 0o644),
    ]
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
