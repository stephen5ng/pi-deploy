"""Rewriting /etc/network/interfaces, which is the risky part.

A malformed interfaces file means no network at boot and a trip to the machine,
so the stanza rewrite is exercised here against the real DietPi file shape
rather than trusted. The script needs root and a live ifupdown; the Python block
inside it does not, so it is extracted and run on fixtures -- a change to the
rewrite is a change to what these tests see.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "network-interfaces.sh"
MARKER = "# Managed by pi-deploy: wired preferred, WiFi fallback."

# The file as DietPi ships it on this rig: eth0's allow-hotplug commented out,
# and static directives stranded under an `inet dhcp` method.
DIETPI_INTERFACES = """\
# Location: /etc/network/interfaces
# Please modify network settings via: dietpi-config

# Drop-in configs
source interfaces.d/*

# Ethernet
#allow-hotplug eth0
iface eth0 inet dhcp
address 192.168.8.247
netmask 255.255.255.0
gateway 192.168.8.1
#dns-nameservers 192.168.8.1

# WiFi
allow-hotplug wlan0
iface wlan0 inet dhcp
pre-up iw dev wlan0 set power_save off
post-down iw dev wlan0 set power_save on
wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf
"""


def extract_rewriter() -> str:
    """The python heredoc the script runs, taken from the script itself."""
    source = SCRIPT.read_text()
    match = re.search(r"python3 - .*?<<'PY'\n(.*?)\nPY\n", source, re.S)
    assert match, "could not find the rewrite block in network-interfaces.sh"
    return match.group(1)


class StanzaRewriteTests(unittest.TestCase):
    def rewrite(self, original: str, tmp: Path) -> str:
        path = tmp / "interfaces"
        path.write_text(original)
        subprocess.run(
            ["python3", "-", str(path), MARKER, "100", "600"],
            input=extract_rewriter(), text=True, check=True, capture_output=True,
        )
        return path.read_text()

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_eth0_is_brought_up_at_boot(self):
        # DietPi ships this commented out, which is why a plugged cable did
        # nothing and the interface sat DOWN.
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        self.assertRegex(result, r"(?m)^allow-hotplug eth0$")

    def test_the_wired_path_outranks_the_wireless_one(self):
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        eth = result.index("iface eth0")
        wlan = result.index("iface wlan0")
        self.assertIn("metric 100", result[eth:wlan])
        self.assertIn("metric 600", result[wlan:])

    def test_the_stranded_static_directives_are_gone(self):
        # address/netmask/gateway under `inet dhcp` read as configuration but
        # ifupdown ignores them; leaving them invites belief in a lie.
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        for directive in ("address 192.168.8.247", "netmask ", "gateway "):
            self.assertNotIn(directive, result)

    def test_the_wifi_supplicant_and_power_save_lines_survive(self):
        # Losing wpa-conf costs the fallback path entirely; losing power_save
        # off reintroduces latency this deployment already fixed.
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        self.assertIn("wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf", result)
        self.assertIn("iw dev wlan0 set power_save off", result)

    def test_unrelated_configuration_is_preserved(self):
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        self.assertIn("source interfaces.d/*", result)

    def test_each_interface_is_defined_exactly_once(self):
        # ifupdown rejects a duplicate iface stanza, and the failure mode is no
        # network at boot.
        result = self.rewrite(DIETPI_INTERFACES, self.tmp)
        for iface in ("eth0", "wlan0"):
            self.assertEqual(
                len(re.findall(rf"(?m)^iface {iface} inet", result)), 1, iface
            )

    def test_rewriting_twice_is_stable(self):
        # bootstrap is idempotent; the marker guard means this should not even
        # run twice, but a re-run must not corrupt the file if it does.
        once = self.rewrite(DIETPI_INTERFACES, self.tmp)
        twice = self.rewrite(once, self.tmp)
        self.assertEqual(
            len(re.findall(r"(?m)^iface eth0 inet", twice)), 1,
            "a second rewrite duplicated the stanza",
        )


class ScriptWiringTests(unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_it_backs_the_file_up_before_rewriting(self):
        self.assertIn("before-pi-deploy", SCRIPT.read_text())

    def test_a_marker_makes_it_idempotent(self):
        self.assertIn('grep -qF "$MARKER"', SCRIPT.read_text())

    def test_the_subnet_route_is_handled_too(self):
        # dhclient's IF_METRIC covers the default route only; on-link
        # destinations -- every cube -- follow the subnet route.
        text = SCRIPT.read_text()
        self.assertIn("pi-deploy-route-metrics", text)
        self.assertIn("proto kernel scope link", text)

    def test_arp_is_scoped_to_the_owning_interface(self):
        text = SCRIPT.read_text()
        self.assertIn("net.ipv4.conf.all.arp_ignore = 1", text)
        self.assertIn("net.ipv4.conf.all.arp_announce = 2", text)

    def test_bootstrap_runs_it_before_the_hardening(self):
        # The metrics are only half the fallback; ignore_routes_with_linkdown
        # from reliability.sh is the other half.
        bootstrap = (REPOSITORY / "bootstrap.sh").read_text()
        # Compare the INVOCATIONS, not the first mention: reliability.sh is
        # named in a comment well above the line that runs it.
        network = bootstrap.index('bash "$SCRIPT_DIR/scripts/network-interfaces.sh"')
        reliability = bootstrap.index('bash "$SCRIPT_DIR/scripts/reliability.sh"')
        self.assertLess(network, reliability)


if __name__ == "__main__":
    unittest.main()
