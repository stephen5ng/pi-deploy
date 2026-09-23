"""The Pi must be findable, and its first boot watchable, before the long part.

A fresh flash's first boot was killed during the cubes clone. The mDNS and
DHCP-preference steps ran at the very end of bootstrap, so neither landed; the
Pi sat on the house router's lease, absent from the rig router's client list,
with no name to look it up by. The only record was a DietPi log in the RAM-
backed /var/log that stopped mid-line.
"""

import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOOTSTRAP.read_text(encoding="utf-8")

    def position(self, needle):
        at = self.source.find(needle)
        self.assertNotEqual(at, -1, f"bootstrap must contain {needle!r}")
        return at

    def test_reachability_comes_before_the_long_part_of_the_install(self):
        # The point of the change: findable and watchable while the clones and
        # builds run, and configured even if they die.
        long_part = min(
            self.position('echo "$apt_packages" | xargs apt-get install'),
            self.position('git_clone_or_update "$dep_repo"'),
        )
        for step in ("dhcp-preference.sh", "mdns.sh"):
            self.assertLess(
                self.position(f'bash "$SCRIPT_DIR/scripts/{step}"'), long_part, step
            )

    def test_the_log_captures_the_first_thing_that_can_fail(self):
        self.assertLess(
            self.position('exec > >(tee -a "$BOOTSTRAP_LOG") 2>&1'),
            self.position("apt-get update"),
        )

    def test_the_log_lives_off_the_ramlog(self):
        # /var/log is a tmpfs on DietPi; a log there dies with the boot it
        # was meant to explain.
        self.assertIn(
            "BOOTSTRAP_LOG=${PI_DEPLOY_LOG:-/var/lib/pi-deploy/bootstrap.log}",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
