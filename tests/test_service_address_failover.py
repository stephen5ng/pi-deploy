"""Behaviour of scripts/service-address-failover.sh, driven with stateful fakes.

The script only shells out to `ip`, `systemctl` and the service-address helper,
so it can be exercised for real. The fakes share a state file holding the
current holder interface, and the fake helper mutates it exactly as the real one
would: `stop` clears the holder, `start` sets it (or fails, on demand). That
makes the loop observable across iterations, which matters -- the bug this suite
was extended for only appears on the iteration *after* a failed re-claim.
"""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "service-address-failover.sh"
ADDRESS = "192.168.8.247/24"
OWNER = "lexacube-address.service"


class Harness:
    """Fake ip/systemctl/service-address sharing one holder-state file."""

    def __init__(self, root, *, holder, carrier, owner_active=True,
                 start_failures=0, start_target="wlan0"):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.calls = self.root / "calls.log"
        self.state = self.root / "holder"
        self.failures = self.root / "start_failures"
        self.sysnet = self.root / "sys/class/net"

        self.state.write_text(holder or "")
        self.failures.write_text(str(start_failures))

        # Carrier for every interface that can hold the address.
        for iface, value in {holder: carrier, start_target: 1}.items():
            if not iface:
                continue
            d = self.sysnet / iface
            d.mkdir(parents=True, exist_ok=True)
            if value is not None:
                (d / "carrier").write_text(f"{value}\n")

        self._write("ip", f"""
            holder=$(cat "{self.state}")
            if [ "$1 $2 $3" = "-o -4 address" ]; then
                [ -n "$holder" ] && \\
                  printf '2: %s    inet {ADDRESS} scope global secondary %s\\n' "$holder" "$holder"
            fi
            exit 0
        """)
        self._write("systemctl", f"""
            echo "systemctl $*" >> "{self.calls}"
            exit {0 if owner_active else 3}
        """)
        self._write("service-address", f"""
            echo "service-address $*" >> "{self.calls}"
            case "$1" in
              stop)  : > "{self.state}" ;;
              start)
                n=$(cat "{self.failures}" 2>/dev/null || echo 0)
                if [ "$n" -gt 0 ]; then echo $((n - 1)) > "{self.failures}"; exit 1; fi
                printf '%s' "{start_target}" > "{self.state}" ;;
            esac
            exit 0
        """)

    def _write(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def run(self, iterations=1):
        patched = SCRIPT.read_text()
        patched = patched.replace("/sys/class/net/", f"{self.sysnet}/")
        patched = patched.replace(
            "HELPER=/usr/local/sbin/service-address", f"HELPER={self.bin}/service-address")
        patched = patched.replace(
            'while sleep "$INTERVAL"; do', f'for _i in $(seq 1 {iterations}); do')
        runner = self.root / "runner.sh"
        runner.write_text(patched)
        subprocess.run(
            ["bash", str(runner), ADDRESS, OWNER, "0"],
            env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}"),
            capture_output=True, timeout=30,
        )
        return (self.calls.read_text() if self.calls.exists() else ""), self.state.read_text()


