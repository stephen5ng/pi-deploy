"""Behaviour of scripts/service-address-failover.sh, driven with fake tools.

The script only shells out to `ip`, `systemctl` and the service-address helper,
so it can be exercised for real: each test builds a throwaway PATH of fakes plus
a fake /sys carrier file, runs one poll iteration, and asserts what the script
asked the helper to do. That covers the decisions worth protecting -- when it
re-homes, and the three cases where it must keep its hands off.
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


class FailoverHarness:
    """A sandbox of fake `ip`/`systemctl`/`service-address` plus fake carrier."""

    def __init__(self, root, *, holder, carrier, owner_active=True):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.calls = self.root / "calls.log"
        self.sysnet = self.root / "sys/class/net"

        if holder:
            iface_dir = self.sysnet / holder
            iface_dir.mkdir(parents=True, exist_ok=True)
            if carrier is not None:
                (iface_dir / "carrier").write_text(f"{carrier}\n")

        addr_line = f"2: {holder}    inet {ADDRESS} scope global secondary {holder}" if holder else ""
        self._write("ip", f"""
            [ "$1 $2 $3" = "-o -4 address" ] && {{ printf '%s\\n' "{addr_line}"; exit 0; }}
            exit 0
        """)
        self._write("systemctl", f"""
            echo "systemctl $*" >> "{self.calls}"
            [ "{'0' if owner_active else '1'}" = "0" ] && exit 0
            exit 3
        """)
        self._write("service-address", f"""
            echo "service-address $*" >> "{self.calls}"
            exit 0
        """)

    def _write(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def run_one_iteration(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        # Point the script at the fake sysfs and helper, and stop after one poll.
        patched = SCRIPT.read_text()
        patched = patched.replace("/sys/class/net/", f"{self.sysnet}/")
        patched = patched.replace(
            "HELPER=/usr/local/sbin/service-address", f"HELPER={self.bin}/service-address"
        )
        patched = patched.replace('while sleep "$INTERVAL"; do', "for _once in 1; do")
        runner = self.root / "runner.sh"
        runner.write_text(patched)
        subprocess.run(
            ["bash", str(runner), ADDRESS, OWNER, "0"],
            env=env, capture_output=True, timeout=30,
        )
        return self.calls.read_text() if self.calls.exists() else ""


class ServiceAddressFailoverTests(unittest.TestCase):
    def run_case(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            return FailoverHarness(tmp, **kwargs).run_one_iteration()

    def test_rehomes_when_the_holding_interface_loses_carrier(self):
        calls = self.run_case(holder="eth0", carrier=0)
        self.assertIn(f"service-address stop {ADDRESS} auto", calls)
        self.assertIn(f"service-address start {ADDRESS} auto", calls)
        self.assertLess(
            calls.index("service-address stop"),
            calls.index("service-address start"),
            "the address must be released before it is re-claimed",
        )

    def test_leaves_a_healthy_interface_alone(self):
        calls = self.run_case(holder="eth0", carrier=1)
        self.assertNotIn("service-address", calls)

    def test_does_nothing_while_the_owning_unit_is_stopped(self):
        # A stopped app must not have its address resurrected elsewhere.
        calls = self.run_case(holder="eth0", carrier=0, owner_active=False)
        self.assertNotIn("service-address", calls)

    def test_does_nothing_when_the_address_is_not_configured(self):
        # That case belongs to the ifup reclaim hook, not to this watcher.
        calls = self.run_case(holder="", carrier=None)
        self.assertNotIn("service-address", calls)

    def test_unreadable_carrier_is_treated_as_unusable(self):
        # An admin-down interface exposes no readable carrier; re-home rather
        # than sit on an interface that cannot pass traffic.
        calls = self.run_case(holder="eth0", carrier=None)
        self.assertIn("service-address stop", calls)


class FailoverWiringTests(unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_bootstrap_gates_the_watcher_on_the_apps_yaml_key(self):
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        self.assertIn("service_address.failover", bootstrap)
        self.assertIn('if [[ "$service_address_failover" == "true" ]]', bootstrap)

    def test_bootstrap_removes_the_watcher_when_disabled(self):
        # Flipping the key back to false must not leave a unit running.
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        self.assertIn('rm -f "/etc/systemd/system/$failover_service"', bootstrap)

    def test_lexacube_enables_failover(self):
        import yaml
        config = yaml.safe_load((REPOSITORY / "apps.yaml").read_text())
        app = next(a for a in config["apps"] if a["name"] == "lexacube")
        self.assertTrue(app["service_address"]["failover"])


if __name__ == "__main__":
    unittest.main()
