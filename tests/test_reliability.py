"""What scripts/reliability.sh must keep doing.

The script needs root, systemd and a live kernel, so it cannot be executed here.
These tests read it instead, and guard the two decisions that are easy to
"simplify" back into bugs:

  * the WiFi monitor is *masked*, not merely disabled. dietpi-config's WiFi menu
    runs `systemctl enable --now dietpi-wifi-monitor`, so a disabled unit comes
    back silently; a masked one fails loudly.
  * masking is preceded by moving the real unit file aside. The unit ships as a
    regular file in /etc/systemd/system and `systemctl mask` refuses to replace
    it with its /dev/null symlink ("File ... already exists").
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
RELIABILITY = REPOSITORY / "scripts" / "reliability.sh"
UNIT = "dietpi-wifi-monitor"


class ReliabilityScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = RELIABILITY.read_text(encoding="utf-8")

    def test_the_script_is_valid_bash(self):
        subprocess.run(
            ["bash", "-n", str(RELIABILITY)], check=True, capture_output=True
        )

    def test_the_wifi_monitor_is_masked_not_only_disabled(self):
        self.assertRegex(
            self.source,
            rf"systemctl mask {re.escape(UNIT)}",
            "dietpi-config re-enables a merely disabled unit; it must be masked",
        )

    def test_the_real_unit_is_moved_aside_before_masking(self):
        move = self.source.find('mv "$wifi_monitor_unit"')
        mask = self.source.find(f"systemctl mask {UNIT}")
        self.assertNotEqual(move, -1, "the real unit file must be moved aside")
        self.assertNotEqual(mask, -1, "the unit must be masked")
        self.assertLess(
            move, mask, "mask fails unless the real unit file is moved first"
        )

    def test_the_backup_keeps_the_original_unit_recoverable(self):
        self.assertIn("/opt/pi-deploy-disabled/", self.source)

    def test_link_down_routes_are_ignored(self):
        # Without this a pulled cable blackholes traffic on a NO-CARRIER
        # interface instead of failing over to a higher-metric path.
        for scope in ("all", "default"):
            self.assertRegex(
                self.source,
                rf"net\.ipv4\.conf\.{scope}\.ignore_routes_with_linkdown\s*=\s*1",
            )

    def test_the_sysctl_drop_in_is_namespaced_to_pi_deploy(self):
        self.assertIn("/etc/sysctl.d/60-pi-deploy-linkdown.conf", self.source)

    def test_every_step_banner_agrees_on_the_step_count(self):
        banners = re.findall(r'echo "\[(\d+)/(\d+)\]', self.source)
        self.assertTrue(banners, "expected numbered step banners")
        totals = {total for _, total in banners}
        self.assertEqual(len(totals), 1, f"step banners disagree: {totals}")
        total = int(totals.pop())
        self.assertEqual(
            sorted(int(step) for step, _ in banners),
            list(range(1, total + 1)),
            "step banners must be consecutive and complete",
        )


class UnitOrderingTests(unittest.TestCase):
    """No unit written here may be ordered after multi-user.target.

    `WantedBy=multi-user.target` already places a unit in the target; adding
    `After=` makes it wait for the whole target's start job as well. On this rig
    that job can stay pending indefinitely -- lexacube-address.service retries
    its claim forever by design, so with no cable multi-user.target never
    finishes starting -- and anything ordered after it then waits forever too.

    That is how bootstrap.sh came to hang, silently, inside
    `systemctl enable --now pi-health-watch.service`. Probed on the rig against
    a wedged job queue: an otherwise identical unit WITH the ordering timed out
    (exit 124), one WITHOUT it exited 0.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = RELIABILITY.read_text(encoding="utf-8")
        # Every `cat > /etc/systemd/system/<name> <<'EOF' ... EOF` heredoc.
        cls.units = dict(
            re.findall(
                r"cat > /etc/systemd/system/(\S+) <<'EOF'\n(.*?)^EOF$",
                cls.source,
                re.M | re.S,
            )
        )

    def test_the_script_writes_at_least_one_unit(self):
        # Guards the regex above: a silent zero-match would pass everything.
        self.assertTrue(self.units, "no unit heredocs found -- has the form changed?")

    def test_no_unit_waits_for_the_whole_multi_user_target(self):
        offenders = [
            name
            for name, body in self.units.items()
            if re.search(r"^After=.*\bmulti-user\.target\b", body, re.M)
        ]
        self.assertEqual(
            offenders, [], "WantedBy= is enough; After= deadlocks on a stuck target"
        )

    def test_the_health_logger_still_starts_at_boot(self):
        # Dropping the ordering must not drop the unit out of the target.
        body = self.units.get("pi-health-watch.service")
        self.assertIsNotNone(body, "pi-health-watch.service is no longer written")
        self.assertRegex(body, r"(?m)^WantedBy=multi-user\.target$")


