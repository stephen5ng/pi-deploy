import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "prepare_dietpi_sd.sh"


class PrepareDietPiSdTests(unittest.TestCase):
    def make_fixture(self, root: Path, internal: bool = False) -> tuple[Path, dict]:
        binary_directory = root / "bin"
        binary_directory.mkdir()
        diskutil = binary_directory / "diskutil"
        location = "Internal: Yes" if internal else "Device Location: External"
        diskutil.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = info ]; then\n"
            "  echo '   Device / Media Name: Test SD Card'\n"
            "  echo '   Disk Size: 32.0 GB'\n"
            f"  echo '   {location}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        diskutil.chmod(0o755)

        public_key = root / "id_ed25519.pub"
        public_key.write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest lexacube\n",
            encoding="utf-8",
        )
        config = root / "provisioning.env"
        config.write_text(
            "WIFI_SSID='LEXACUBE'\n"
            "WIFI_PASSWORD='temporary-password'\n"
            "WIFI_COUNTRY='US'\n"
            "DIETPI_PASSWORD='temporary-password'\n"
            f"SSH_PUBLIC_KEY_FILE='{public_key}'\n",
            encoding="utf-8",
        )
        config.chmod(0o600)

        environment = os.environ.copy()
        environment["PATH"] = f"{binary_directory}:{environment['PATH']}"
        environment["HOME"] = str(root)
        environment["RPI_IMAGER_BIN"] = "/usr/bin/true"
        return config, environment

    def test_dry_run_renders_without_erasing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, environment = self.make_fixture(root)

            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--device",
                    "/dev/disk9",
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                cwd=REPOSITORY,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY RUN", result.stdout)
            self.assertIn("Test SD Card", result.stdout)

    def test_refuses_internal_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, environment = self.make_fixture(root, internal=True)

            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--device",
                    "/dev/disk9",
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                cwd=REPOSITORY,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to erase an internal disk", result.stderr)

    def test_refuses_primary_system_disk_before_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, environment = self.make_fixture(root)

            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--device",
                    "/dev/disk0",
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                cwd=REPOSITORY,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("primary system disk", result.stderr)


if __name__ == "__main__":
    unittest.main()
