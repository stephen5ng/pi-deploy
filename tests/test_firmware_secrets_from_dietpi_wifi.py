import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "firmware_secrets_from_dietpi_wifi.py"
)
SPEC = importlib.util.spec_from_file_location("firmware_secrets", SCRIPT_PATH)
assert SPEC and SPEC.loader
firmware_secrets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firmware_secrets)


class FirmwareSecretsTests(unittest.TestCase):
    def test_reads_first_configured_profile_and_shell_escaped_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "dietpi-wifi.txt"
            profile.write_text(
                "aWIFI_SSID[0]=''\n"
                "aWIFI_KEY[0]=''\n"
                "aWIFI_SSID[1]='LEXA CUBE'\n"
                r"aWIFI_KEY[1]='game'\''night'"
                "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                firmware_secrets.read_wifi_profile(profile),
                ("LEXA CUBE", "game'night"),
            )

    def test_finds_later_candidate_and_renders_both_firmware_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            profile = root / "dietpi-wifi.txt"
            output = root / "secrets.h"
            profile.write_text(
                "aWIFI_SSID[0]='LEXA\"CUBE'\n"
                r"aWIFI_KEY[0]='back\slash'"
                "\n",
                encoding="utf-8",
            )

            ssid, key, source = firmware_secrets.find_wifi_profile([missing, profile])
            created = firmware_secrets.create_header(
                output, firmware_secrets.render_header(ssid, key)
            )

            self.assertTrue(created)
            self.assertEqual(source, profile)
            content = output.read_text(encoding="utf-8")
            self.assertIn('#define SSID_NAME "LEXA\\"CUBE"', content)
            self.assertIn('#define SSID_NAME_PORTABLE "LEXA\\"CUBE"', content)
            self.assertIn('#define WIFI_PASSWORD "back\\\\slash"', content)
            self.assertIn(
                '#define WIFI_PASSWORD_PORTABLE "back\\\\slash"', content
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_existing_override_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "secrets.h"
            output.write_text("custom\n", encoding="utf-8")

            created = firmware_secrets.create_header(output, "generated\n")

            self.assertFalse(created)
            self.assertEqual(output.read_text(encoding="utf-8"), "custom\n")

    def test_interrupted_write_does_not_leave_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "secrets.h"

            with mock.patch.object(
                firmware_secrets.os, "fsync", side_effect=OSError("interrupted")
            ):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    firmware_secrets.create_header(output, "generated\n")

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".secrets.h.*")), [])

    def test_accepts_64_character_hexadecimal_wpa_psk(self):
        psk = "0123456789abcdef" * 4

        content = firmware_secrets.render_header("LEXACUBE", psk)

        self.assertIn(f'#define WIFI_PASSWORD "{psk}"', content)

    def test_rejects_non_hexadecimal_64_character_key(self):
        with self.assertRaisesRegex(ValueError, "hexadecimal WPA PSK"):
            firmware_secrets.render_header("LEXACUBE", "z" * 64)


if __name__ == "__main__":
    unittest.main()
