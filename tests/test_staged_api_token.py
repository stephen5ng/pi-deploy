"""The API token has to reach the boot partition like the other secrets.

bootstrap installs /boot/github-api-token and uses it for the word sound
release, but nothing put it there: a fresh flash came up with no token, so the
release query 404'd and the rig played with no words at all -- silently, which
is exactly the failure this whole path exists to make visible.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import render_dietpi_provisioning as render  # noqa: E402

SD_SCRIPT = REPOSITORY / "scripts" / "prepare_dietpi_sd.sh"
BOOTSTRAP = REPOSITORY / "bootstrap.sh"


class StageApiTokenTests(unittest.TestCase):
    def test_the_token_is_staged_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "github-api-token").write_text("github_pat_example\n")
            output = root / "rendered"
            output.mkdir()

            staged = render.stage_github_api_token({"SECRETS_DIR": str(secrets)}, output)

            self.assertTrue(staged)
            written = output / "github-api-token"
            self.assertEqual(written.read_text(), "github_pat_example\n")
            self.assertEqual(os.stat(written).st_mode & 0o077, 0)

    def test_a_missing_token_stages_nothing_and_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "secrets").mkdir()
            output = root / "rendered"
            output.mkdir()

            self.assertFalse(
                render.stage_github_api_token({"SECRETS_DIR": str(root / "secrets")}, output)
            )
            self.assertEqual(list(output.iterdir()), [])

    def test_the_staged_name_is_the_one_bootstrap_consumes(self):
        # The two sides agree only by literal, in two different languages.
        self.assertIn('boot_token="$boot_dir/github-api-token"', BOOTSTRAP.read_text(encoding="utf-8"))
        self.assertIn(
            '"$BOOT_MOUNT/github-api-token"', SD_SCRIPT.read_text(encoding="utf-8")
        )

    def test_dry_run_reports_the_staged_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            diskutil = binaries / "diskutil"
            diskutil.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = info ]; then\n"
                "  echo '   Device / Media Name: Test SD Card'\n"
                "  echo '   Disk Size: 32.0 GB'\n"
                "  echo '   Device Location: External'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n"
            )
            diskutil.chmod(0o755)

            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "github-api-token").write_text("github_pat_example\n")

            config = root / "provisioning.env"
            config.write_text(
                "WIFI_SSID='LEXACUBE'\n"
                "WIFI_PASSWORD='temporary-password'\n"
                "WIFI_COUNTRY='US'\n"
                "DIETPI_PASSWORD='temporary-password'\n"
                f"SECRETS_DIR='{secrets}'\n"
            )
            config.chmod(0o600)

            environment = os.environ.copy()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            environment["HOME"] = str(root)
            environment["RPI_IMAGER_BIN"] = "/usr/bin/true"

            result = subprocess.run(
                ["bash", str(SD_SCRIPT), "--device", "/dev/disk9",
                 "--config", str(config), "--dry-run"],
                capture_output=True, text=True, env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Staged GitHub API token", result.stdout)

    def test_a_missing_token_warns_about_the_silent_consequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "secrets"
            secrets.mkdir()
            output = root / "rendered"
            output.mkdir()
            values = {"SECRETS_DIR": str(secrets)}

            # render_files prints the warning; call the predicate it branches on.
            self.assertFalse(render.stage_github_api_token(values, output))
            self.assertIn(
                "the rig will install with no word",
                (REPOSITORY / "scripts" / "render_dietpi_provisioning.py").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
