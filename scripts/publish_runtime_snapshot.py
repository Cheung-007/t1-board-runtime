#!/usr/bin/env python3
"""Publish a runtime export without overwriting a newer main snapshot."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_as_of(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], text=True, capture_output=True, check=check)


def remote_as_of() -> str:
    result = git("show", "origin/main:data/dashboard.json", check=False)
    if result.returncode != 0:
        raise RuntimeError("origin/main:data/dashboard.json is missing; refusing an unguarded publish")
    try:
        value = json.loads(result.stdout)["asOf"]
        parse_as_of(value)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("origin/main dashboard has no valid asOf; refusing an unguarded publish") from exc
    return value


def write_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")

    source_dashboard = args.snapshot_dir / "dashboard.json"
    try:
        expected = json.loads(source_dashboard.read_text(encoding="utf-8"))["asOf"]
        expected_time = parse_as_of(expected)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"snapshot dashboard has no valid asOf: {exc}") from exc

    git("config", "user.name", "t1-runtime-bot")
    git("config", "user.email", "t1-runtime-bot@users.noreply.github.com")
    for attempt in range(1, args.attempts + 1):
        git("fetch", "origin", "main")
        current = remote_as_of()
        current_time = parse_as_of(current)
        if current_time >= expected_time:
            print(json.dumps({"snapshotAsOf": expected, "publishedAsOf": current, "result": "already_current_or_newer", "attempt": attempt}))
            write_output("expected_as_of", expected)
            write_output("published_as_of", current)
            return 0

        git("reset", "--hard", "origin/main")
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        for existing in data_dir.glob("*.json"):
            existing.unlink()
        for payload in args.snapshot_dir.glob("*.json"):
            shutil.copy2(payload, data_dir / payload.name)
        git("add", "data")
        staged = git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            raise RuntimeError("snapshot is newer by asOf but produces no data diff")
        git("commit", "-m", f"data: refresh market snapshot {expected}")
        pushed = git("push", "origin", "HEAD:refs/heads/main", check=False)
        if pushed.returncode == 0:
            # Confirm the remote accepted this exact or a newer snapshot before reporting success.
            git("fetch", "origin", "main")
            current = remote_as_of()
            if parse_as_of(current) >= expected_time:
                print(json.dumps({"snapshotAsOf": expected, "publishedAsOf": current, "result": "published", "attempt": attempt}))
                write_output("expected_as_of", expected)
                write_output("published_as_of", current)
                return 0
        else:
            print(f"push attempt {attempt}/{args.attempts} lost a remote update race: {pushed.stderr.strip()}", file=sys.stderr)

    raise SystemExit(f"could not publish snapshot {expected} after {args.attempts} bounded attempts")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
