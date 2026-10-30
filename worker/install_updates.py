#!/usr/bin/env python3
"""Install the reviewed Factory host-update runner as an hourly LaunchAgent."""

import argparse
import os
import plistlib
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--credentials-project", type=Path, required=True)
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
        return
    if not manifest.is_file() or not args.credentials_project.is_dir():
        parser.error("manifest and credentials project must exist")
    subprocess.run(["python3", str(root / "worker/update_controller.py"), "status", "--manifest", str(manifest), "--host", args.host], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "Label": "com.morpheus-factory.host-updates",
        "ProgramArguments": ["/bin/bash", str(runner), "--host", args.host, "--manifest", str(manifest), "--credentials-project", str(args.credentials_project.resolve())],
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
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{document['Label']}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"installed {document['Label']} for {args.host}")


if __name__ == "__main__":
    main()
