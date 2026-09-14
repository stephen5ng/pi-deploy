"""Preferring one WiFi network over another, and naming the cubes' network.

Both failures here are silent and slow to notice. A Pi that falls back to
2.4GHz still works -- it just takes airtime from the single-band cubes that
cannot leave that band. Cubes compiled with a 5GHz SSID flash fine and simply
never associate, with nothing on the Pi to say why.

The shell script only edits a file, so it is exercised for real on fixtures.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "wifi-preference.sh"
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
CONFIG = REPOSITORY / "apps.yaml"

# Exactly what /boot/dietpi/func/dietpi-wifidb writes: ssid, scan_ssid,
# key_mgmt, psk. No priority line -- which is the whole problem.
DIETPI_SUPPLICANT = """\
# WiFi country code, set here in case the access point does send one
country=US
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1

network={
\tssid="FunAcross"
\tscan_ssid=1
\tkey_mgmt=WPA-PSK
\tpsk=aaaa1111
}

network={
\tssid="FunAcross-5G"
\tscan_ssid=1
\tkey_mgmt=WPA-PSK
\tpsk=bbbb2222
}
"""


def priorities(text):
    """{ssid: priority} for every network block, priority None if absent."""
    result = {}
    for block in re.findall(r"network=\{.*?\n\}", text, re.S):
        ssid = re.search(r'ssid="(.*)"', block)
        if ssid is None:
            continue
        found = re.search(r"priority=(\S+)", block)
        result[ssid.group(1)] = found.group(1) if found else None
    return result


class PreferenceTests(unittest.TestCase):
    def apply(self, supplicant, preferred):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wpa_supplicant.conf"
            path.write_text(supplicant)
            body = SCRIPT.read_text()
            body = body.replace(
                "SUPPLICANT=/etc/wpa_supplicant/wpa_supplicant.conf",
                f"SUPPLICANT={path}",
            )
            subprocess.run(
                ["bash", "-s", preferred], input=body, text=True,
                check=True, capture_output=True,
            )
            return path.read_text()

    def test_the_preferred_network_outranks_the_other(self):
        result = self.apply(DIETPI_SUPPLICANT, "FunAcross-5G")
        found = priorities(result)
        self.assertGreater(int(found["FunAcross-5G"]), int(found["FunAcross"]))

    def test_dietpi_writes_no_priority_at_all(self):
        """The premise. If DietPi ever starts emitting priority, this script
        becomes a different problem and this fixture should fail."""
        self.assertEqual(
            priorities(DIETPI_SUPPLICANT), {"FunAcross": None, "FunAcross-5G": None}
        )

    def test_applying_twice_changes_nothing(self):
        once = self.apply(DIETPI_SUPPLICANT, "FunAcross-5G")
        twice = self.apply(once, "FunAcross-5G")
        self.assertEqual(once, twice)
        self.assertEqual(once.count("priority="), 2)

    def test_an_existing_priority_is_corrected_not_duplicated(self):
        backwards = DIETPI_SUPPLICANT.replace(
            '\tpsk=aaaa1111\n}', '\tpsk=aaaa1111\n\tpriority=10\n}'
        )
        result = self.apply(backwards, "FunAcross-5G")
        self.assertEqual(result.count("priority="), 2)
        found = priorities(result)
        self.assertGreater(int(found["FunAcross-5G"]), int(found["FunAcross"]))

    def test_the_credentials_are_never_touched(self):
        """psk is PBKDF2(passphrase, SSID), so it differs per network even with
        one password. This script must never move or rewrite one."""
        result = self.apply(DIETPI_SUPPLICANT, "FunAcross-5G")
        self.assertIn("psk=aaaa1111", result)
        self.assertIn("psk=bbbb2222", result)
        self.assertIn("country=US", result)
        self.assertIn("update_config=1", result)

    def test_no_preference_configured_is_a_no_op(self):
        self.assertEqual(self.apply(DIETPI_SUPPLICANT, ""), DIETPI_SUPPLICANT)


class WiringTests(unittest.TestCase):
    def test_bootstrap_runs_it_with_the_configured_ssid(self):
        bootstrap = BOOTSTRAP.read_text()
        self.assertIn('scripts/wifi-preference.sh" "$preferred_ssid"', bootstrap)
        self.assertIn(".wifi.preferred_ssid // empty", bootstrap)

    def test_the_cube_ssid_is_named_rather_than_positional(self):
        """The generator otherwise takes the lowest-numbered configured entry.
        This Pi is dual-band and the ESP32 is 2.4GHz only, so an unnamed choice
        can compile an SSID the cubes physically cannot join."""
        bootstrap = BOOTSTRAP.read_text()
        self.assertIn(".secret_file.ssid // empty", bootstrap)
        self.assertIn("--ssid", bootstrap)

    def test_this_rig_names_both_networks(self):
        config = CONFIG.read_text()
        self.assertRegex(config, r"(?m)^\s*preferred_ssid:\s*\S+")
        self.assertRegex(config, r"(?m)^\s*ssid:\s*\S+")

    def test_the_two_named_networks_differ(self):
        """If they were the same the Pi would be preferring the cubes' band,
        which is the state this whole change exists to leave."""
        config = CONFIG.read_text()
        preferred = re.search(r"(?m)^\s*preferred_ssid:\s*(\S+)", config).group(1)
        cube = re.search(r"(?m)^\s*ssid:\s*(\S+)", config).group(1)
        self.assertNotEqual(preferred, cube)


if __name__ == "__main__":
    unittest.main()
