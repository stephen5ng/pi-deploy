"""The apps.yaml wiring that gives the Pi cabinet its own high-score board.

Nothing here runs bootstrap.sh -- it needs root, apt and git. What it checks is
the declaration bootstrap.sh acts on, because the failure is silent either way:
without the board directory the game installs the null client, which draws
nothing and prompts for nothing, and the Pi otherwise plays perfectly.

apps.yaml is read as text, per test_generated_units.py: bootstrap.sh reads it
with the `yq` CLI and the rest of this suite is stdlib-only.
"""

import re
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
CONFIG = REPOSITORY / "apps.yaml"

# The cabinet's board directory. A local JSON file lives under it; the game
# creates it, like the analytics outbox. Unrelated to the online leaderboard
# by decision (lexacube docs/pi-high-scores-design.md).
BOARD_DIR = "/var/lib/lexacube/highscores"


def app_block(name: str) -> str:
    config = CONFIG.read_text(encoding="utf-8")
    start = config.index(f"- name: {name}")
    remainder = config[start + 1 :]
    end = remainder.find("\n  - name:")
    return remainder if end == -1 else remainder[:end]


class HighscoresWiringTest(unittest.TestCase):
    def setUp(self):
        self.block = app_block("lexacube")

    def test_the_game_is_told_where_its_board_lives(self):
        """The local board is opt-in by this variable's presence, so without
        it the cabinet shows no board at all -- no error, no rows."""
        self.assertIn(f"- LEXACUBE_HIGHSCORES_DIR={BOARD_DIR}", self.block)

    def test_the_dir_is_absolute(self):
        """A relative directory would land under the service's WorkingDirectory
        (/opt/lexacube) instead of /var/lib, scattering cabinet state across
        the checkout that deploys wipe and replace."""
        match = re.search(
            r"(?m)^\s*-\s*LEXACUBE_HIGHSCORES_DIR=(\S+)\s*$", self.block
        )
        self.assertIsNotNone(match, "LEXACUBE_HIGHSCORES_DIR not declared")
        self.assertTrue(match.group(1).startswith("/"))


if __name__ == "__main__":
    unittest.main()