class FailoverTests(unittest.TestCase):
    def run_case(self, iterations=1, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            return Harness(tmp, **kwargs).run(iterations)

    def test_rehomes_when_the_holding_interface_loses_carrier(self):
        calls, holder = self.run_case(holder="eth0", carrier=0)
        self.assertIn(f"service-address stop {ADDRESS} auto", calls)
        self.assertIn(f"service-address start {ADDRESS} auto", calls)
        self.assertLess(calls.index("service-address stop"), calls.index("service-address start"))
        self.assertEqual(holder, "wlan0", "the address should land on the live interface")

    def test_leaves_a_healthy_interface_alone(self):
        calls, holder = self.run_case(holder="eth0", carrier=1, iterations=3)
        self.assertNotIn("service-address", calls)
        self.assertEqual(holder, "eth0")

    def test_does_nothing_while_the_owning_unit_is_stopped(self):
        calls, _ = self.run_case(holder="eth0", carrier=0, owner_active=False)
        self.assertNotIn("service-address", calls)

    def test_does_not_claim_an_address_it_did_not_release(self):
        # Missing but not ours: the ifup reclaim hook owns that case, and
        # claiming it here would race that hook.
        calls, holder = self.run_case(holder="", carrier=None, iterations=3)
        self.assertNotIn("service-address", calls)
        self.assertEqual(holder, "")

    def test_unreadable_carrier_is_treated_as_unusable(self):
        calls, _ = self.run_case(holder="eth0", carrier=None)
        self.assertIn("service-address stop", calls)

    # --- regression: PR #18 review, [P2] stranded address ---------------------
    def test_retries_start_after_a_failed_rehome(self):
        # stop succeeds and start fails, so the address is configured nowhere.
        # The next iteration must retry start rather than seeing "no holder"
        # and skipping forever.
        calls, holder = self.run_case(
            holder="eth0", carrier=0, start_failures=1, iterations=3)
        self.assertEqual(
            calls.count(f"service-address start {ADDRESS} auto"), 2,
            "start must be retried after it failed",
        )
        self.assertEqual(holder, "wlan0", "the address must end up configured")

    def test_keeps_retrying_while_no_interface_is_usable(self):
        calls, holder = self.run_case(
            holder="eth0", carrier=0, start_failures=99, iterations=4)
        self.assertGreaterEqual(
            calls.count("service-address start"), 4,
            "it must keep retrying rather than giving up",
        )
        self.assertEqual(holder, "", "still unconfigured, but not abandoned")

    def test_a_stopped_owner_cancels_a_pending_rehome(self):
        # If the app stops mid-retry its ExecStop owns the address; the watcher
        # must not resurrect it. Covered by the is-active guard clearing state.
        calls, _ = self.run_case(
            holder="eth0", carrier=0, start_failures=99,
            owner_active=False, iterations=3)
        self.assertNotIn("service-address", calls)


class WiringTests(unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_bootstrap_gates_the_watcher_on_the_apps_yaml_key(self):
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        self.assertIn("service_address.failover", bootstrap)
        self.assertIn('if [[ "$service_address_failover" == "true" ]]', bootstrap)

    def test_bootstrap_removes_the_watcher_when_disabled(self):
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        self.assertIn('rm -f "/etc/systemd/system/$failover_service"', bootstrap)

    def test_lexacube_pins_its_service_address_to_the_wire(self):
        """lexacube deliberately does NOT fail over. See apps.yaml for the
        measurement, but in short: failing over keeps the game limping on WiFi
        and then leaves it there after the cable returns, needing a restart
        nobody knows to perform. Pinning drops every cube at once -- loud and
        certain -- and recovers by itself when carrier comes back, because the
        address never went anywhere.

        The watcher itself stays in this repo and is still tested above; this
        asserts only that this app does not ask for it.

        Read as text rather than parsed, per test_generated_units.py: this
        suite is stdlib-only and bootstrap.sh reads apps.yaml with the `yq`
        CLI. The `import yaml` this used to do made the test an error rather
        than a pass on any box without pyyaml -- including this one.
        """
        config = (REPOSITORY / "apps.yaml").read_text(encoding="utf-8")
        start = config.index("- name: lexacube")
        block = config[start:]
        end = block.find("\n  - name:")
        block = block if end == -1 else block[:end]
        self.assertRegex(block, r"(?m)^\s*failover:\s*false\s*(#.*)?$")

    def test_bootstrap_removes_the_watcher_when_an_app_stops_asking(self):
        """Turning the key off must actually uninstall it. A watcher left
        running would keep moving the address off the wire, which is the whole
        behaviour being retired -- and it would do so invisibly."""
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        self.assertIn('systemctl disable --now "$failover_service"', bootstrap)
        self.assertIn('rm -f "/etc/systemd/system/$failover_service"', bootstrap)


if __name__ == "__main__":
    unittest.main()
