#!/usr/bin/env python3
"""Install the reviewed Factory host-update runner as an hourly LaunchAgent."""

import argparse
import os
import plistlib
import re
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--credentials-project", type=Path, required=True)
    parser.add_argument("--github-user", default="cpheinrich")
    parser.add_argument("--provider-command", type=Path)
    parser.add_argument("--provider-url")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = root / "worker/updates.json"
    runner = root / "worker/update_runner.sh"
    target = Path.home() / "Library/LaunchAgents/com.morpheus-factory.host-updates.plist"
    if args.check:
        print(f"manifest: {'present' if manifest.is_file() else 'missing'}")
        print(f"credentials project: {'present' if args.credentials_project.is_dir() else 'missing'}")
        print(f"LaunchAgent: {'installed' if target.is_file() else 'missing'}")
        print(f"verification provider: {'configured' if args.provider_command and args.provider_url else 'missing'}")
        return
    if not manifest.is_file() or not args.credentials_project.is_dir():
        parser.error("manifest and credentials project must exist")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.github_user):
        parser.error("GitHub user must be an account login")
    if bool(args.provider_command) != bool(args.provider_url):
        parser.error("provider command and URL must be supplied together")
    if args.provider_command and (not args.provider_command.is_absolute() or not os.access(args.provider_command, os.X_OK)):
        parser.error("provider command must be an executable absolute path")
    if args.provider_url and not args.provider_url.startswith("https://"):
        parser.error("provider URL must use HTTPS")
    subprocess.run(["python3", str(root / "worker/update_controller.py"), "status", "--manifest", str(manifest), "--host", args.host], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["/bin/bash", str(runner), "--host", args.host, "--manifest", str(manifest), "--credentials-project", str(args.credentials_project.resolve()), "--github-user", args.github_user]
    if args.provider_command:
        arguments.extend(["--provider-command", str(args.provider_command), "--provider-url", args.provider_url])
    document = {
        "Label": "com.morpheus-factory.host-updates",
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "StartInterval": 3600,
        "EnvironmentVariables": {"PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": str(Path.home() / "Library/Logs/morpheus-factory/host-updates.log"),
        "StandardErrorPath": str(Path.home() / "Library/Logs/morpheus-factory/host-updates.log"),
    }
    Path(document["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(document))
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    subprocess.run(["plutil", "-lint", str(target)], check=True, capture_output=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{document['Label']}"], check=False, capture_output=True)
    for attempt in range(5):
        result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=False, capture_output=True)
        if result.returncode == 0:
            break
        if attempt == 4:
            raise RuntimeError("LaunchAgent bootstrap failed after five attempts")
        time.sleep(2)
    print(f"installed {document['Label']} for {args.host}")


if __name__ == "__main__":
    main()
