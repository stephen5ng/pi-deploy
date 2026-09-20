"""A fresh Pi must be able to clone private application repos unattended.

The lexacube rig came up with no GitHub credential at all: `cubes` and
`knockstrip` are private, so bootstrap stopped at the first clone. GitHub
refuses one deploy key on a second repository, so there is one key per repo,
each behind its own SSH host alias with git `insteadOf` rules pointing the
repository at that alias.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import render_dietpi_provisioning as render  # noqa: E402

BOOTSTRAP = REPOSITORY / "bootstrap.sh"
SD_SCRIPT = REPOSITORY / "scripts" / "prepare_dietpi_sd.sh"


def run_configure(root: Path) -> subprocess.CompletedProcess:
    """Run bootstrap's deploy-key setup against a fake root filesystem.

    The function is extracted and re-rooted rather than mocked, so the test
    exercises the shell that actually runs on the Pi.
    """
    source = BOOTSTRAP.read_text(encoding="utf-8")
    start = source.index("DEPLOY_KEY_DIR=")
    end = source.index("setup_ssh_for_root() {")
    functions = source[start:end].replace("/root/.ssh", f"{root}/root/.ssh")
    functions = functions.replace("$GITHUB_KNOWN_HOSTS", f"{root}/root/.ssh/github_known_hosts")
    functions = functions.replace("/boot/firmware", f"{root}/boot/firmware")
    functions = functions.replace("/boot", f"{root}/boot")

    script = textwrap.dedent(
        """
        set -euo pipefail
        export HOME={root}/root
        """
    ).format(root=root) + functions + "\nconfigure_github_deploy_keys\n"

    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True
    )


class ConfigureDeployKeysTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "boot").mkdir()
        (self.root / "root").mkdir()
        self.addCleanup(self.directory.cleanup)

    def stage(self, name: str, content: str = "PRIVATE KEY\n") -> Path:
        path = self.root / "boot" / f"github-deploy-key-{name}"
        path.write_text(content)
        return path

    def test_staged_key_is_installed_aliased_and_removed_from_boot(self):
        staged = self.stage("stephen5ng.nfc-control")

        result = run_configure(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

        installed = self.root / "root/.ssh/github-deploy-keys/stephen5ng.nfc-control"
        self.assertEqual(installed.read_text(), "PRIVATE KEY\n")
        self.assertEqual(os.stat(installed).st_mode & 0o077, 0)
        self.assertFalse(staged.exists(), "boot copy must not survive: FAT32 is world-readable")

        config = (self.root / "root/.ssh/config").read_text()
        self.assertIn("Host github-stephen5ng-nfc-control", config)
        self.assertIn(f"IdentityFile {installed}", config)
        self.assertIn("IdentitiesOnly yes", config)

    def test_the_alias_pins_githubs_host_key(self):
        """The rewritten clone gets no GIT_SSH_COMMAND, so the alias must carry
        the managed known_hosts itself.

        `github_ssh_auth_works` tests `git@github.com`, which a per-repo deploy
        key cannot authenticate, so `use_ssh` stays false and the clone keeps
        its HTTPS spelling -- reaching SSH only through `insteadOf`, outside
        git_clone_or_update's GIT_SSH_COMMAND. On a fresh Pi that meant host
        key verification failed on the first private clone.
        """
        self.stage("stephen5ng.cubes")
        self.assertEqual(run_configure(self.root).returncode, 0)
        config = self.root / "root/.ssh/config"

        effective = subprocess.run(
            ["ssh", "-G", "-F", str(config), "github-stephen5ng-cubes"],
            capture_output=True,
            text=True,
        ).stdout.lower()

        self.assertIn("hostname github.com", effective)
        # ssh -G normalises `yes` to `true`.
        self.assertIn("stricthostkeychecking true", effective)
        self.assertIn(
            f"userknownhostsfile {self.root}/root/.ssh/github_known_hosts".lower(),
            effective,
        )

    def test_the_known_hosts_file_is_written_before_the_aliases_use_it(self):
        source = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")

        self.assertLess(
            source.index("\nsetup_ssh_for_root\n"),
            source.index("\nconfigure_github_deploy_keys\n"),
        )

    def test_both_url_forms_are_rewritten_to_the_repository_alias(self):
        self.stage("stephen5ng.cubes")

        result = run_configure(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

        gitconfig = (self.root / "root/.gitconfig").read_text()
        # git_clone_or_update may clone the HTTPS URL from apps.yaml directly,
        # or rewrite it to git@github.com first; both must land on the alias.
        self.assertIn("insteadOf = https://github.com/stephen5ng/cubes", gitconfig)
        self.assertIn("insteadOf = git@github.com:stephen5ng/cubes", gitconfig)
        self.assertIn('[url "git@github-stephen5ng-cubes:stephen5ng/cubes"]', gitconfig)

    def test_two_repositories_get_separate_keys_and_aliases(self):
        self.stage("stephen5ng.cubes", "CUBES KEY\n")
        self.stage("stephen5ng.knockstrip", "KNOCKSTRIP KEY\n")

        result = run_configure(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

        keys = self.root / "root/.ssh/github-deploy-keys"
        self.assertEqual(
            (keys / "stephen5ng.cubes").read_text(), "CUBES KEY\n"
        )
        self.assertEqual(
            (keys / "stephen5ng.knockstrip").read_text(), "KNOCKSTRIP KEY\n"
        )
        config = (self.root / "root/.ssh/config").read_text()
        self.assertIn(f"IdentityFile {keys}/stephen5ng.cubes", config)
        self.assertIn(f"IdentityFile {keys}/stephen5ng.knockstrip", config)

    def test_rerun_after_the_boot_copy_is_gone_repairs_the_config(self):
        self.stage("stephen5ng.cubes")
        self.assertEqual(run_configure(self.root).returncode, 0)
        (self.root / "root/.ssh/config").unlink()

        result = run_configure(self.root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Host github-stephen5ng-cubes",
            (self.root / "root/.ssh/config").read_text(),
        )

    def test_an_alias_from_an_older_bootstrap_is_repaired(self):
        """The box this bug stranded is the one that already has a stanza.

        An earlier bootstrap wrote the alias without host-key options and its
        first private clone failed; if a rerun skipped the existing stanza the
        failure would survive every future run.
        """
        keys = self.root / "root/.ssh/github-deploy-keys"
        keys.mkdir(parents=True)
        (keys / "stephen5ng.cubes").write_text("KEY\n")
        config = self.root / "root/.ssh/config"
        config.write_text(
            "Host other-host\n"
            "    HostName example.invalid\n"
            "\n"
            "Host github-stephen5ng-cubes\n"
            "    HostName github.com\n"
            "    User git\n"
            f"    IdentityFile {keys}/stephen5ng.cubes\n"
            "    IdentitiesOnly yes\n"
            "\n"
        )

        result = run_configure(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

        effective = subprocess.run(
            ["ssh", "-G", "-F", str(config), "github-stephen5ng-cubes"],
            capture_output=True,
            text=True,
        ).stdout.lower()
        self.assertIn("stricthostkeychecking true", effective)
        self.assertIn(
            f"userknownhostsfile {self.root}/root/.ssh/github_known_hosts".lower(),
            effective,
        )
        text = config.read_text()
        self.assertEqual(text.count("Host github-stephen5ng-cubes"), 1)
        # An unrelated stanza must survive the rewrite.
        self.assertIn("Host other-host", text)
        self.assertIn("HostName example.invalid", text)

    def test_rerun_does_not_duplicate_the_alias_block(self):
        self.stage("stephen5ng.cubes")
        self.assertEqual(run_configure(self.root).returncode, 0)

        self.assertEqual(run_configure(self.root).returncode, 0)

        config = (self.root / "root/.ssh/config").read_text()
        self.assertEqual(config.count("Host github-stephen5ng-cubes"), 1)

    def test_an_installed_key_is_not_overwritten_by_a_staged_one(self):
        keys = self.root / "root/.ssh/github-deploy-keys"
        keys.mkdir(parents=True)
        (keys / "stephen5ng.cubes").write_text("INSTALLED\n")
        self.stage("stephen5ng.cubes", "STAGED\n")

        self.assertEqual(run_configure(self.root).returncode, 0)

        self.assertEqual((keys / "stephen5ng.cubes").read_text(), "INSTALLED\n")


class StageDeployKeysTests(unittest.TestCase):
    def test_keys_are_rendered_with_the_boot_filename_and_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = root / "secrets" / "github-deploy-keys"
            keys.mkdir(parents=True)
            (keys / "stephen5ng.cubes").write_text("KEY\n")
            (keys / "stephen5ng.cubes.pub").write_text("ssh-ed25519 AAAA\n")
            output = root / "rendered"
            output.mkdir()

            staged = render.stage_github_deploy_keys(
                {"SECRETS_DIR": str(root / "secrets")}, output
            )

            self.assertEqual(staged, ["stephen5ng.cubes"])
            written = output / "github-deploy-key-stephen5ng.cubes"
            self.assertEqual(written.read_text(), "KEY\n")
            self.assertEqual(os.stat(written).st_mode & 0o077, 0)
            self.assertFalse(
                (output / "github-deploy-key-stephen5ng.cubes.pub").exists(),
                "the public half has no business on the boot partition",
            )

    def test_missing_directory_stages_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "rendered"
            output.mkdir()

            self.assertEqual(
                render.stage_github_deploy_keys({"SECRETS_DIR": str(root)}, output), []
            )


class BootPartitionCopyTests(unittest.TestCase):
    def test_staged_keys_are_written_to_boot(self):
        source = SD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'for STAGED_KEY in "$WORK_DIRECTORY"/rendered/github-deploy-key-*; do',
            source,
        )
        self.assertIn(
            'cp "$STAGED_KEY" "$BOOT_MOUNT/$(basename "$STAGED_KEY")"', source
        )


if __name__ == "__main__":
    unittest.main()
