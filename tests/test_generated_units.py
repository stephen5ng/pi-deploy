"""The systemd unit bootstrap.sh generates, rendered through bash itself.

bootstrap.sh needs root, apt and git, so it cannot run in a test. The unit
template inside it can: this extracts the real heredoc from the real script and
renders it with the real shell, so a change to the template is a change to what
these tests see.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
CONFIG = REPOSITORY / "apps.yaml"

HEREDOC_START = 'cat > "/etc/systemd/system/${name}.service" <<EOF'
# The block that turns apps.yaml values into unit directives. Extracted and run
# alongside the template so the tests cover how directives are built, not only
# where they are placed.
DERIVATION_START = 'requires_unit=""'
DERIVATION_END = 'bound_unit=""'

# The apps.yaml-derived inputs, as bootstrap.sh reads them. Anything the script
# gains must be added here or the render fails on `set -u` -- which is the
# point: a new directive cannot go untested.
DEFAULTS = {
    "name": "testapp",
    "after": "network.target",
    "requires_units": "",
    "start_limit_interval": "",
    "start_limit_burst": "",
    "address_unit": "",
    "bound_unit": "",
    "service_user": "dietpi",
    "path": "/opt/testapp",
    "env_file_line": "",
    "exec": "/usr/bin/true",
    "env_lines": "",
    "install_target": "multi-user.target",
}


def _slice(start_marker: str, end_marker: str, *, keep_start: bool) -> str:
    """The lines between two markers. Both markers are matched on the stripped
    line, and the end marker is always excluded."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == start_marker
    )
    end = next(
        index
        for index, line in enumerate(lines)
        if index > start and line.strip() == end_marker
    )
    return "\n".join(lines[start if keep_start else start + 1 : end])


def unit_template() -> str:
    # The `cat >` line itself would redirect to a real path; the test supplies
    # its own `cat <<EOF` instead.
    return _slice(HEREDOC_START, "EOF", keep_start=False)


def derivation() -> str:
    # Starts at `requires_unit=""`, which the derivation needs as its own
    # initialisation -- without it the render trips `set -u`.
    return _slice(DERIVATION_START, DERIVATION_END, keep_start=True)


def render(**overrides: str) -> str:
    values = {**DEFAULTS, **overrides}
    assignments = "\n".join(
        f"{key}={_quote(value)}" for key, value in values.items()
    )
    script = (
        f"set -u\n{assignments}\n{derivation()}\n"
        f"cat <<EOF\n{unit_template()}\nEOF\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return result.stdout


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def app_block(name: str) -> str:
    """The apps.yaml text for one app.

    Read as text rather than parsed: bootstrap.sh reads this file with the `yq`
    CLI, and every other test in this repo is stdlib-only. Adding pyyaml just
    to assert on two scalars would put a dependency in the test suite that the
    thing under test does not have.
    """
    config = CONFIG.read_text(encoding="utf-8")
    start = config.index(f"- name: {name}")
    remainder = config[start + 1 :]
    end = remainder.find("\n  - name:")
    return remainder if end == -1 else remainder[:end]


def scalar(block: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", block, re.MULTILINE)
    assert match, f"{key} not found"
    return match.group(1).strip('"')


def directives(unit: str, section: str) -> list[str]:
    """The non-blank lines of one section, so blank-line noise from an unset
    optional variable never masks a missing directive."""
    body = unit.split(f"[{section}]", 1)[1]
    body = re.split(r"^\[", body, maxsplit=1, flags=re.MULTILINE)[0]
    return [line for line in body.splitlines() if line.strip()]


class GeneratedUnitTests(unittest.TestCase):
    def test_optional_directives_are_absent_when_not_configured(self):
        unit = directives(render(), "Unit")
        self.assertEqual(unit, ["Description=testapp service", "After=network.target"])

    def test_required_units_and_start_limit_land_in_the_unit_section(self):
        # StartLimit* are [Unit] directives, not [Service] ones. Put them in
        # [Service] and systemd ignores them, so the retry loop this exists to
        # stop would carry on with the config looking correct.
        unit = directives(
            render(
                requires_units="mosquitto.service",
                start_limit_interval="300",
                start_limit_burst="5",
            ),
            "Unit",
        )
        self.assertIn("Requires=mosquitto.service", unit)
        self.assertIn("StartLimitIntervalSec=300", unit)
        self.assertIn("StartLimitBurst=5", unit)
        # PartOf= is what makes a dependency *restart* propagate. Requires=
        # alone only propagates stop, so a routine `systemctl restart
        # mosquitto` would leave this app stopped for good.
        self.assertIn("PartOf=mosquitto.service", unit)
        self.assertNotIn(
            "Requires=mosquitto.service", directives(render(), "Service")
        )

    def test_restart_settings_still_ship(self):
        service = directives(render(), "Service")
        self.assertIn("Restart=on-failure", service)
        self.assertIn("RestartSec=5", service)


class NfcControlConfigurationTests(unittest.TestCase):
    """nfc-control exits immediately when the broker is unreachable, so its
    unit is what decides between 'stops and says so' and 'restarts forever'."""

    def test_nfc_control_requires_the_broker_and_caps_its_restarts(self):
        block = app_block("nfc-control")
        self.assertIn("- mosquitto.service", block)
        self.assertIn("mosquitto.service", scalar(block, "after"))
        self.assertEqual(scalar(block, "burst"), "5")
        # Must exceed burst * RestartSec (5 * 5s), or the window closes between
        # attempts and the counter never reaches the burst -- the exact defect
        # in systemd's 10s default that let this restart indefinitely.
        self.assertGreater(int(scalar(block, "interval_sec")), 5 * 5)

    def test_the_generated_nfc_control_unit_stops_after_the_burst(self):
        block = app_block("nfc-control")
        interval = scalar(block, "interval_sec")
        unit = directives(
            render(
                name="nfc-control",
                after=scalar(block, "after"),
                requires_units="mosquitto.service",
                start_limit_interval=interval,
                start_limit_burst=scalar(block, "burst"),
                bound_unit="PartOf=lexacube.service\nAfter=lexacube.service",
                install_target="lexacube.service",
            ),
            "Unit",
        )
        self.assertIn("Requires=mosquitto.service", unit)
        self.assertIn(f"StartLimitIntervalSec={interval}", unit)
        # Both parents propagate: the game it follows, and the broker it cannot
        # run without.
        self.assertIn("PartOf=mosquitto.service", unit)
        self.assertIn("PartOf=lexacube.service", unit)


if __name__ == "__main__":
    unittest.main()
