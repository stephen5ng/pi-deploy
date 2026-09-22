"""uv must exist before anything asks for it, on either venv branch.

apps.yaml's install_python_cmd spells out `uv pip install ...` for the
rgbmatrix and platformio dependencies. The venv setup has two branches -- a
committed uv.lock gets `uv sync`, otherwise requirements.txt gets
`pip install -r` -- and the "install uv if missing" block used to live inside
the lock branch only.

So an app on the requirements branch got neither uv nor its PATH entry, and
the run died at the dependency step with `uv: command not found`. Measured on
the rig right after lexacube moved branches: uv was present at
/root/.local/bin from an earlier run and the run still aborted, because only
the lock branch ever exported that directory onto PATH.

The fix hoists the block above the branch. These tests pin the ordering, which
is the whole content of the fix.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = REPOSITORY / "bootstrap.sh"
SOURCE = BOOTSTRAP.read_text(encoding="utf-8")


def _pos(pattern, label):
    matches = list(re.finditer(pattern, SOURCE, re.M))
    assert matches, f"pattern not found: {label}"  # guards every ordering assert
    return matches


class UvAvailabilityTests(unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=True, capture_output=True)

    def test_uv_is_ensured_exactly_once(self):
        # A second copy would drift from the first, as this one drifted from
        # being in the wrong place at all.
        ensures = _pos(
            r"^    if ! command -v uv &> /dev/null; then", "uv ensure"
        )
        self.assertEqual(len(ensures), 1)

    def test_the_ensure_precedes_both_branches_and_the_dependency_loop(self):
        ensure = _pos(
            r"^    if ! command -v uv &> /dev/null; then", "uv ensure"
        )[0].start()
        for pattern, label in [
            (r"^    if \[\[ -f \"\$path/uv\.lock\" \]\]; then", "lock branch"),
            (r"pip install -r \"\$path/requirements\.txt\"", "requirements branch"),
            (r"dep_python_cmd=\$\(yq", "dependency install_python_cmd eval"),
        ]:
            with self.subTest(step=label):
                use = _pos(pattern, label)[0].start()
                self.assertLess(ensure, use, f"uv must be ensured before {label}")

    def test_the_path_export_rides_with_the_ensure(self):
        # The installer writes to ~/.local/bin; without the export the binary
        # exists and is still unreachable -- the exact state the rig died in.
        ensure = _pos(r"^    export PATH=\"\$HOME/\.local/bin:\$PATH\"", "PATH export")
        self.assertEqual(len(ensure), 1)


if __name__ == "__main__":
    unittest.main()
