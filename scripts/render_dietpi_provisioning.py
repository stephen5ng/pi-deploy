#!/usr/bin/env python3
"""Render DietPi first-boot files from a local, Git-ignored env file."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
from pathlib import Path


ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
UNSAFE_DIETPI_PASSWORD_CHARACTERS = set('$"|\\')


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"{path}:{line_number}: expected NAME=VALUE")

        name, raw_value = stripped.split("=", 1)
        name = name.strip()
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"{path}:{line_number}: invalid variable name {name!r}")

        fields = shlex.split(raw_value.strip(), comments=False, posix=True)
        if len(fields) != 1:
            raise ValueError(f"{path}:{line_number}: expected one quoted value")
        values[name] = fields[0]
    return values


def required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value:
        raise ValueError(f"{name} must be set in provisioning.env")
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"{name} contains an unsupported control character")
    return value


def replace_setting(document: str, name: str, value: str) -> str:
    pattern = re.compile(rf"^(?:#)?{re.escape(name)}=.*$", re.MULTILINE)
    updated, count = pattern.subn(lambda _: f"{name}={value}", document, count=1)
    if count != 1:
        raise ValueError(f"setting {name} was not found in the DietPi template")
    return updated


def shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", r"'\''") + "'"


def validate(values: dict[str, str]) -> None:
    """Check only what this script is itself responsible for.

    Deliberately does not check WPA passphrase length, PSK format, country
    codes or password length. wpa_supplicant and DietPi enforce their own
    input rules, and a value that trips them fails the same way whether or not
    it was rejected here first -- so those checks were re-implementations that
    bought nothing but the tests needed to cover them. What is left either
    means the operator never filled the file in, or would corrupt the rendered
    output, which is this script's own job to get right.
    """
    required(values, "WIFI_SSID")
    wifi_password = required(values, "WIFI_PASSWORD")
    required(values, "WIFI_COUNTRY")
    password = required(values, "DIETPI_PASSWORD")

    if wifi_password == "replace-me":
        raise ValueError("WIFI_PASSWORD still contains the example placeholder")
    if password == "replace-with-a-unique-password":
        raise ValueError("DIETPI_PASSWORD still contains the example placeholder")
    if UNSAFE_DIETPI_PASSWORD_CHARACTERS.intersection(password):
        raise ValueError(
            'DIETPI_PASSWORD cannot contain characters DietPi warns against: $"|\\'
        )


def render_dietpi(
    template: str, values: dict[str, str], public_key: str | None
) -> str:
    settings = {
        "AUTO_SETUP_GLOBAL_PASSWORD": required(values, "DIETPI_PASSWORD"),
        "AUTO_SETUP_NET_ETHERNET_ENABLED": "1",
        "AUTO_SETUP_NET_WIFI_ENABLED": "1",
        "AUTO_SETUP_NET_WIFI_COUNTRY_CODE": required(values, "WIFI_COUNTRY"),
        "AUTO_SETUP_NET_HOSTNAME": values.get("DIETPI_HOSTNAME", "lexacube"),
        "AUTO_SETUP_HEADLESS": "1",
        "AUTO_SETUP_CUSTOM_SCRIPT_EXEC": "0",
        "AUTO_SETUP_AUTOMATED": "1",
    }
    if values.get("DIETPI_TIMEZONE"):
        settings["AUTO_SETUP_TIMEZONE"] = values["DIETPI_TIMEZONE"]
    if public_key:
        settings["AUTO_SETUP_SSH_PUBKEY"] = public_key
        settings["SOFTWARE_DISABLE_SSH_PASSWORD_LOGINS"] = "1"

    rendered = template
    for name, value in settings.items():
        rendered = replace_setting(rendered, name, value)
    return rendered


def render_wifi(template: str, values: dict[str, str]) -> str:
    rendered = replace_setting(
        template, "aWIFI_SSID[0]", shell_single_quote(required(values, "WIFI_SSID"))
    )
    return replace_setting(
        rendered,
        "aWIFI_KEY[0]",
        shell_single_quote(required(values, "WIFI_PASSWORD")),
    )


def read_public_key(values: dict[str, str]) -> str | None:
    raw_path = values.get("SSH_PUBLIC_KEY_FILE", "")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    key = path.read_text(encoding="utf-8").strip()
    if "\n" in key or not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
        raise ValueError(f"{path} does not contain one supported SSH public key")
    return key


def render_files(
    env_path: Path,
    dietpi_template: Path,
    wifi_template: Path,
    output_directory: Path,
) -> None:
    if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
        raise ValueError(f"{env_path} must not be readable by group or other users")
    values = parse_env(env_path)
    validate(values)
    public_key = read_public_key(values)

    output_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(output_directory, 0o700)
    outputs = {
        output_directory / "dietpi.txt": render_dietpi(
            dietpi_template.read_text(encoding="utf-8"), values, public_key
        ),
        output_directory / "dietpi-wifi.txt": render_wifi(
            wifi_template.read_text(encoding="utf-8"), values
        ),
    }
    # Consumed and deleted by bootstrap.sh on first boot, which is the only
    # place it can be given permissions -- the boot partition is FAT32.
    if values.get("ZAI_API_KEY"):
        outputs[output_directory / "lexacube-zai-key"] = values["ZAI_API_KEY"] + "\n"
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(path, 0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--dietpi-template", required=True, type=Path)
    parser.add_argument("--wifi-template", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    render_files(
        args.env, args.dietpi_template, args.wifi_template, args.output_directory
    )
    print(f"Rendered DietPi first-boot files in {args.output_directory}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"Unable to render DietPi provisioning files: {error}") from error
