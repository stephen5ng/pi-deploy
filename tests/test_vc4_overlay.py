"""Enabling the KMS display driver, and noticing when it did not get enabled.

DietPi's config.txt template ships the overlay commented out:

    # Enable KMS/DRM display driver, recommended when a GUI application ...
    #dtoverlay=vc4-kms-v3d,noaudio

An unanchored `grep -q "dtoverlay=vc4-kms-v3d"` matches that line, so bootstrap
reported "VC4 KMS overlay already configured" on a machine where KMS had never
been enabled. Measured on the rig: the commented line at config.txt:88, no
/dev/dri at all, and the game's SDL kmsdrm backend with nothing to open --
while every bootstrap run printed success.

The block is run for real against fixtures. Its whole failure mode was a
mistaken belief about a file, so a test that reads the source and believes the
same thing would be no test.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
SOURCE = BOOTSTRAP.read_text(encoding="utf-8")

# The VC4 block, lifted whole so the test exercises what bootstrap runs.
# Anchored on the closing branch rather than the first `fi`: the block opens
# with a nested `if`, and a non-greedy match to `^fi$` silently captures four
# lines of it and passes a test that has stopped testing anything.
BLOCK = re.search(
    r"^VC4_CONFIG_FILE=.*?already configured.*?^fi$", SOURCE, re.M | re.S
)

DIETPI_TEMPLATE = """\
# DietPi config.txt
#-------Display---------
disable_overscan=1

# Enable KMS/DRM display driver, recommended when a GUI application is used.
# Remove ",noaudio" if HDMI audio is needed, or enable it via dietpi-config.
#dtoverlay=vc4-kms-v3d,noaudio
arm_64bit=1
"""


class Harness:
    def run_block(self, config_text):
        self.assertIsNotNone(BLOCK, "VC4 block not found in bootstrap.sh")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = tmp / "config.txt"
        config.write_text(config_text)
        done = subprocess.run(
            ["bash", "-s"],
            input="set -euo pipefail\n" + BLOCK.group(0) + "\n",
            text=True, capture_output=True,
            env={**os.environ, "VC4_CONFIG_FILE": str(config)},
        )
        return done, config.read_text()

    def active_overlays(self, text):
        return re.findall(r"(?m)^[ \t]*dtoverlay=vc4-kms-v3d.*$", text)


class Vc4OverlayTests(Harness, unittest.TestCase):
    def test_the_whole_block_was_captured(self):
        # Guards every test below: a short capture runs harmless fragments and
        # reports nothing, which looks exactly like success.
        self.assertIsNotNone(BLOCK, "VC4 block not found in bootstrap.sh")
        body = BLOCK.group(0)
        for needed in ("Adding VC4 KMS overlay", "already configured",
                       "dtoverlay=vc4-kms-v3d"):
            self.assertIn(needed, body)

    def test_a_commented_overlay_does_not_count_as_configured(self):
        # The bug, exactly: the only vc4 line is commented out.
        done, text = self.run_block(DIETPI_TEMPLATE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("already configured", done.stdout)
        self.assertEqual(len(self.active_overlays(text)), 1, text)

    def test_the_dietpi_comment_is_left_alone(self):
        # It documents the ",noaudio" choice; rewriting it is not this job.
        _, text = self.run_block(DIETPI_TEMPLATE)
        self.assertIn("#dtoverlay=vc4-kms-v3d,noaudio", text)

    def test_an_active_overlay_is_left_alone(self):
        config = DIETPI_TEMPLATE + "dtoverlay=vc4-kms-v3d\n"
        done, text = self.run_block(config)
        self.assertIn("already configured", done.stdout)
        self.assertEqual(text, config, "an already-configured file was rewritten")

    def test_it_is_idempotent(self):
        # bootstrap is re-run routinely; a second overlay line is at best noise.
        _, once = self.run_block(DIETPI_TEMPLATE)
        _, twice = self.run_block(once)
        self.assertEqual(len(self.active_overlays(twice)), 1, twice)

    def test_a_file_without_the_dietpi_marker_still_gets_the_overlay(self):
        # The old sed keyed on "#-------Display---------" and wrote nothing when
        # it was absent -- while still reporting that it had added the overlay.
        done, text = self.run_block("arm_64bit=1\n")
        self.assertEqual(len(self.active_overlays(text)), 1, text)
        self.assertNotIn("WARNING", done.stderr)

    def test_a_failed_write_is_reported_rather_than_claimed(self):
        # Read the result back out of the file: the previous code reported
        # success from a command that exits 0 whether or not it wrote anything.
        self.assertIn("could not add the VC4 KMS overlay", SOURCE)
        added = SOURCE.find("Adding VC4 KMS overlay")
        warned = SOURCE.find("could not add the VC4 KMS overlay")
        self.assertLess(added, warned)


if __name__ == "__main__":
    unittest.main()
