import os
import shutil
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
                    # These exercise disk handling; freshness has its own tests
                    # and would otherwise fetch from GitHub on every run.
                    "--allow-stale",
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
                    # These exercise disk handling; freshness has its own tests
                    # and would otherwise fetch from GitHub on every run.
                    "--allow-stale",
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
                    # These exercise disk handling; freshness has its own tests
                    # and would otherwise fetch from GitHub on every run.
                    "--allow-stale",
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



GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(
    GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                   capture_output=True, text=True)


class CheckoutFreshnessTests(unittest.TestCase):
    """The card is built from the local checkout's scripts. One prepared from a
    checkout behind main shipped a recipe that predated deploy-key staging,
    and first boot hung on the private cubes clone."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git("init", "-q", "-b", "main", cwd=self.upstream)
        self.commit(self.upstream, "one")
        self.checkout = root / "checkout"
        git("clone", "-q", str(self.upstream), str(self.checkout), cwd=root)

    def commit(self, repo, name):
        (repo / name).write_text(name)
        git("add", name, cwd=repo)
        git("commit", "-q", "-m", name, cwd=repo)

    def check(self):
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("checkout_is_current() {")
        end = source.index("\n}\n", start) + 3
        script = "set -euo pipefail\n" + source[start:end] + (
            f'checkout_is_current "{self.checkout}"\n'
        )
        return subprocess.run(["bash", "-c", script], env=GIT_ENV,
                              capture_output=True, text=True)

    def test_a_current_checkout_passes(self):
        self.assertEqual(self.check().returncode, 0)

    def test_a_checkout_behind_main_is_refused(self):
        self.commit(self.upstream, "two")
        self.commit(self.upstream, "three")
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("2 commit(s) behind origin/main", done.stderr)
        self.assertIn("--allow-stale", done.stderr)

    def test_a_branch_ahead_of_main_passes(self):
        # How a branch gets tested on hardware before it merges.
        git("switch", "-q", "-c", "feature", cwd=self.checkout)
        self.commit(self.checkout, "feature-work")
        self.assertEqual(self.check().returncode, 0)

    def test_a_branch_cut_before_main_moved_is_refused(self):
        # Today's case: a side branch, never rebased, while main gained the
        # deploy-key staging.
        git("switch", "-q", "-c", "old-branch", cwd=self.checkout)
        self.commit(self.checkout, "side")
        self.commit(self.upstream, "deploy-keys")
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("(old-branch) is 1 commit(s) behind", done.stderr)

    def test_an_unreachable_remote_warns_rather_than_blocking(self):
        # Preparing a card offline is legitimate; the check just cannot say.
        shutil.rmtree(self.upstream)
        done = self.check()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("could not fetch", done.stderr)

    def test_the_check_runs_before_the_disk_is_touched(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(
            source.index('checkout_is_current "$REPOSITORY_DIR" || exit 1'),
            source.index('DISK_INFO=$(diskutil info "$DEVICE")'),
        )


class NoCredentialPromptTests(unittest.TestCase):
    def test_bootstrap_disables_git_prompts_before_any_clone(self):
        # With tty1 attached on first boot, git's username prompt hung the
        # private cubes clone indefinitely instead of failing.
        source = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertLess(
            source.index("\nexport GIT_TERMINAL_PROMPT=0\n"),
            source.index("\ngit_clone_or_update() {"),
        )

    def test_git_over_ssh_never_prompts_either(self):
        # An encrypted deploy key asks for a passphrase: the same hang on
        # tty1, by the SSH path instead of the HTTPS one.
        source = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")
        start = source.index("\ngit_clone_or_update() {")
        line = next(l for l in source[start:].splitlines()
                    if l.strip().startswith('local ssh_opts="'))
        self.assertIn("-o BatchMode=yes", line)


if __name__ == "__main__":
    unittest.main()
