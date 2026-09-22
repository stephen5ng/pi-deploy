"""The WiFi boot path must be able to rescue itself.

Measured on the rig: at boot, ifupdown's pre-up starts wpa_supplicant as a
daemon and it dies -- "daemon failed to start", ifup aborts, wlan0 stays down,
the box is unreachable until someone drives it from the console. The same
binary run by hand minutes later works first try. The boot-time failure
captures no logs, so the race is undiagnosed; this unit is the witness that
keeps the box reachable until it is.

Tests run the generated rescue script for real against stubs. The stub `sleep`
is a no-op so the 120s wait loop runs instantly.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "network-interfaces.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")

# The generated rescue script, lifted whole from the heredoc.
RESCUE = re.search(
    r"cat > /usr/local/sbin/pi-deploy-wifi-rescue <<'EOF'\n(.*?)^EOF$",
    SOURCE, re.M | re.S,
)


class Harness:
    def run_rescue(self, *, associated, has_route=False):
        self.assertIsNotNone(RESCUE, "rescue heredoc not found in network-interfaces.sh")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        log = tmp / "calls.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        def stub(name, body):
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\necho "{name} $@" >> {log}\n{body}\n')
            path.chmod(0o755)

        # wpa_cli reports the daemon's own view of the link. `associated`
        # means wpa_state=COMPLETED, not merely that a process exists.
        stub("wpa_cli", 'echo "wpa_state=%s"'
             % ("COMPLETED" if associated else "SCANNING"))
        # A process may exist while unassociated -- the exact state that fooled
        # the first version -- so pgrep succeeds in both cases here.
        stub("pgrep", "exit 0")
        stub("pkill", "exit 0")
        stub("rm", "exit 0")
        stub("sleep", "exit 0")  # the 120s wait becomes instant
        # `ip route show default dev wlan0` prints a line when a default route
        # exists and nothing when it does not; the rescue greps for content.
        stub("ip", 'case "$*" in *"route show default"*) %s ;; esac\nexit 0'
             % ("echo 'default via 192.168.8.1 dev wlan0'" if has_route else ":"))
        stub("wpa_supplicant", "exit 0")
        stub("dhclient", "exit 0")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        done = subprocess.run(
            ["bash", "-s"], input=RESCUE.group(1), text=True,
            capture_output=True, env=env,
        )
        return done, log.read_text() if log.exists() else ""


class RescueScriptTests(Harness, unittest.TestCase):
    def test_the_rescue_script_is_generated(self):
        self.assertIsNotNone(RESCUE)

    def test_a_normal_boot_is_a_noop(self):
        # Associated by the standard path: this must not touch anything.
        done, calls = self.run_rescue(associated=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nothing to do", done.stdout)
        for forbidden in ("wpa_supplicant ", "dhclient ", "ip link"):
            self.assertNotIn(forbidden, calls)

    def test_an_unassociated_daemon_does_not_count_as_success(self):
        # The bug this replaces: the first version polled for a wpa_supplicant
        # PROCESS. ifupdown's wrapper reports "daemon failed to start" when the
        # daemon is not ready in time and aborts ifup, but the process it
        # spawned can still be sitting there unassociated. The rescue found it,
        # called the boot healthy and exited -- measured on the rig, the box
        # stayed off the network for the whole boot while the unit reported
        # success. The stub keeps pgrep succeeding to pin exactly that.
        done, calls = self.run_rescue(associated=False)
        self.assertIn("taking over", done.stdout)
        self.assertIn("wpa_supplicant", calls)

    def test_the_takeover_clears_the_stale_control_socket(self):
        # A half-started daemon holds /run/wpa_supplicant/wlan0, and a second
        # instance on it exits 255 -- verified on the rig. Without this the
        # takeover inherits the state it exists to replace.
        _, calls = self.run_rescue(associated=False)
        self.assertIn("pkill", calls)

    def test_a_failed_boot_starts_the_daemon_and_leases(self):
        done, calls = self.run_rescue(associated=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        # Matched per-flag, not as one literal: pinning the whole command
        # string means any added flag fails a test that has no opinion on it.
        wpa = next(c for c in calls.splitlines() if c.startswith("wpa_supplicant "))
        for flag in ("-B", "-i wlan0", "-c /etc/wpa_supplicant/wpa_supplicant.conf"):
            self.assertIn(flag, wpa)
        dhcp = next(c for c in calls.splitlines() if c.startswith("dhclient "))
        self.assertTrue(dhcp.endswith("wlan0"), dhcp)

    def test_the_rescue_uses_the_wrappers_own_invocation(self):
        # Same args as Debian's /etc/network/if-pre-up.d/wpasupplicant, so the
        # rescue reproduces the intended configuration, not a variant of it.
        self.assertIn(
            "-B -P /run/wpa_supplicant.wlan0.pid", RESCUE.group(1)
        )
        self.assertIn("-D nl80211,wext", RESCUE.group(1))

    def test_the_rescued_daemon_logs_to_syslog(self):
        # Under -B nothing else reaches the journal, and the one boot this
        # daemon runs on is the boot whose evidence is worth having. Debian's
        # own wrapper passes -s for the same reason.
        self.assertIn("-s -B -P /run/wpa_supplicant.wlan0.pid", RESCUE.group(1))

    def test_the_rescued_lease_keeps_wifi_behind_the_wire(self):
        # ifupdown passes the stanza's `metric` to dhclient as IF_METRIC, and
        # dhclient-script is the only thing that puts a metric on the default
        # route. A bare `dhclient` leaves it at 0 -- ahead of eth0's 100 -- so
        # a rescued boot would route internet traffic over WiFi while the wire
        # idles, inverting what this script exists to set.
        _, calls = self.run_rescue(associated=False)
        self.assertIn("-e IF_METRIC=", calls)

    def test_the_rescued_metric_matches_the_wireless_stanza(self):
        # The quoted heredoc cannot expand WIRELESS_METRIC, so the literal is
        # written out -- and would drift silently without this.
        declared = re.search(r"(?m)^WIRELESS_METRIC=(\d+)", SOURCE).group(1)
        used = re.search(r"-e IF_METRIC=(\d+)", RESCUE.group(1)).group(1)
        self.assertEqual(
            used, declared,
            "the rescue's IF_METRIC must track WIRELESS_METRIC",
        )

    def test_the_rescued_client_can_be_found_by_ifdown(self):
        # ifupdown's dhcp method names these files; a bare `dhclient` uses the
        # defaults, and a later `ifdown wlan0` then cannot stop this client.
        self.assertIn("-pf /run/dhclient.wlan0.pid", RESCUE.group(1))
        self.assertIn("-lf /var/lib/dhcp/dhclient.wlan0.leases", RESCUE.group(1))

    def test_a_stale_address_without_a_route_still_leases(self):
        # An address alone is not a working lease. Measured on the rig: wlan0
        # held 192.168.8.129 with no default route, so the on-link resolver
        # answered while everything off-subnet failed -- `git pull` could not
        # reach github while the gateway pinged fine. An address-only guard
        # skips dhclient in exactly that state.
        _, calls = self.run_rescue(associated=False, has_route=False)
        self.assertIn("dhclient", calls)

    def test_a_working_route_is_left_alone(self):
        # The other half: with a real lease in place, do not re-lease.
        _, calls = self.run_rescue(associated=False, has_route=True)
        self.assertNotIn("dhclient", calls)

    def test_it_waits_before_concluding_the_standard_path_failed(self):
        # The whole point is giving the boot-time race time to win on its own.
        self.assertIn("seq 1 24", RESCUE.group(1))
        self.assertIn("sleep 5", RESCUE.group(1))


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE

    def test_the_unit_is_enabled_not_just_written(self):
        self.assertIn("systemctl enable pi-deploy-wifi-rescue.service", self.source)

    def test_the_unit_is_not_ordered_after_multi_user_target(self):
        # pi-deploy #40: that target's start job can stay pending on this rig
        # while the service address loops with no carrier, and anything
        # ordered behind it waits forever -- which is how bootstrap once hung.
        body = re.search(
            r"cat > /etc/systemd/system/pi-deploy-wifi-rescue\.service <<'EOF'\n(.*?)^EOF$",
            self.source, re.M | re.S,
        ).group(1)
        self.assertIn("WantedBy=multi-user.target", body)
        # Non-comment lines only: the comment explaining WHY the ordering is
        # absent names the very line that must not exist.
        effective = re.sub(r"(?m)^\s*#.*$", "", body)
        self.assertNotIn("After=multi-user.target", effective)

    def test_a_oneshot_that_leaves_a_daemon_behind_is_not_killed_by_systemd(self):
        """Stated as the property, not as the literal line.

        Type=oneshot defaults to KillMode=control-group: when the main process
        exits, systemd kills everything left in the service cgroup -- including
        a daemon the script started with -B. Measured on the rig: the rescue
        associated at 20:47:13, printed a live address at 20:47:15, and
        CTRL-EVENT-TERMINATING landed in the same second as "Deactivated
        successfully". The unit reported status=0/SUCCESS while the box stayed
        offline for the entire boot.

        The premise is checked rather than assumed, so if the rescue is ever
        changed to run wpa_supplicant in the foreground this stops demanding a
        flag that would no longer be needed.
        """
        unit = re.search(
            r"cat > /etc/systemd/system/pi-deploy-wifi-rescue\.service <<'EOF'\n(.*?)^EOF$",
            SOURCE, re.M | re.S,
        )
        self.assertIsNotNone(unit, "rescue unit not found in network-interfaces.sh")
        body = re.sub(r"(?m)^\s*#.*$", "", unit.group(1))

        # Comments stripped first: the rescue's own comment explains what -B
        # does, and matching that would make the premise permanently true no
        # matter what the code did.
        rescue_code = re.sub(r"(?m)^\s*#.*$", "", RESCUE.group(1))
        starts_a_daemon = "-B" in rescue_code
        is_oneshot = re.search(r"(?m)^Type=oneshot$", body)
        if not (starts_a_daemon and is_oneshot):
            return  # premise gone; the flags below are no longer the point

        self.assertRegex(
            body, r"(?m)^RemainAfterExit=yes$",
            "without this the cgroup is collected as soon as the script exits",
        )
        self.assertRegex(
            body, r"(?m)^KillMode=process$",
            "the default control-group mode kills the daemon the script started",
        )

    def test_the_script_is_valid_bash(self):
        subprocess.run(
            ["bash", "-n", str(SCRIPT)], check=True, capture_output=True
        )


if __name__ == "__main__":
    unittest.main()
