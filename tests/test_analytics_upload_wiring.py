"""The apps.yaml wiring that makes gameplay analytics reach PostHog.

Nothing here runs bootstrap.sh -- it needs root, apt and git. What it checks is
the declaration bootstrap.sh acts on, because every mistake available in this
wiring is silent: a Pi that plays fine and records nothing, or records to a
directory the uploader never looks in. Either way it is discovered when
someone asks why there is no data from an event that has already happened.

apps.yaml is read as text, per test_generated_units.py: bootstrap.sh reads it
with the `yq` CLI and the rest of this suite is stdlib-only.
"""

import re
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
CONFIG = REPOSITORY / "apps.yaml"
TEMPLATE = REPOSITORY / "lexacube.env.template.txt"

# The outbox. The game writes it (apps.yaml environment, here) and the uploader
# drains it (lexacube-analytics-upload.service, in the cubes repo). They are
# configured in two repositories and agree only by this literal.
OUTBOX = "/var/lib/lexacube/analytics"

UNIT = "deploy/lexacube-analytics-upload.service"
TIMER = "deploy/lexacube-analytics-upload.timer"


def app_block(name: str) -> str:
    config = CONFIG.read_text(encoding="utf-8")
    start = config.index(f"- name: {name}")
    remainder = config[start + 1 :]
    end = remainder.find("\n  - name:")
    return remainder if end == -1 else remainder[:end]


def extra_units(block: str) -> dict[str, bool]:
    """Each declared extra unit mapped to whether bootstrap enables it.

    Mirrors bootstrap.sh's own reading: an entry may be a bare string (never
    enabled) or a mapping with an optional `enable`.
    """
    section = block.split("extra_units:", 1)[1]
    # Stop at the next key at the same indentation as `extra_units:`.
    section = re.split(r"^    \w", section, maxsplit=1, flags=re.MULTILINE)[0]
    units: dict[str, bool] = {}
    current = None
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if match := re.match(r"-\s*source:\s*(\S+)", stripped):
            current = match.group(1)
            units[current] = False
        elif match := re.match(r"-\s*(\S+)", stripped):
            current = match.group(1)
            units[current] = False
        elif match := re.match(r"enable:\s*(\S+)", stripped):
            assert current is not None
            units[current] = match.group(1) == "true"
    return units


class AnalyticsWiringTest(unittest.TestCase):
    def setUp(self):
        self.block = app_block("lexacube")

    def test_the_game_is_told_where_to_write_its_outbox(self):
        """Analytics are opt-in by this variable's presence, so without it the
        Pi plays normally and records nothing at all -- no error, no data."""
        self.assertIn(f"- LEXACUBE_ANALYTICS_DIR={OUTBOX}", self.block)

    def test_both_units_are_declared(self):
        """bootstrap.sh installs only what is declared, and the timer is inert
        without the service it activates."""
        units = extra_units(self.block)
        self.assertIn(UNIT, units)
        self.assertIn(TIMER, units)

    def test_the_timer_is_enabled_and_the_oneshot_is_not(self):
        """The service is a Type=oneshot with no [Install] section: the timer
        is what triggers it. Enabling the service as well has nothing to link,
        and would duplicate every run if it ever gained an [Install]."""
        units = extra_units(self.block)
        self.assertIs(units[TIMER], True)
        self.assertIs(units[UNIT], False)

    def test_the_admin_page_stays_enabled(self):
        """The parser above is easy to get wrong in a way that silently drops
        an entry, and a dropped `enable` here is an admin page that no longer
        starts at boot."""
        units = extra_units(self.block)
        self.assertIs(units["deploy/lexacube-admin.service"], True)

    def test_the_template_names_what_the_uploader_requires(self):
        """The uploader exits 2 on either of these being unset. A template
        that does not mention one is a reflash that silently cannot upload."""
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertRegex(template, r"(?m)^POSTHOG_API_KEY=")
        self.assertRegex(template, r"(?m)^ANALYTICS_PRODUCT=")

    def test_the_product_is_filled_in_but_the_key_is_blank(self):
        """Opposite rules for the two. ANALYTICS_PRODUCT has a correct
        committable value and is the field whose being wrong corrupts a shared
        vendor namespace, so it ships filled in. The key is a secret, so a
        committed default would be someone's real key in git history."""
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertRegex(template, r"(?m)^ANALYTICS_PRODUCT=lexacube\s*$")
        self.assertRegex(template, r"(?m)^POSTHOG_API_KEY=\s*$")


if __name__ == "__main__":
    unittest.main()
