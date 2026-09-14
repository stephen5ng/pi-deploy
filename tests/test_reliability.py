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

import re
import subprocess
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


if __name__ == "__main__":
    unittest.main()
