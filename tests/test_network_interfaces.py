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


# A third interface nobody told this script about. The stanza is *bare* -- no
# auto/allow-hotplug line -- which is what a regex bounded by "the next
# auto/allow-hotplug line" cannot see, so it consumed this along with wlan0's.
THIRD_INTERFACE = """\
auto lo
iface lo inet loopback

allow-hotplug wlan0
iface wlan0 inet dhcp
    wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf

iface usb0 inet dhcp
    metric 900
"""

# A bare owned stanza with unindented options, DietPi style, followed by an
# unrelated stanza. Removing the head but not the options would leave
# `address`/`netmask` at top level, which is a malformed file -- worse than
# losing one interface, because ifupdown then configures nothing.
BARE_OWNED_STANZA = """\
auto lo
iface lo inet loopback

iface eth0 inet dhcp
address 10.0.0.5
netmask 255.255.255.0

auto br0
iface br0 inet manual
"""

# One allow line naming an owned interface *and* another one.
SHARED_ALLOW_LINE = """\
auto eth0 usb0

allow-hotplug usb0
iface usb0 inet dhcp
    metric 900
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

    def test_a_bare_third_interface_survives(self):
        """The reported case. `iface usb0` has no auto/allow-hotplug line, so
        the old regex ran past it and deleted it with wlan0's stanza -- after
        a reboot, no configuration for that interface at all."""
        result = self.rewrite(THIRD_INTERFACE, self.tmp)
        self.assertIn("iface usb0 inet dhcp", result)
        self.assertIn("metric 900", result)

    def test_a_bare_owned_stanza_takes_its_options_with_it(self):
        """The other half of the same regex bug: the head was removed and the
        unindented options were left orphaned at top level."""
        result = self.rewrite(BARE_OWNED_STANZA, self.tmp)
        kept = result.split(MARKER)[0]
        self.assertNotIn("address 10.0.0.5", kept)
        self.assertNotIn("netmask 255.255.255.0", kept)
        # ...without taking the neighbour with it.
        self.assertIn("iface br0 inet manual", result)
        self.assertIn("auto br0", result)

    def test_an_allow_line_naming_others_keeps_them(self):
        """`auto eth0 usb0` must not cost usb0 its boot-time bring-up. eth0's
        is covered by the appended `allow-hotplug eth0`."""
        result = self.rewrite(SHARED_ALLOW_LINE, self.tmp)
        kept = result.split(MARKER)[0]
        self.assertIn("auto usb0", kept)
        self.assertNotIn("auto eth0 usb0", kept)
        self.assertIn("iface usb0 inet dhcp", result)

    def test_no_fixture_yields_a_duplicate_definition(self):
        """ifupdown rejects a duplicate `iface`, and the result is no
        networking at all rather than one interface short -- so this is the
        assertion that matters most on every shape of input.

        It is also the regression an intermediate version of the parser
        introduced, by reading `inet`/`dhcp` in `iface wlan0 inet dhcp` as
        interface names: the allow line went and the iface stanza stayed.
        """
        for name, fixture in (
            ("dietpi", DIETPI_INTERFACES),
            ("third-interface", THIRD_INTERFACE),
            ("bare-owned", BARE_OWNED_STANZA),
            ("shared-allow", SHARED_ALLOW_LINE),
        ):
            with self.subTest(fixture=name):
                result = self.rewrite(fixture, self.tmp)
                for interface in ("eth0", "wlan0"):
                    heads = [
                        line for line in result.splitlines()
                        if line.split()[:2] == ["iface", interface]
                    ]
                    self.assertEqual(len(heads), 1, f"{interface}: {heads}")

class ScriptWiringTests(unittest.TestCase):
    def route_metrics_hook(self) -> str:
        """The hook as the script writes it, extracted from the script."""
        match = re.search(
            r"cat > /usr/local/sbin/pi-deploy-route-metrics <<EOF\n(.*?)\nEOF\n",
            SCRIPT.read_text(), re.S,
        )
        assert match, "could not find the route-metrics heredoc"
        return match.group(1)

    def route_metrics_commands(self) -> str:
        """The hook with comment lines stripped, so an assertion about what it
        runs cannot be satisfied by a comment explaining what it avoids."""
        return "\n".join(
            line for line in self.route_metrics_hook().splitlines()
            if not line.strip().startswith("#")
        )

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

    def test_the_subnet_route_is_not_installed_with_replace(self):
        """`ip route replace` cannot do this job, and using it looks like it
        works.

        Metric is part of the route key, so replacing the kernel's automatic
        metric-0 route with a metric-100 one ADDS a second route and leaves the
        original in place. Measured on the rig after a reboot: four routes for
        the one prefix -- kernel metric-0 on eth0 AND wlan0, plus 100 and 600 --
        with the metric-0 pair outranking both of ours and tying with each
        other, so the winner was boot insertion order rather than anything this
        script configured. It picked the wire that time. Nothing made it.
        """
        # Commands only: the hook explains in a comment why it does not use
        # replace, so a substring search matches its own rationale.
        commands = self.route_metrics_commands()
        self.assertNotIn("replace", commands)
        self.assertIn("ip route del", commands)
        self.assertIn("ip route add", commands)
        # Delete must precede the add, or the add is what gets deleted.
        self.assertLess(commands.index("ip route del"), commands.index("ip route add"))

    def test_the_delete_loop_is_bounded(self):
        """It deletes until none remain because iproute2 has no selective
        delete-by-metric -- an unspecified metric and an explicit `metric 0`
        both mean "match any", so either form removes whichever route is found
        first. An unbounded loop here would hang ifup on any surprise."""
        commands = self.route_metrics_commands()
        self.assertIn("while ip route del", commands)
        self.assertRegex(commands, r"attempts.*-gt\s*\d+")
        self.assertIn("break", commands)

    def test_both_interfaces_get_their_metric(self):
        # The heredoc is unquoted, so these are still shell variables here and
        # are interpolated when the hook is written.
        commands = self.route_metrics_commands()
        self.assertIn("apply eth0 $WIRED_METRIC", commands)
        self.assertIn("apply wlan0 $WIRELESS_METRIC", commands)

    def dhclient_exit_hook(self) -> str:
        match = re.search(
            r"cat > (/etc/dhcp/dhclient-exit-hooks\.d/\S+) <<'EOF'\n(.*?)\nEOF\n",
            SCRIPT.read_text(), re.S,
        )
        assert match, "no dhclient exit hook is installed"
        self.hook_path = match.group(1)
        return match.group(2)

    def test_the_metric_is_reapplied_after_a_dhcp_lease_change(self):
        """if-up.d is run by ifup and NOT by dhclient.

        /sbin/dhclient-script handles RENEW and REBIND itself and calls
        /etc/dhcp/dhclient-exit-hooks.d; the string "if-up.d" does not appear
        in it at all. On the path where a lease returns a different address it
        runs `ip -4 addr flush dev $interface label $interface` and re-adds,
        which destroys the metric route and leaves the kernel's fresh metric-0
        one -- so the wired preference silently reverts until the next ifup.
        """
        hook = self.dhclient_exit_hook()
        self.assertIn("pi-deploy-route-metrics", hook)
        for reason in ("BOUND", "RENEW", "REBIND", "REBOOT"):
            self.assertIn(reason, hook)

    def test_the_exit_hook_never_calls_exit(self):
        """dhclient-script SOURCES these (`. $script`). An `exit` would end
        dhclient-script itself, skipping every hook after this one -- on this
        rig that includes resolved, timesyncd and rfc3442-classless-routes."""
        commands = "\n".join(
            line for line in self.dhclient_exit_hook().splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotRegex(commands, r"(?m)^\s*exit\b")

    def test_the_exit_hook_name_survives_run_parts(self):
        """run-parts ignores filenames containing a dot, so a `.sh` suffix
        would install a hook that silently never runs."""
        self.dhclient_exit_hook()
        self.assertNotIn(".", Path(self.hook_path).name)

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
