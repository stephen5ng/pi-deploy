"""Text written into a generated unit must not be executed on the way there.

The unit heredocs are unquoted because they interpolate `$name`, `$path` and
friends -- which also makes every backtick in them a command substitution run
as root. One comment reached the Pi as ``so `start` exits non-zero``, so
bootstrap printed `line 862: start: command not found` and installed a unit
whose comment had the word removed. Harmless in that instance, and an arbitrary
root command in the next one.
"""

import re
import unittest
from pathlib import Path

BOOTSTRAP = Path(__file__).parents[1] / "bootstrap.sh"
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def unquoted_heredoc_lines():
    """Yield (line number, text) for lines inside heredocs that interpolate."""
    delimiter = None
    for number, line in enumerate(BOOTSTRAP.read_text(encoding="utf-8").splitlines(), 1):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            else:
                yield number, line
            continue
        match = HEREDOC_START.search(line)
        if match and not match.group(1):
            delimiter = match.group(2)


class HeredocQuotingTests(unittest.TestCase):
    def test_no_unescaped_backtick_runs_as_a_command(self):
        offenders = [
            f"{number}: {line}"
            for number, line in unquoted_heredoc_lines()
            if re.search(r"(?<!\\)`", line)
        ]

        self.assertEqual(
            offenders,
            [],
            "unescaped backticks inside an unquoted heredoc are executed as root",
        )

    def test_the_scanner_sees_the_lines_it_is_meant_to(self):
        # Guards the test itself: a scanner that yielded nothing would pass the
        # assertion above against any bootstrap.sh at all.
        lines = list(unquoted_heredoc_lines())

        self.assertTrue(lines)
        self.assertTrue(
            any("exits non-zero" in line for _, line in lines),
            "expected the address unit's comment among the scanned lines",
        )


if __name__ == "__main__":
    unittest.main()
