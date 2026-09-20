"""Per-rig `<app>.env` secrets must reach the boot partition unattended.

PROVISIONING.md told the operator to stage these files, but nothing read them:
a fresh flash came up with no /etc/knockstrip.env (the service will not start)
and no /etc/lexacube.env (the game plays and silently records nothing). These
tests pin the path from the secrets directory on the Mac to the rendered
first-boot files, and the copy from there onto /boot.
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


class StageAppEnvFilesTests(unittest.TestCase):
    def test_env_files_are_copied_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "knockstrip.env").write_text("PUSHER_SECRET=abc\n")
            (secrets / "lexacube.env").write_text("ANALYTICS_PRODUCT=lexacube\n")
            (secrets / "notes.txt").write_text("not a secret\n")
            output = root / "rendered"
            output.mkdir()

            staged = render.stage_app_env_files(
                {"SECRETS_DIR": str(secrets)}, output
            )

            self.assertEqual(staged, ["knockstrip.env", "lexacube.env"])
            self.assertFalse((output / "notes.txt").exists())
            copied = output / "knockstrip.env"
            self.assertEqual(copied.read_text(), "PUSHER_SECRET=abc\n")
            self.assertEqual(os.stat(copied).st_mode & 0o077, 0)

    def test_missing_directory_warns_and_stages_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "rendered"
            output.mkdir()

            staged = render.stage_app_env_files(
                {"SECRETS_DIR": str(root / "absent")}, output
            )

            self.assertEqual(staged, [])
            self.assertEqual(list(output.iterdir()), [])

    def test_default_directory_is_used_when_unset(self):
        self.assertEqual(
            render.secrets_directory({}),
            Path("~/.lexacube-secrets").expanduser(),
        )


class BootPartitionCopyTests(unittest.TestCase):
    def test_every_rendered_env_file_is_written_to_boot(self):
        source = SD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('for STAGED_ENV in "$WORK_DIRECTORY"/rendered/*.env; do', source)
        self.assertIn('cp "$STAGED_ENV" "$BOOT_MOUNT/$(basename "$STAGED_ENV")"', source)

    def test_dry_run_reports_staged_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_directory = root / "bin"
            binary_directory.mkdir()
            diskutil = binary_directory / "diskutil"
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
            (secrets / "knockstrip.env").write_text("PUSHER_SECRET=abc\n")

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
            environment["PATH"] = f"{binary_directory}:{environment['PATH']}"
            environment["HOME"] = str(root)
            environment["RPI_IMAGER_BIN"] = "/usr/bin/true"

            result = subprocess.run(
                [
                    "bash",
                    str(SD_SCRIPT),
                    "--device",
                    "/dev/disk9",
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Staged per-rig secrets: knockstrip.env", result.stdout)


if __name__ == "__main__":
    unittest.main()
