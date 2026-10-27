"""The apps.yaml wiring that shows the high-score board on the Pi cabinet.

Nothing here runs bootstrap.sh -- it needs root, apt and git. What it checks is
the declaration bootstrap.sh acts on, because the failure is silent either way:
without the board origin the game installs the null client, which draws
nothing and prompts for nothing, and the Pi otherwise plays perfectly.

apps.yaml is read as text, per test_generated_units.py: bootstrap.sh reads it
with the `yq` CLI and the rest of this suite is stdlib-only.
"""

import re
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
CONFIG = REPOSITORY / "apps.yaml"

# The board origin. The game appends /api/leaderboard itself, and the same
# origin serves the web build the Pi shares the board with.
BOARD_ORIGIN = "https://lexacube.vercel.app"


def app_block(name: str) -> str:
    config = CONFIG.read_text(encoding="utf-8")
    start = config.index(f"- name: {name}")
    remainder = config[start + 1 :]
    end = remainder.find("\n  - name:")
    return remainder if end == -1 else remainder[:end]


class HighscoresWiringTest(unittest.TestCase):
    def setUp(self):
        self.block = app_block("lexacube")

    def test_the_game_is_told_where_the_board_lives(self):
        """The board client is opt-in by this variable's presence, so without
        it the cabinet shows no board at all -- no error, no rows."""
        self.assertIn(f"- LEXACUBE_HIGHSCORES_URL={BOARD_ORIGIN}", self.block)

    def test_the_url_is_a_bare_origin(self):
        """The game builds the endpoint as origin + /api/leaderboard, so a
        value carrying its own path would break every fetch and post."""
        match = re.search(
            r"(?m)^\s*-\s*LEXACUBE_HIGHSCORES_URL=(\S+)\s*$", self.block
        )
        self.assertIsNotNone(match, "LEXACUBE_HIGHSCORES_URL not declared")
        self.assertRegex(match.group(1), r"^https?://[^/]+$")


if __name__ == "__main__":
    unittest.main()
