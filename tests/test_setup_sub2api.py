import hashlib
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup-sub2api.sh"
COMPOSE = "services:\n  sub2api:\n    image: weishaw/sub2api:latest\n"


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class SetupSub2apiTests(unittest.TestCase):
    """Runs a copy pinned to a local fixture; docker and curl are fakes, so nothing leaves the machine."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.deploy = self.root / "deploy"
        self.log = self.root / "log"
        self.log.write_text("")
        self.upstream = self.root / "upstream.yml"
        self.upstream.write_text(COMPOSE)
        bin_dir = self.root / "bin"
        executable(bin_dir / "docker", f'echo "docker $*" >> "{self.log}"\n')
        executable(bin_dir / "curl", f'''echo "curl $*" >> "{self.log}"
while [[ $# -gt 0 ]]; do
  if [[ $1 == -o ]]; then cp "{self.upstream}" "$2"; exit 0; fi
  shift
done
exit 1
''')
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.root)}
        self.script = self.root / "setup-sub2api.sh"
        self.pin(hashlib.sha256(COMPOSE.encode()).hexdigest())

    def pin(self, digest):
        source = SCRIPT.read_text()
        patched, count = re.subn(r'^COMPOSE_SHA256="[0-9a-f]{64}"$', f'COMPOSE_SHA256="{digest}"', source, flags=re.M)
        self.assertEqual(count, 1)
        self.script.write_text(patched)
        self.script.chmod(0o755)

    def run_setup(self, **extra):
        return subprocess.run(["bash", str(self.script), str(self.deploy)], env={**self.env, **extra},
                              capture_output=True, text=True)

    def env_value(self, name):
        for line in (self.deploy / ".env").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1]
        self.fail(f"{name} missing from .env")

    def test_fresh_install_never_prints_secrets_and_locks_env(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        for name in ("ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET", "TOTP_ENCRYPTION_KEY"):
            value = self.env_value(name)
            self.assertGreaterEqual(len(value), 32, name)
            self.assertNotIn(value, output, name)
        self.assertEqual(stat.S_IMODE((self.deploy / ".env").stat().st_mode), 0o600)
        self.assertIn("RUN_MODE=simple", (self.deploy / ".env").read_text())
        self.assertIn("image: weishaw/sub2api:0.2.19", (self.deploy / "docker-compose.pin.yml").read_text())
        self.assertIn("docker compose -f docker-compose.local.yml -f docker-compose.pin.yml up -d", self.log.read_text())

    def test_rerun_preserves_env_and_skips_download_when_pinned(self):
        self.assertEqual(self.run_setup().returncode, 0)
        before = (self.deploy / ".env").read_text()
        self.log.write_text("")
        result = self.run_setup(BIND_HOST="127.0.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.deploy / ".env").read_text(), before)
        self.assertNotIn("curl", self.log.read_text())
        self.assertIn("up -d", self.log.read_text())

    def test_checksum_mismatch_changes_nothing_and_does_not_start(self):
        installed = "services: {}\n"
        self.deploy.mkdir()
        (self.deploy / "docker-compose.local.yml").write_text(installed)
        self.pin("0" * 64)
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual((self.deploy / "docker-compose.local.yml").read_text(), installed)
        self.assertEqual(sorted(p.name for p in self.deploy.iterdir()), ["docker-compose.local.yml"])
        self.assertNotIn("up -d", self.log.read_text())

    def test_image_tag_must_be_a_release(self):
        result = self.run_setup(SUB2API_IMAGE_TAG="latest")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.deploy.exists())

    def test_committed_pin_matches_a_full_commit_and_digest(self):
        source = SCRIPT.read_text()
        self.assertRegex(source, r'(?m)^COMPOSE_REF="[0-9a-f]{40}"$')
        self.assertRegex(source, r'(?m)^COMPOSE_SHA256="[0-9a-f]{64}"$')
        self.assertNotIn("/main/deploy", source)


if __name__ == "__main__":
    unittest.main()
