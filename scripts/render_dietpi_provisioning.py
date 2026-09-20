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

    cube_ssid = values.get("CUBE_WIFI_SSID", "")
    cube_password = values.get("CUBE_WIFI_PASSWORD", "")
    if bool(cube_ssid) != bool(cube_password):
        raise ValueError(
            "CUBE_WIFI_SSID and CUBE_WIFI_PASSWORD must be set together"
        )
    if cube_password == "replace-me":
        raise ValueError("CUBE_WIFI_PASSWORD still contains the example placeholder")

    keyboard_layout = values.get("DIETPI_KEYBOARD_LAYOUT", "us")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", keyboard_layout):
        raise ValueError("DIETPI_KEYBOARD_LAYOUT must be a keyboard-layout name")


def render_dietpi(
    template: str, values: dict[str, str], public_key: str | None
) -> str:
    settings = {
        "AUTO_SETUP_GLOBAL_PASSWORD": required(values, "DIETPI_PASSWORD"),
        # DietPi's stock template defaults to `gb`.  This project is
        # provisioned from US keyboards unless the local env says otherwise.
        "AUTO_SETUP_KEYBOARD_LAYOUT": values.get("DIETPI_KEYBOARD_LAYOUT", "us"),
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
    rendered = replace_setting(
        rendered,
        "aWIFI_KEY[0]",
        shell_single_quote(required(values, "WIFI_PASSWORD")),
    )
    return rendered


def c_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def render_cube_firmware_secrets(values: dict[str, str]) -> str:
    ssid = c_string(values["CUBE_WIFI_SSID"])
    password = c_string(values["CUBE_WIFI_PASSWORD"])
    return f'''// Generated from provisioning.env by pi-deploy.\n#pragma once\n\n#define SSID_NAME "{ssid}"\n#define WIFI_PASSWORD "{password}"\n#define SSID_NAME_PORTABLE "{ssid}"\n#define WIFI_PASSWORD_PORTABLE "{password}"\n'''


def read_public_key(values: dict[str, str]) -> str | None:
    raw_path = values.get("SSH_PUBLIC_KEY_FILE", "")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    key = path.read_text(encoding="utf-8").strip()
    if "\n" in key or not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
        raise ValueError(f"{path} does not contain one supported SSH public key")
    return key


def secrets_directory(values: dict[str, str]) -> Path:
    return Path(values.get("SECRETS_DIR") or "~/.lexacube-secrets").expanduser()


APP_NAME = re.compile(r"^\s*-\s+name:\s*(\S+)\s*$", re.MULTILINE)


def configured_app_names(apps_config: Path) -> set[str]:
    """The app names bootstrap will look for on the boot partition.

    Anything else staged there is never installed and never deleted, because
    `consume_staged_app_env` only runs for names in apps.yaml -- so a typo or a
    retired app would leave credentials readable on a FAT partition forever.
    """
    return set(APP_NAME.findall(apps_config.read_text(encoding="utf-8")))


DEFAULT_APPS_CONFIG = Path(__file__).resolve().parents[1] / "apps.yaml"


def stage_app_env_files(
    values: dict[str, str],
    output_directory: Path,
    apps_config: Path = DEFAULT_APPS_CONFIG,
) -> list[str]:
    """Copy per-rig `<app>.env` secrets so they can be written to /boot.

    These files are gitignored in the app repos and cannot be defaulted, so a
    reflash loses them: lexacube then plays while recording no analytics at
    all, and knockstrip refuses to start. Keeping them in a directory outside
    the repository is what lets a fresh image pick them up unattended.
    """
    directory = secrets_directory(values)
    if not directory.is_dir():
        print(
            f"Warning: no per-rig secrets directory at {directory}; "
            "no <app>.env files will be staged."
        )
        return []

    app_names = configured_app_names(apps_config)
    staged: list[str] = []
    for source in sorted(directory.glob("*.env")):
        if source.stem not in app_names:
            print(
                f"Warning: not staging {source.name}; "
                f"{apps_config.name} configures no app named '{source.stem}'."
            )
            continue
        destination = output_directory / source.name
        destination.write_bytes(source.read_bytes())
        os.chmod(destination, 0o600)
        staged.append(source.name)

    if not staged:
        print(f"Warning: {directory} holds no *.env files; none will be staged.")
    return staged


def stage_github_deploy_keys(values: dict[str, str], output_directory: Path) -> list[str]:
    """Copy per-repository GitHub deploy keys so they reach /boot.

    Without one of these a fresh Pi cannot clone a private application repo at
    all, and bootstrap stops at the first clone. GitHub rejects the same deploy
    key on a second repository, so there is one key per repo, named
    `<owner>.<repo>`; `scripts/make_deploy_key.sh` creates and registers them.
    """
    directory = secrets_directory(values) / "github-deploy-keys"
    if not directory.is_dir():
        return []

    staged: list[str] = []
    for source in sorted(directory.iterdir()):
        if not source.is_file() or source.name.endswith(".pub"):
            continue
        if "." not in source.name:
            print(f"Warning: ignoring {source}; expected <owner>.<repo>")
            continue
        destination = output_directory / f"github-deploy-key-{source.name}"
        destination.write_bytes(source.read_bytes())
        os.chmod(destination, 0o600)
        staged.append(source.name)
    return staged


def render_files(
    env_path: Path,
    dietpi_template: Path,
    wifi_template: Path,
    output_directory: Path,
    apps_config: Path = DEFAULT_APPS_CONFIG,
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
    if values.get("CUBE_WIFI_SSID"):
        outputs[output_directory / "lexacube-firmware-secrets.h"] = (
            render_cube_firmware_secrets(values)
        )
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(path, 0o600)

    for name in stage_app_env_files(values, output_directory, apps_config):
        print(f"Staged per-rig secrets: {name}")
    for name in stage_github_deploy_keys(values, output_directory):
        print(f"Staged GitHub deploy key: {name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--dietpi-template", required=True, type=Path)
    parser.add_argument("--wifi-template", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--apps-config", type=Path, default=DEFAULT_APPS_CONFIG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    render_files(
        args.env,
        args.dietpi_template,
        args.wifi_template,
        args.output_directory,
        args.apps_config,
    )
    print(f"Rendered DietPi first-boot files in {args.output_directory}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"Unable to render DietPi provisioning files: {error}") from error
