"""A service that will not start must not skip the rest of the bootstrap.

bootstrap.sh runs under `set -euo pipefail`, so a bare `systemctl restart` of a
unit that fails aborted the whole run at that line. On this rig the common
causes are environmental rather than config errors -- a carrier-less eth0
leaves lexacube-address.service unable to claim the service address, and a
missing USB sound card or display makes the game exit -- and every one of them
used to skip the host-level hardening 200 lines further down (watchdog,
persistent journal, zram, link-down routing, mDNS), leaving the machine half
provisioned with nothing saying which half.

The fix records the failure, carries on, and fails at the end instead. These
tests run the real fragments out of bootstrap.sh against a stubbed systemctl,
because the property that matters -- "this does not terminate the script" --
is a runtime one that reading the source cannot show.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
SOURCE = BOOTSTRAP.read_text(encoding="utf-8")

HELPER = re.search(r"^failed_services=\(\)\n.*?^\}\n", SOURCE, re.M | re.S)
SUMMARY = re.search(
    r"^if \(\( \$\{#failed_services\[@\]\} \)\); then\n.*\Z", SOURCE, re.M | re.S
)


class Harness:
    def run_fragments(self, *units_and_outcomes):
        """Run helper + calls + summary, with systemctl stubbed per unit.

        units_and_outcomes is (unit, ok) pairs. Returns the CompletedProcess.
        """
        self.assertIsNotNone(HELPER, "start_unit helper not found in bootstrap.sh")
        self.assertIsNotNone(SUMMARY, "failure summary not found at end of bootstrap.sh")

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        # The stub fails for exactly the units marked not-ok.
        bad = " ".join(u for u, ok in units_and_outcomes if not ok)
        stub = bin_dir / "systemctl"
        stub.write_text(
            "#!/bin/sh\n"
            f'for bad in {bad or "__none__"}; do\n'
            '  for arg in "$@"; do [ "$arg" = "$bad" ] && exit 1; done\n'
            "done\n"
            "exit 0\n"
        )
        stub.chmod(0o755)

        calls = "\n".join(f'start_unit "{u}"' for u, _ in units_and_outcomes)
        script = (
            "set -euo pipefail\n"
            + HELPER.group(0)
            + "\n"
            + calls
            + '\necho "REACHED THE END"\n'
            + SUMMARY.group(0)
        )
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        return subprocess.run(
            ["bash", "-s"], input=script, text=True, capture_output=True, env=env
        )


class StartUnitTests(Harness, unittest.TestCase):
    def test_a_healthy_service_leaves_the_run_clean(self):
        done = self.run_fragments(("mosquitto", True))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Bootstrap complete", done.stdout)
        self.assertNotIn("not running", done.stdout)

    def test_a_failing_service_does_not_abort_the_run(self):
        # The whole point: everything after the failure still executes.
        done = self.run_fragments(("lexacube.service", False))
        self.assertIn("REACHED THE END", done.stdout)

    def test_a_failing_service_is_reported_by_name(self):
        done = self.run_fragments(("lexacube.service", False))
        self.assertIn("WARNING", done.stderr)
        self.assertIn("lexacube.service", done.stdout + done.stderr)

    def test_a_failing_service_still_fails_the_run_at_the_end(self):
        # Carrying on must not become silence: the run did not fully succeed.
        done = self.run_fragments(("lexacube.service", False))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("1 service(s) not running", done.stdout)

    def test_later_services_still_start_after_an_earlier_one_fails(self):
        done = self.run_fragments(("lexacube.service", False), ("mosquitto", True))
        self.assertIn("1 service(s) not running", done.stdout)
        self.assertNotIn("mosquitto", done.stdout.split("not running")[1])

    def test_every_failure_is_counted(self):
        done = self.run_fragments(("a.service", False), ("b.service", False))
        self.assertIn("2 service(s) not running", done.stdout)


class NoBareRestartsTests(unittest.TestCase):
    def test_no_unit_is_restarted_outside_the_helper(self):
        # A new bare `systemctl restart` reintroduces the abort silently.
        offenders = []
        for number, line in enumerate(SOURCE.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("systemctl restart"):
                continue
            if stripped.endswith("|| true"):
                continue  # pre-existing, deliberate, and already non-fatal
            offenders.append(f"{number}: {stripped}")
        self.assertEqual(offenders, [], "use start_unit instead of systemctl restart")


if __name__ == "__main__":
    unittest.main()