class RamlogIsRemovedTests(unittest.TestCase):
    """RAMlog's hourly `dietpi-logclear 1` truncates every file under /var/log,
    including the bind-mounted journal, so the persistent journal held at most
    an hour. A whole event day of logs was lost to it.

    The step is run here against a fake /boot/dietpi with a stub
    dietpi-software, so what is checked is what the shell actually does.
    """

    @classmethod
    def setUpClass(cls):
        source = RELIABILITY.read_text(encoding="utf-8")
        match = re.search(
            r"^if \[ -f /boot/dietpi/\.installed \]; then\n.*?^fi$",
            source,
            re.M | re.S,
        )
        assert match, "the RAMlog step is no longer a top-level if-block"
        cls.step = match.group(0)

    def run_step(self, installed):
        with tempfile.TemporaryDirectory() as tmp:
            dietpi = Path(tmp) / "dietpi"
            dietpi.mkdir()
            (dietpi / ".installed").write_text(installed, encoding="utf-8")
            calls = Path(tmp) / "calls"
            stub = dietpi / "dietpi-software"
            stub.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n', encoding="utf-8")
            stub.chmod(0o755)
            env = dict(os.environ)
            if sys.platform == "darwin":
                # The script is GNU sed (the Pi); BSD sed's -i takes a suffix.
                shim = Path(tmp) / "bin"
                shim.mkdir()
                (shim / "sed").write_text(
                    '#!/bin/sh\n[ "$1" = -i ] && { shift; exec /usr/bin/sed -i "" "$@"; }\n'
                    'exec /usr/bin/sed "$@"\n',
                    encoding="utf-8",
                )
                (shim / "sed").chmod(0o755)
                env["PATH"] = f"{shim}:{env['PATH']}"
            script = "set -euo pipefail\n" + self.step.replace(
                "/boot/dietpi/", f"{dietpi}/"
            )
            subprocess.run(
                ["bash", "-c", script], check=True, capture_output=True, env=env
            )
            return (
                (dietpi / ".installed").read_text(encoding="utf-8"),
                calls.read_text(encoding="utf-8") if calls.exists() else "",
            )

    def test_an_installed_ramlog_is_uninstalled_and_the_clear_stops(self):
        installed, calls = self.run_step(
            "aSOFTWARE_INSTALL_STATE[103]=2\nINDEX_LOGGING=-1\n"
        )
        self.assertEqual(calls, "uninstall 103\n")
        self.assertIn("INDEX_LOGGING=0\n", installed)
        self.assertNotIn("INDEX_LOGGING=-1", installed)

    def test_a_rerun_after_the_uninstall_does_nothing(self):
        installed, calls = self.run_step(
            "aSOFTWARE_INSTALL_STATE[103]=0\nINDEX_LOGGING=0\n"
        )
        self.assertEqual(calls, "")
        self.assertIn("INDEX_LOGGING=0\n", installed)

    def test_fresh_flashes_never_install_ramlog(self):
        template = (REPOSITORY / "dietpi.template.txt").read_text(encoding="utf-8")
        self.assertRegex(template, r"(?m)^AUTO_SETUP_LOGGING_INDEX=0$")


if __name__ == "__main__":
    unittest.main()
