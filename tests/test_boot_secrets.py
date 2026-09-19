"""Staged first-boot secrets must not survive on the FAT boot partition."""

import unittest
from pathlib import Path


BOOTSTRAP = Path(__file__).parents[1] / "bootstrap.sh"


class BootSecretTests(unittest.TestCase):
    def test_app_env_files_are_consumed_before_app_processing(self):
        source = BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn("consume_staged_app_env()", source)
        self.assertIn("install -D -m 600 \"$boot_env\" \"$destination\"", source)
        self.assertIn("rm -f \"$boot_env\"", source)
        self.assertIn("yq -r '.apps[].name' \"$CONFIG\"", source)
        self.assertLess(
            source.index("while IFS= read -r staged_app_name"),
            source.index("for ((app_idx=0; app_idx<app_count; app_idx++))"),
        )


if __name__ == "__main__":
    unittest.main()
