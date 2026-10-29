#!/usr/bin/env python3
"""Reclaim old Morpheus task worktrees only when Morpheus proves removal safe."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


HOME = Path.home()
TASK_BRANCH = re.compile(r"^([a-z]{2,4}-\d{2}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2})-[a-z0-9-]+$")
STATE = HOME / ".local/state/morpheus-factory/worktree-attempts.json"
DAY = 86400


def repository_roots(code_root):
    parents = [code_root, HOME / ".openclaw/workspace", HOME]
    seen = set()
    for parent in parents:
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_symlink() or not child.is_dir() or not (child / ".git").is_dir():
                continue
            canonical = child.resolve()
            if canonical not in seen:
                seen.add(canonical)
                yield canonical


def parse_worktrees(output):
    for record in output.split("\0\0"):
        if not record:
            continue
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if "worktree" in fields:
            yield fields


def candidates(repo, min_age_days):
    output = subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"])
    cutoff = time.time() - min_age_days * DAY
    for item in parse_worktrees(output.decode()):
        path = Path(item["worktree"])
        branch = item.get("branch", "").removeprefix("refs/heads/")
        match = TASK_BRANCH.fullmatch(branch)
        if not match or not ({".morpheus-worktrees", "morpheus-worktrees"} & set(path.parts)):
            continue
        if path.is_symlink() or not path.is_dir() or path.stat().st_mtime >= cutoff:
            continue
        yield path, match.group(1).upper(), item.get("HEAD", "")


def free_gib():
    stat = os.statvfs(HOME)
    return stat.f_bavail * stat.f_frsize / 1024 ** 3


def load_attempts():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_attempts(attempts):
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(attempts, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(STATE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--code-root", type=Path, default=HOME / "code")
    parser.add_argument("--min-age-days", type=int, default=14)
    parser.add_argument("--target-free-gb", type=float, default=0)
    parser.add_argument("--max-attempts", type=int, default=10)
    args = parser.parse_args()
    if args.min_age_days < 7 or args.max_attempts < 1:
        parser.error("retention and attempt limit must be positive and conservative")
    if not shutil.which("morpheus"):
        print("Morpheus CLI unavailable; skipping task worktree deletion", flush=True)
        return 0
    attempts = load_attempts() if args.apply else {}
    used = 0
    for repo in repository_roots(args.code_root):
        if args.target_free_gb and free_gib() >= args.target_free_gb:
            break
        prune = ["git", "-C", str(repo), "worktree", "prune", "--verbose", "--expire", "now"]
        if not args.apply:
            prune.append("--dry-run")
        pruned = subprocess.run(prune, capture_output=True, text=True)
        if pruned.returncode:
            print(f"Cannot prune stale registrations in {repo}: {pruned.stderr.strip()}", flush=True)
            continue
        if pruned.stdout.strip():
            print(f"Pruned stale Git registrations in {repo}: {len(pruned.stdout.splitlines())}", flush=True)
        try:
            old = sorted(candidates(repo, args.min_age_days), key=lambda item: item[0].stat().st_mtime)
        except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
            print(f"Cannot inventory worktrees in {repo}: {error}", flush=True)
            continue
        for path, task_id, head in old:
            if args.target_free_gb and free_gib() >= args.target_free_gb:
                break
            key = str(path)
            previous = attempts.get(key, {})
            if args.apply and previous.get("head") == head and time.time() - previous.get("time", 0) < DAY:
                continue
            if used >= args.max_attempts:
                break
            used += 1
            try:
                probe = subprocess.run(["morpheus", "pm", "cleanup", task_id, "--dry-run"],
                                       cwd=repo, capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                print(f"Retain {path}: cleanup proof timed out", flush=True)
                if args.apply:
                    attempts[key] = {"head": head, "time": time.time()}
                continue
            if probe.returncode:
                reason = (probe.stderr or probe.stdout).strip().splitlines()
                print(f"Retain {path}: {reason[0] if reason else 'cleanup proof refused'}", flush=True)
                if args.apply:
                    attempts[key] = {"head": head, "time": time.time()}
                continue
            if not probe.stdout.startswith(f"Safe to remove: {path} ("):
                print(f"Retain {path}: cleanup proof named a different worktree", flush=True)
                if args.apply:
                    attempts[key] = {"head": head, "time": time.time()}
                continue
            if args.apply:
                try:
                    result = subprocess.run(["morpheus", "pm", "cleanup", task_id], cwd=repo,
                                            capture_output=True, text=True, timeout=90)
                except subprocess.TimeoutExpired:
                    print(f"Retain {path}: cleanup recheck timed out", flush=True)
                    attempts[key] = {"head": head, "time": time.time()}
                    continue
                if result.returncode:
                    reason = (result.stderr or result.stdout).strip().splitlines()
                    print(f"Retain {path}: {reason[0] if reason else 'cleanup recheck refused'}", flush=True)
                    attempts[key] = {"head": head, "time": time.time()}
                else:
                    print(f"Removed proved-complete task worktree {path}", flush=True)
                    attempts.pop(key, None)
            else:
                print(f"Would remove proved-complete task worktree {path}", flush=True)
        if used >= args.max_attempts:
            break
    if args.apply:
        save_attempts(attempts)
    print(f"Worktree cleanup checked {used} old task candidates", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
