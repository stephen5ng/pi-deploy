"""The hooks that put a service address back after something removed it.

Nothing here runs bootstrap.sh -- it needs root, apt and git. These assert on
the hook bodies it writes, because every failure mode in this area is silent:
the address disappears, `systemctl is-active` keeps saying the owning unit is
running, and the only visible symptom is that every client of that address --
for lexacube, every cube -- is offline.
"""

import re
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"


def heredoc(target: str) -> str:
    """The body bootstrap.sh writes to `target`, taken from the script."""
    source = BOOTSTRAP.read_text(encoding="utf-8")
    match = re.search(
        rf'cat > "{re.escape(target)}" <<EOF\n(.*?)\nEOF\n', source, re.S
    )
    assert match, f"bootstrap.sh does not write {target}"
    return match.group(1)


def uncommented(body: str) -> str:
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


class ReclaimHookTests(unittest.TestCase):
    IFUP = "$address_hook"
    DHCLIENT = "$addr_dhclient_hook"
    GUARD = "$guard_helper"

    def test_a_dhcp_lease_change_can_reclaim_the_address(self):
        """if-up.d is run by ifup and NOT by dhclient.

        /sbin/dhclient-script handles RENEW and REBIND itself and calls
        /etc/dhcp/dhclient-exit-hooks.d. On the path where a lease returns a
        different address it runs `ip -4 addr flush dev $interface label
        $interface`, and a secondary service address on that interface carries
        the interface's own label -- so the flush takes it too. Measured on the
        rig: eth0 was left with no addresses at all.

        Nothing else covers it. The ifup hook does not run, because no ifup
        happened; the failover watcher does not fire, because the carrier never
        dropped.
        """
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("/etc/dhcp/dhclient-exit-hooks.d/50-${name}-address", source)
        hook = heredoc(self.DHCLIENT)
        for reason in ("BOUND", "RENEW", "REBIND", "REBOOT"):
            self.assertIn(reason, hook)

    def test_the_dhclient_hook_never_calls_exit(self):
        """dhclient-script SOURCES these (`. $script`). An `exit` would end
        dhclient-script itself and skip every hook after it -- on this rig that
        includes the route-metric hook, resolved and timesyncd."""
        self.assertNotRegex(
            uncommented(heredoc(self.DHCLIENT)), r"(?m)^\s*exit\b"
        )

    def test_the_dhclient_hook_name_survives_run_parts(self):
        """run-parts skips filenames containing a dot, so a `.sh` suffix would
        install a hook that silently never runs."""
        source = BOOTSTRAP.read_text(encoding="utf-8")
        match = re.search(
            r'addr_dhclient_hook="/etc/dhcp/dhclient-exit-hooks\.d/(\S+)"', source
        )
        assert match
        self.assertNotIn(".", match.group(1).replace("${name}", "name"))

    def test_both_hooks_share_one_guard(self):
        """The question "does this event mean the address needs reclaiming?" is
        answered identically for both callers. Two copies would drift, and the
        drift would only show on whichever path is exercised less."""
        for caller in (self.IFUP, self.DHCLIENT):
            self.assertIn("$guard_helper", heredoc(caller))

    def test_the_guard_refuses_when_the_owner_is_stopped(self):
        """A stopped service must not have its address handed back by an
        unrelated interface event."""
        guard = heredoc(self.GUARD)
        self.assertIn("systemctl is-active --quiet $address_service", guard)

    def test_the_guard_does_not_block_its_caller(self):
        """The reclaim arpings for up to ~20s. ifup blocking on that delays
        boot; dhclient-script blocking on it stalls a lease renewal."""
        guard = heredoc(self.GUARD)
        self.assertTrue(
            "systemd-run" in guard and "setsid" in guard,
            "reclaim must be detached from the caller",
        )

    def test_removing_the_service_address_removes_both_hooks(self):
        """Left behind, the dhclient hook would keep reclaiming an address the
        app no longer owns."""
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn(
            'rm -f "/etc/dhcp/dhclient-exit-hooks.d/50-${name}-address"', source
        )
        self.assertIn(
            'rm -f "/usr/local/sbin/${name}-address-reclaim-if-missing"', source
        )


if __name__ == "__main__":
    unittest.main()
