"""A rerun of bootstrap must recover from a clone that was interrupted.

A first boot was killed during the cubes clone and left /opt/lexacube as a
`.git` with HEAD at refs/heads/.invalid and nothing else. git_clone_or_update
saw `.git`, ran `pull`, and failed -- on that run and on every rerun, so the
documented recovery ("rerun bootstrap") could not recover.

The function is extracted from bootstrap.sh and run against real local
repositories, with GIT_* scrubbed so a run under a git hook cannot reach the
repository the suite lives in.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"

ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
ENV.update(
    GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@t",
)


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def function_source():
    source = BOOTSTRAP.read_text(encoding="utf-8")
    start = source.index("git_clone_or_update() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


class CloneRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        git("init", "-q", "-b", "master", cwd=self.upstream)
        (self.upstream / "runpygame.py").write_text("print('game')\n")
        git("add", ".", cwd=self.upstream)
        git("commit", "-q", "-m", "one", cwd=self.upstream)

        self.dest = self.tmp / "opt" / "lexacube"
        self.dest.parent.mkdir()

    def run_function(self, repo=None, env=ENV):
        script = "\n".join([
            "set -euo pipefail",
            "github_ssh_auth_works() { return 1; }",
            "GITHUB_KNOWN_HOSTS=/dev/null",
            "GITHUB_IDENTITY_FILE=",
            function_source(),
            f'git_clone_or_update "{repo or self.upstream}" "{self.dest}" master',
        ])
        return subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True
        )

    def interrupted_clone(self):
        # What `git clone` leaves when killed during the fetch, as found on
        # the Pi: a .git whose HEAD names a ref that does not exist.
        git("init", "-q", str(self.dest))
        # Written by hand: git refuses to name an invalid ref, clone does not.
        (self.dest / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n")

    def assert_cloned(self, done):
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.dest),
                         git("rev-parse", "HEAD", cwd=self.upstream))
        self.assertTrue((self.dest / "runpygame.py").exists())
        self.assertFalse(Path(f"{self.dest}.partial").exists())

    def test_a_fresh_clone_lands_at_the_destination(self):
        self.assert_cloned(self.run_function())

    def test_a_rerun_recovers_an_interrupted_clone(self):
        self.interrupted_clone()
        self.assert_cloned(self.run_function())

    def test_a_killed_clone_leaves_nothing_at_the_destination(self):
        # A clone that *fails* cleans up after itself; one that is killed --
        # power pulled, drive moved -- does not. Modelled by a git that starts
        # the clone the way the real one does and then dies by SIGKILL. The
        # next run must find no checkout and clone, not a checkout and pull.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == clone ]]; then\n'
            '    target="${@: -1}"\n'
            '    mkdir -p "$target/.git"\n'
            '    echo "ref: refs/heads/.invalid" > "$target/.git/HEAD"\n'
            "    kill -9 $$\n"
            "fi\n"
            f'exec {shutil.which("git")} "$@"\n'
        )
        (bin_dir / "git").chmod(0o755)
        killed = dict(ENV, PATH=f"{bin_dir}:{ENV['PATH']}")
        done = self.run_function(env=killed)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.dest.exists())

    def test_a_staging_directory_left_by_a_killed_run_is_discarded(self):
        leftover = Path(f"{self.dest}.partial")
        leftover.mkdir()
        (leftover / "half-written").write_text("x")
        self.assert_cloned(self.run_function())

    def test_an_empty_directory_is_cloned_into(self):
        self.dest.mkdir()
        self.assert_cloned(self.run_function())

    def test_a_non_empty_non_checkout_is_refused_before_cloning(self):
        self.dest.mkdir()
        (self.dest / "notes.txt").write_text("mine")
        done = self.run_function()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.dest / "notes.txt").read_text(), "mine")
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["notes.txt"])

    def test_a_broken_checkout_holding_other_files_is_not_deleted(self):
        self.interrupted_clone()
        (self.dest / "output").mkdir()
        (self.dest / "output" / "game.jsonl").write_text("{}")
        done = self.run_function()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("refusing to delete", done.stderr)
        self.assertTrue((self.dest / "output" / "game.jsonl").exists())

    def test_a_working_checkout_is_still_updated_in_place(self):
        self.assert_cloned(self.run_function())
        (self.dest / "local-only").write_text("keep")
        (self.upstream / "runpygame.py").write_text("print('two')\n")
        git("commit", "-q", "-am", "two", cwd=self.upstream)
        self.assert_cloned(self.run_function())
        self.assertEqual((self.dest / "local-only").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
