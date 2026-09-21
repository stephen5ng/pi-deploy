"""A deploy may install what an app asks for. It may not uninstall the rest.

`uv sync` makes the environment match the lock EXACTLY, so a lock that is
merely stale deletes everything outside it. Measured on the rig, when
lexacube's lock still named three packages against the seventeen in its
requirements.txt:

    Resolved 3 packages in 4ms
    Uninstalled 27 packages in 114ms

That took pygame-ce, aiomqtt and numpy off the machine and left the game unable
to import. It scrolled past in a log and bootstrap still reported success --
which is the part that makes this worth a test rather than a comment.

The flags are read out of bootstrap.sh and handed to a real `uv`, so this
exercises the invocation the deploy actually makes. Asserting on the flag
string alone would pass against a uv that had renamed it.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
SOURCE = BOOTSTRAP.read_text(encoding="utf-8")

# Every `uv sync ...` the deploy runs, with its flags.
INVOCATIONS = re.findall(r"uv sync ([^)\n]*)", SOURCE)

UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


class FlagTests(unittest.TestCase):
    def test_bootstrap_syncs_at_least_once(self):
        # Guards the tests below against a silently empty match.
        self.assertTrue(INVOCATIONS, "no `uv sync` found in bootstrap.sh")

    def test_every_sync_refuses_to_uninstall(self):
        for flags in INVOCATIONS:
            with self.subTest(flags=flags):
                self.assertIn("--inexact", flags)


@unittest.skipUnless(Path(UV).exists(), "uv not installed")
class BehaviourTests(unittest.TestCase):
    """Run the deploy's own flags against a real uv and a real environment."""

    def sync_with(self, flags):
        """Returns True if a package the lock does not name survived."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        # `package = false` and no dependencies keeps this offline: uv has
        # nothing to resolve, build or download, so the test cannot depend on
        # a network or an index being reachable.
        (tmp / "pyproject.toml").write_text(
            '[project]\nname = "proto"\nversion = "0.1.0"\ndependencies = []\n'
            "\n[tool.uv]\npackage = false\n"
        )
        env_dir = tmp / "env"
        subprocess.run([UV, "venv", "--quiet", str(env_dir)], check=True,
                       capture_output=True)
        site = next(env_dir.glob("lib/python*/site-packages"))

        # Stands in for pygame-ce: installed, and named nowhere in the project.
        dist = site / "extraneous-1.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: extraneous\nVersion: 1.0\n"
        )
        (dist / "RECORD").write_text("extraneous-1.0.dist-info/METADATA,,\n")
        (dist / "INSTALLER").write_text("pip\n")

        subprocess.run(
            [UV, "sync", *flags.split(), "--offline"],
            cwd=tmp, capture_output=True, text=True,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(env_dir)},
        )
        return (dist / "METADATA").exists()

    def test_the_deploys_own_flags_keep_an_unlisted_package(self):
        for flags in INVOCATIONS:
            with self.subTest(flags=flags):
                self.assertTrue(
                    self.sync_with(flags),
                    "uv sync removed a package the project never named",
                )

    def test_without_the_flag_the_package_is_destroyed(self):
        # The control: proves the test above can fail, and that this is the
        # behaviour the deploy had before --inexact.
        survived = self.sync_with("--all-extras")
        self.assertFalse(survived, "expected a plain `uv sync` to uninstall it")


if __name__ == "__main__":
    unittest.main()
