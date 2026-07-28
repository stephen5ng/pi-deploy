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

    def test_rejects_unedited_example_placeholders(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "replace-me",
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": "replace-with-a-unique-password",
        }

        with self.assertRaisesRegex(ValueError, "WIFI_PASSWORD still contains"):
            provisioning.validate(values)

        values["WIFI_PASSWORD"] = "game-night"
        with self.assertRaisesRegex(ValueError, "DIETPI_PASSWORD still contains"):
            provisioning.validate(values)

    def test_rejects_password_characters_that_would_corrupt_dietpi_txt(self):
        values = {
            "WIFI_SSID": "LEXACUBE",
            "WIFI_PASSWORD": "game-night",
            "WIFI_COUNTRY": "US",
            "DIETPI_PASSWORD": 'quote"and$dollar',
        }

        with self.assertRaisesRegex(ValueError, "DietPi warns against"):
            provisioning.validate(values)

    def test_renders_zai_key_only_when_provided(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "provisioning.env"
            dietpi_template = root / "dietpi.template.txt"
            wifi_template = root / "dietpi-wifi.template.txt"
            base_env = (
                "WIFI_SSID='LEXACUBE'\n"
                "WIFI_PASSWORD='game-night'\n"
                "WIFI_COUNTRY='US'\n"
                "DIETPI_PASSWORD='temporary-password'\n"
            )
            dietpi_template.write_text(DIETPI_TEMPLATE, encoding="utf-8")
            wifi_template.write_text(WIFI_TEMPLATE, encoding="utf-8")

            without_key = root / "without-key"
            env.write_text(base_env + "ZAI_API_KEY=''\n", encoding="utf-8")
            env.chmod(0o600)
            provisioning.render_files(env, dietpi_template, wifi_template, without_key)
            self.assertFalse((without_key / "lexacube-zai-key").exists())

            with_key = root / "with-key"
            env.write_text(base_env + "ZAI_API_KEY='sk-zai-secret'\n", encoding="utf-8")
            env.chmod(0o600)
            provisioning.render_files(env, dietpi_template, wifi_template, with_key)
            key_file = with_key / "lexacube-zai-key"
            self.assertEqual(key_file.read_text(encoding="utf-8"), "sk-zai-secret\n")
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
