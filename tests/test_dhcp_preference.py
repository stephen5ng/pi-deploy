"""Choosing which DHCP server this Pi listens to.

Two DHCP servers answer on one bridged segment here, and dhclient takes
whichever OFFER lands first. The Pi kept losing and sitting on the house
subnet, with no route to the cubes or to the broker's service address, while
its SSID and signal looked perfect -- which is why this is configuration and
not a diagnosis anyone enjoys repeating.

The script only edits a file, so it is exercised for real on fixtures.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).parents[1]
SCRIPT = REPOSITORY / "scripts" / "dhcp-preference.sh"
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
CONFIG = REPOSITORY / "apps.yaml"

# Trimmed from Debian's stock file: the directives that must survive.
STOCK = """\
option rfc3442-classless-static-routes code 121 = array of unsigned integer 8;
send host-name = gethostname();
request subnet-mask, broadcast-address, routers, domain-name-servers;
#reject 192.33.137.209;
"""


class Harness:
    def make_conf(self, text=STOCK):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "dhclient.conf"
        path.write_text(text)
        return path

    def invoke(self, path, *servers):
        """Run the script against a fixture. Returns the CompletedProcess."""
        env = dict(os.environ, DHCLIENT_CONF=str(path))
        return subprocess.run(
            ["bash", str(SCRIPT), *servers], text=True, capture_output=True, env=env
        )

    def rejects(self, path):
        return re.findall(r"(?m)^reject (\S+);", path.read_text())


class DhcpPreferenceTests(Harness, unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)

    def test_it_rejects_the_named_server(self):
        conf = self.make_conf()
        done = self.invoke(conf, "192.168.0.1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("192.168.0.1", self.rejects(conf))

    def test_the_distro_directives_survive(self):
        # Losing `send host-name` or the request list breaks DHCP differently.
        conf = self.make_conf()
        self.invoke(conf, "192.168.0.1")
        text = conf.read_text()
        self.assertIn("send host-name = gethostname();", text)
        self.assertIn("rfc3442-classless-static-routes", text)

    def test_rerunning_does_not_accumulate(self):
        # bootstrap.sh is re-run routinely; an appending block would grow forever.
        conf = self.make_conf()
        for _ in range(3):
            self.invoke(conf, "192.168.0.1")
        self.assertEqual(self.rejects(conf), ["192.168.0.1"])

    def test_a_changed_list_replaces_the_old_one(self):
        # A stale entry means the Pi keeps ignoring a server it now needs.
        conf = self.make_conf()
        self.invoke(conf, "192.168.0.1")
        self.invoke(conf, "10.0.0.1")
        self.assertEqual(self.rejects(conf), ["10.0.0.1"])

    def test_an_empty_list_removes_the_block(self):
        conf = self.make_conf()
        self.invoke(conf, "192.168.0.1")
        self.invoke(conf)
        self.assertEqual(self.rejects(conf), [])
        self.assertIn("send host-name = gethostname();", conf.read_text())

    def test_a_non_address_is_refused_and_the_file_is_untouched(self):
        # A malformed directive makes dhclient exit at startup. On a WiFi-only
        # box that is no network and no way back in -- worse than losing a race.
        conf = self.make_conf()
        done = self.invoke(conf, "not-an-ip")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(conf.read_text(), STOCK, "the file must not be modified")

    def test_an_out_of_range_octet_is_refused(self):
        # A regex loose enough to read accepts 192.168.0.256, and dhclient then
        # refuses the whole file: "line 1: 256 exceeds max (255) for precision"
        # (verified against ISC 4.4.3-P1). That is the no-network failure this
        # check exists to prevent, so it must not be the one it lets through.
        for bad in ("192.168.0.256", "999.1.1.1"):
            with self.subTest(address=bad):
                conf = self.make_conf()
                done = self.invoke(conf, bad)
                self.assertNotEqual(done.returncode, 0, f"{bad} was accepted")
                self.assertEqual(conf.read_text(), STOCK)

    def test_ambiguous_forms_are_refused(self):
        # Leading zeros are read as octal by some parsers and decimal by others,
        # and a short form expands unpredictably. Neither belongs in a file whose
        # only job is naming one specific server.
        for bad in ("010.1.1.1", "1.2.3"):
            with self.subTest(address=bad):
                conf = self.make_conf()
                done = self.invoke(conf, bad)
                self.assertNotEqual(done.returncode, 0, f"{bad} was accepted")
                self.assertEqual(conf.read_text(), STOCK)

    def test_a_bad_entry_does_not_write_the_good_ones_first(self):
        # Validation happens before any write, so a partial list cannot land.
        conf = self.make_conf()
        self.invoke(conf, "192.168.0.1", "nonsense")
        self.assertEqual(conf.read_text(), STOCK)

    def test_it_does_not_re_lease(self):
        # bootstrap often runs over SSH on the interface that would drop, and a
        # lease change mid-run takes the session and the remaining steps with it.
        source = "\n" + SCRIPT.read_text(encoding="utf-8")
        for command in ("\ndhclient", "\nifdown", "\nifup", "\nsystemctl restart"):
            self.assertNotIn(
                command, source, f"{command.strip()} would re-lease mid-bootstrap"
            )


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOOTSTRAP.read_text(encoding="utf-8")

    def test_bootstrap_runs_it_behind_an_existence_guard(self):
        self.assertIn(
            'if [[ -f "$SCRIPT_DIR/scripts/dhcp-preference.sh" ]]; then', self.source
        )

    def test_it_runs_before_the_wifi_preference_step(self):
        dhcp = self.source.find('bash "$SCRIPT_DIR/scripts/dhcp-preference.sh"')
        wifi = self.source.find('bash "$SCRIPT_DIR/scripts/wifi-preference.sh"')
        self.assertNotEqual(dhcp, -1, "bootstrap must invoke dhcp-preference.sh")
        self.assertNotEqual(wifi, -1, "bootstrap must invoke wifi-preference.sh")
        self.assertLess(dhcp, wifi, "a WiFi drop must not be able to skip it")

    def test_the_config_supplies_the_house_server(self):
        config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        servers = (config.get("dhcp") or {}).get("reject_servers", [])
        self.assertIn("192.168.0.1", servers)


if __name__ == "__main__":
    unittest.main()
