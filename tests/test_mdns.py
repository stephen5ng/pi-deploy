"""Publishing the Pi's hostname over mDNS.

The Pi's address moves: it is a DHCP client on a LAN with more than one DHCP
server, so a remembered address stops working with no clue where the box went.
avahi makes <hostname>.local resolve regardless of which server won the lease.

The script only shells out, so it is exercised for real against stubs. The
failure that matters is not "avahi is missing" -- it is bootstrap aborting
because avahi is missing: bootstrap.sh runs under `set -euo pipefail` and
calls each host-level step bare, so a non-zero exit here would skip
wifi-preference.sh, the step this one is deliberately ordered ahead of.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "mdns.sh"
BOOTSTRAP = REPOSITORY / "bootstrap.sh"


class Harness:
    def invoke(self, *, installed=False, apt_ok=True, systemctl_ok=True):
        """Run the script against stubbed apt/dpkg/systemctl.

        Returns the CompletedProcess plus the recorded command log, so a test
        can assert what was *not* run as easily as what was.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        log = tmp / "calls.log"

        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        def stub(name, body):
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\necho "{name} $@" >> {log}\n{body}\n')
            path.chmod(0o755)

        # dpkg-query's real output is what the script greps for.
        stub(
            "dpkg-query",
            'echo "install ok installed"\nexit 0' if installed else "exit 1",
        )
        stub("apt-get", "exit 0" if apt_ok else "exit 100")
        stub("systemctl", "exit 0" if systemctl_ok else "exit 1")

        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        done = subprocess.run(
            ["bash", "-s"], input=SCRIPT.read_text(), text=True,
            capture_output=True, env=env,
        )
        return done, log.read_text() if log.exists() else ""


class MdnsScriptTests(Harness, unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_a_fresh_box_installs_without_recommends(self):
        # avahi-daemon's recommends pull in extra packages nobody asked for on
        # a distro chosen for being minimal.
        _, calls = self.invoke(installed=False)
        self.assertIn("--no-install-recommends avahi-daemon", calls)

    def test_the_daemon_is_started_not_only_enabled(self):
        # An enabled-but-stopped daemon publishes nothing until the next boot.
        _, calls = self.invoke(installed=False)
        self.assertIn("systemctl enable --now avahi-daemon", calls)

    def test_a_box_that_already_has_it_does_not_touch_apt(self):
        # bootstrap.sh is re-run routinely; re-running apt here would spend a
        # network round-trip on every run to learn nothing.
        done, calls = self.invoke(installed=True)
        self.assertNotIn("apt-get", calls)
        self.assertIn("systemctl enable --now avahi-daemon", calls)
        self.assertEqual(done.returncode, 0)

    def test_a_daemon_that_will_not_start_does_not_fail_the_bootstrap(self):
        # The documented case: avahi refuses to start when the search domain
        # is "local". That must not abort provisioning.
        done, _ = self.invoke(installed=True, systemctl_ok=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WARNING", done.stderr)

    def test_a_failed_install_does_not_fail_the_bootstrap(self):
        done, calls = self.invoke(installed=False, apt_ok=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WARNING", done.stderr)
        # Nothing to enable if nothing installed.
        self.assertNotIn("systemctl", calls)


class BootstrapWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOOTSTRAP.read_text(encoding="utf-8")

    def test_bootstrap_runs_the_script_behind_an_existence_guard(self):
        self.assertIn('if [[ -f "$SCRIPT_DIR/scripts/mdns.sh" ]]; then', self.source)
        self.assertIn('bash "$SCRIPT_DIR/scripts/mdns.sh"', self.source)

    def test_mdns_runs_before_the_wifi_preference_step(self):
        # wifi-preference.sh can re-associate the Pi and drop the SSH session,
        # so it is deliberately last; anything after it may never run.
        mdns = self.source.find('bash "$SCRIPT_DIR/scripts/mdns.sh"')
        wifi = self.source.find('bash "$SCRIPT_DIR/scripts/wifi-preference.sh"')
        self.assertNotEqual(mdns, -1, "bootstrap must invoke scripts/mdns.sh")
        self.assertNotEqual(wifi, -1, "bootstrap must invoke wifi-preference.sh")
        self.assertLess(mdns, wifi, "a WiFi drop must not be able to skip mDNS setup")


if __name__ == "__main__":
    unittest.main()
