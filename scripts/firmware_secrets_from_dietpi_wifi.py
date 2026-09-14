#!/usr/bin/env python3
"""Create the protected ESP32 WiFi header from a DietPi WiFi profile."""

from __future__ import annotations

import argparse
import os
import re
import shlex
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


def read_wifi_profile(path: Path, wanted_ssid: str | None = None) -> tuple[str, str] | None:
    """Return one configured SSID and key from a DietPi profile file.

    With `wanted_ssid`, returns that network specifically. Without it, returns
    the lowest-numbered configured entry, which is a guess: the Pi is dual-band
    and the ESP32 is 2.4GHz only, so whichever network happens to sit at entry
    0 may be one the cubes physically cannot join. Name the SSID instead.
    """
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
        if not profile.get("SSID") or "KEY" not in profile:
            continue
        if wanted_ssid is not None and profile["SSID"] != wanted_ssid:
            continue
        return profile["SSID"], profile["KEY"]
    return None


def find_wifi_profile(
    paths: Iterable[Path], wanted_ssid: str | None = None
) -> tuple[str, str, Path]:
    searched = []
    for path in paths:
        if not path.is_file():
            continue
        searched.append(path)
        profile = read_wifi_profile(path, wanted_ssid)
        if profile is not None:
            return profile[0], profile[1], path
    if wanted_ssid is not None:
        # Loud, because the alternative is compiling some other network's
        # credentials into the cubes and discovering it when they will not
        # associate at an event.
        raise RuntimeError(
            f"SSID {wanted_ssid!r} is not configured in any DietPi WiFi "
            "profile. Add it to dietpi-wifi.txt (or correct the configured "
            "cube SSID). Searched: "
            + (", ".join(str(path) for path in searched) or "no profile files")
        )
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


def render_header(ssid: str, key: str) -> str:
    # No credential validation here: these values come out of DietPi's own WiFi
    # database on a Pi that has already joined the network with them, so they
    # are valid by construction. Escaping them into C string literals is the
    # only thing this script is responsible for.
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
        "--ssid",
        help=(
            "SSID to compile into the firmware. The ESP32 is 2.4GHz only while "
            "the Pi is dual-band, so naming it is the only way to be sure the "
            "cubes get a network they can join. Without it the lowest-numbered "
            "configured entry wins, whatever band it is on."
        ),
    )
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

    ssid, key, profile_path = find_wifi_profile(
        args.profiles or DEFAULT_PROFILE_PATHS, args.ssid
    )
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
