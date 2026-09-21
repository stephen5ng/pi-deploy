"""Regression checks for Knockstrip's configuration-driven Pi deployment."""

import re
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
CONFIG = (REPOSITORY / "apps.yaml").read_text(encoding="utf-8")
BOOTSTRAP = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")
FIRST_BOOT = (
    REPOSITORY / "scripts" / "Automation_Custom_Script.sh"
).read_text(encoding="utf-8")


def app_block(name: str) -> str:
    start = CONFIG.index(f"- name: {name}")
    remainder = CONFIG[start + 1 :]
    end = remainder.find("\n  - name:")
    return remainder if end == -1 else remainder[:end]


class KnockstripDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.app = app_block("knockstrip")

    def test_knockstrip_is_a_configured_app(self):
        self.assertIn('repo: "https://github.com/stephen5ng/knockstrip"', self.app)
        self.assertIn("path: /home/dietpi/knockstrip", self.app)
        self.assertIn("exclusive_group: game", self.app)

    def test_repo_owned_units_are_declared(self):
        # Knockstrip's unit has hardware-specific pre-start work, so bootstrap
        # must install it verbatim instead of generating a lossy replacement.
        self.assertIn("unit_source: ops/knockstrip.service", self.app)
        self.assertIn("- ops/knockstrip-preflight.service", self.app)
        self.assertIn('install -m 644 "$path/$unit_source"', BOOTSTRAP)
        self.assertIn('install -m 644 "$path/$extra_unit"', BOOTSTRAP)

    def test_uv_environment_and_hardware_dependencies_are_selected(self):
        # Knockstrip commits uv.lock and puts Pi-only rpi-ws281x in an extra;
        # --all-extras is required for the actual LED hardware backend.
        #
        # Matched per-flag rather than as one literal command: this used to pin
        # the whole string, so adding --inexact to stop sync uninstalling the
        # rest of an app's environment failed a test that has no opinion about
        # that flag. The requirement is that every sync selects the extras.
        syncs = re.findall(r"uv sync ([^)\n]*)", BOOTSTRAP)
        self.assertTrue(syncs, "no `uv sync` found in bootstrap.sh")
        for flags in syncs:
            self.assertIn("--all-extras", flags)
        self.assertRegex(self.app, r"(?m)^\s*- libportaudio2$")

    def test_rig_mapping_is_deployed_without_a_local_git_commit(self):
        self.assertRegex(self.app, r"(?m)^\s*- path: station_ids\.yaml$")
        self.assertRegex(self.app, r"(?m)^\s*- path: config\.local\.yaml$")
        self.assertIn('rig_files paths must be relative without', BOOTSTRAP)

    def test_first_boot_does_not_limit_the_selection_to_lexacube(self):
        # App selection is intentionally additive for maintenance runs, but
        # the SSD's first boot must provision every configured application.
        self.assertIn("exec ./bootstrap.sh\n", FIRST_BOOT)
        self.assertNotIn("exec ./bootstrap.sh lexacube", FIRST_BOOT)


if __name__ == "__main__":
    unittest.main()
