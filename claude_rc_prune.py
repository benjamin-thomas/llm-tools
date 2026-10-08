#!/usr/bin/env python3
"""Delete Claude Code remote sessions not touched in the last 24 hours.

Dry run by default; pass --delete to actually delete.

Uses the undocumented sessions API that claude.ai/code and `claude --teleport`
talk to, authenticated with Claude Code's own OAuth token: it may break
without notice. If it answers 401, run `claude` once to refresh the token.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.anthropic.com/v1/sessions"


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def last_activity(session):
    return parse_time(session.get("updated_at") or session["created_at"])


def verdict(session, now, max_age):
    # connection_status is ignored: a session killed without a clean exit
    # stays "connected" forever.
    return "keep" if now - last_activity(session) <= max_age else "DELETE"


def auth_headers():
    home = Path.home()
    creds = json.loads((home / ".claude/.credentials.json").read_text())
    account = json.loads((home / ".claude.json").read_text())["oauthAccount"]
    return {
        "Authorization": f"Bearer {creds['claudeAiOauth']['accessToken']}",
        "x-organization-uuid": account["organizationUuid"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "ccr-byoc-2025-07-29",
        "content-type": "application/json",
    }


def request(method, url, headers):
    data = b"{}" if method == "DELETE" else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read().decode(errors='replace')}")
    return json.loads(body or b"{}")


def list_sessions(headers):
    sessions, after = [], None
    while True:
        page = request("GET", API + (f"?after_id={after}" if after else ""), headers)
        sessions += page["data"]
        if not (page.get("has_more") and page.get("last_id")):
            return sessions
        after = page["last_id"]


def describe(s):
    return "  ".join(
        [
            last_activity(s).astimezone().strftime("%Y-%m-%d %H:%M"),
            str(s.get("connection_status", "?")),
            s["id"],
            str(s.get("title", "")),
        ]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="really delete")
    parser.add_argument("--hours", type=float, default=24, help="max idle age")
    args = parser.parse_args()

    headers = auth_headers()
    sessions = list_sessions(headers)
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=args.hours)
    verdicts = [(verdict(s, now, max_age), s) for s in sessions]
    stale = [s for v, s in verdicts if v == "DELETE"]

    print(f"{len(sessions)} sessions, {len(stale)} idle for over {args.hours:g}h:")
    for v, s in verdicts:
        print(f"  {v:<8}{describe(s)}")

    if not args.delete:
        if stale:
            print("\nDry run. Pass --delete to delete them.")
        return

    for s in stale:
        request("DELETE", f"{API}/{s['id']}", headers)
        print(f"deleted {s['id']}")


if __name__ == "__main__":
    main()
