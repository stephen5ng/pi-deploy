import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "render_dietpi_provisioning.py"
)
SPEC = importlib.util.spec_from_file_location("render_dietpi_provisioning", SCRIPT_PATH)
assert SPEC and SPEC.loader
provisioning = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provisioning)


DIETPI_TEMPLATE = """\
AUTO_SETUP_GLOBAL_PASSWORD=dietpi
AUTO_SETUP_TIMEZONE=UTC
AUTO_SETUP_NET_ETHERNET_ENABLED=1
AUTO_SETUP_NET_WIFI_ENABLED=0
AUTO_SETUP_NET_WIFI_COUNTRY_CODE=GB
AUTO_SETUP_NET_HOSTNAME=DietPi
AUTO_SETUP_HEADLESS=0
AUTO_SETUP_CUSTOM_SCRIPT_EXEC=0
#AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 placeholder
AUTO_SETUP_AUTOMATED=0
SOFTWARE_DISABLE_SSH_PASSWORD_LOGINS=0
"""
WIFI_TEMPLATE = """\
aWIFI_SSID[0]=''
aWIFI_KEY[0]=''
aWIFI_SSID[1]=''
aWIFI_KEY[1]=''
"""


class RenderDietPiProvisioningTests(unittest.TestCase):
    def test_renders_unattended_wifi_setup_without_committing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "provisioning.env"
            dietpi_template = root / "dietpi.template.txt"
            wifi_template = root / "dietpi-wifi.template.txt"
            public_key = root / "id_ed25519.pub"
            output = root / "rendered"

            env.write_text(
                "WIFI_SSID='LEXA CUBE'\n"
                r"WIFI_PASSWORD='game'\''night'"
                "\n"
                "WIFI_COUNTRY='US'\n"
                "DIETPI_PASSWORD='temporary-password'\n"
                "DIETPI_HOSTNAME='lexacube'\n"
                "DIETPI_TIMEZONE='America/Los_Angeles'\n"
                f"SSH_PUBLIC_KEY_FILE='{public_key}'\n",
                encoding="utf-8",
            )
            env.chmod(0o600)
            dietpi_template.write_text(DIETPI_TEMPLATE, encoding="utf-8")
            wifi_template.write_text(WIFI_TEMPLATE, encoding="utf-8")
            public_key.write_text(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest lexacube\n",
                encoding="utf-8",
            )

            provisioning.render_files(
                env, dietpi_template, wifi_template, output
            )

            dietpi = (output / "dietpi.txt").read_text(encoding="utf-8")
            wifi = (output / "dietpi-wifi.txt").read_text(encoding="utf-8")
            self.assertIn("AUTO_SETUP_AUTOMATED=1", dietpi)
            self.assertIn("AUTO_SETUP_NET_WIFI_ENABLED=1", dietpi)
            self.assertIn("AUTO_SETUP_NET_HOSTNAME=lexacube", dietpi)
            self.assertIn("SOFTWARE_DISABLE_SSH_PASSWORD_LOGINS=1", dietpi)
            self.assertIn("AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 ", dietpi)
            self.assertIn("aWIFI_SSID[0]='LEXA CUBE'", wifi)
            self.assertIn(r"aWIFI_KEY[0]='game'\''night'", wifi)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((output / "dietpi-wifi.txt").stat().st_mode), 0o600
            )

    def test_requires_non_default_password(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "password",
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": "short",
        }

        with self.assertRaisesRegex(ValueError, "8-100 bytes"):
            provisioning.validate(values)

    def test_rejects_invalid_country_code(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "password",
            "WIFI_COUNTRY": "USA",
            "DIETPI_PASSWORD": "temporary-password",
        }

        with self.assertRaisesRegex(ValueError, "two-letter"):
            provisioning.validate(values)

    def test_accepts_64_character_hexadecimal_wpa_psk(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "0123456789abcdef" * 4,
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": "temporary-password",
        }

        provisioning.validate(values)

    def test_rejects_short_wpa_passphrase(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "short",
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": "temporary-password",
        }

        with self.assertRaisesRegex(ValueError, "8-63 characters"):
            provisioning.validate(values)

    def test_rejects_non_hexadecimal_64_character_key(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "z" * 64,
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": "temporary-password",
        }

        with self.assertRaisesRegex(ValueError, "hexadecimal WPA PSK"):
            provisioning.validate(values)


if __name__ == "__main__":
    unittest.main()
