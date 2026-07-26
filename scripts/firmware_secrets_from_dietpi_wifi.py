#!/usr/bin/env python3
"""Create the protected ESP32 WiFi header from a DietPi WiFi profile."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import string
import tempfile
from pathlib import Path
from typing import Iterable


DEFAULT_PROFILE_PATHS = (
    Path("/var/lib/dietpi/dietpi-wifi.db"),
    Path("/boot/dietpi-wifi.txt"),
    Path("/boot/firmware/dietpi-wifi.txt"),
)
ASSIGNMENT = re.compile(r"^\s*aWIFI_(SSID|KEY)\[(\d+)]\s*=\s*(.*?)\s*$")


def decode_shell_value(value: str) -> str:
    """Decode DietPi's shell-quoted value without executing shell syntax."""
    fields = shlex.split(value, comments=False, posix=True)
    if len(fields) != 1:
        raise ValueError("expected one shell-quoted value")
    return fields[0]


def read_wifi_profile(path: Path) -> tuple[str, str] | None:
    """Return the first configured SSID and key from one DietPi profile file."""
    profiles: dict[int, dict[str, str]] = {}

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = ASSIGNMENT.match(line)
        if not match:
            continue

        field, raw_index, raw_value = match.groups()
        try:
            value = decode_shell_value(raw_value)
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
        profiles.setdefault(int(raw_index), {})[field] = value

    for index in sorted(profiles):
        profile = profiles[index]
        if profile.get("SSID") and "KEY" in profile:
            return profile["SSID"], profile["KEY"]
    return None


def find_wifi_profile(paths: Iterable[Path]) -> tuple[str, str, Path]:
    for path in paths:
        if not path.is_file():
            continue
        profile = read_wifi_profile(path)
        if profile is not None:
            return profile[0], profile[1], path
    raise RuntimeError(
        "No configured DietPi WiFi profile found in: "
        + ", ".join(str(path) for path in paths)
    )


def c_string(value: str) -> str:
    if "\0" in value:
        raise ValueError("WiFi values cannot contain NUL bytes")
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def validate_wifi_credentials(ssid: str, key: str) -> None:
    if len(ssid.encode("utf-8")) > 32:
        raise ValueError("WiFi SSID exceeds the ESP32 limit of 32 bytes")

    if not key:
        return
    if len(key) == 64:
        if any(character not in string.hexdigits for character in key):
            raise ValueError("a 64-character WiFi key must be a hexadecimal WPA PSK")
        return
    if not 8 <= len(key) <= 63:
        raise ValueError("WiFi passphrase must contain 8-63 characters")
    if any(not 32 <= ord(character) <= 126 for character in key):
        raise ValueError("WiFi passphrase must contain printable ASCII characters")


def render_header(ssid: str, key: str) -> str:
    validate_wifi_credentials(ssid, key)
    escaped_ssid = c_string(ssid)
    escaped_key = c_string(key)
    return f"""\
// Generated from the Raspberry Pi's DietPi WiFi profile by pi-deploy.
// To use different cube credentials, replace /etc/lexacube-firmware-secrets.h.
#pragma once

#define SSID_NAME "{escaped_ssid}"
#define WIFI_PASSWORD "{escaped_key}"
#define SSID_NAME_PORTABLE "{escaped_ssid}"
#define WIFI_PASSWORD_PORTABLE "{escaped_key}"
"""


def create_header(output: Path, content: str) -> bool:
    """Atomically create output mode 0600 without replacing an override."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(temporary_name)

    try:
        os.fchmod(descriptor, 0o600)
        file = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = -1
        with file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        try:
            os.link(temporary, output)
        except FileExistsError:
            return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    directory = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--profile",
        action="append",
        type=Path,
        dest="profiles",
        help="DietPi profile candidate; may be repeated (defaults to standard paths)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        print(f"Preserving existing firmware secrets: {args.output}")
        return 0

    ssid, key, profile_path = find_wifi_profile(args.profiles or DEFAULT_PROFILE_PATHS)
    if create_header(args.output, render_header(ssid, key)):
        print(f"Created protected firmware secrets from {profile_path}")
    else:
        print(f"Preserving existing firmware secrets: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"Unable to generate firmware secrets: {error}") from error
