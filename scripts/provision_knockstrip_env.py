#!/usr/bin/env python3
"""Provision the knockstrip service secrets into /etc/knockstrip.env, over ssh.

The knockstrip systemd unit reads secrets from /etc/knockstrip.env (an
EnvironmentFile, mode 0600). bootstrap.sh wires that file into the unit when it
exists but never populates it, so the secret values are provisioned here instead
of hand-edited on the Pi. Run this from the laptop after the Pi is up — as a
step in a re-image, or as a one-off when a key rotates.

Values come from the Git-ignored provisioning.env (same file the SD render
reads). Secrets provisioned (each skipped when blank):

  POSTHOG_API_KEY      the phc_ project ingest key. If not given directly, it is
                       fetched from the PostHog management API using
                       POSTHOG_PERSONAL_API_KEY (phx_). The phx_ key never leaves
                       the laptop; only the public phc_ key reaches the Pi.
  CLUE_STATUS_API_KEY  Pursuit clue-grant API key.
  PUSHER_SECRET        Pusher app secret.

Nothing secret is printed (only masked confirmations). Usage:

    python3 scripts/provision_knockstrip_env.py [--pi USER@HOST] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from pathlib import Path

ENV_FILE = "/etc/knockstrip.env"
DEFAULT_PI_HOST = "dietpi@192.168.8.247"
DEFAULT_PROJECT = "507032"
DEFAULT_MGMT_HOST = "https://us.posthog.com"

# Direct-value secrets copied verbatim from provisioning.env to the Pi.
DIRECT_SECRETS = ("CLUE_STATUS_API_KEY", "PUSHER_SECRET")


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; ignore comments/blanks; strip surrounding quotes."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def upsert_env_line(content: str, key: str, value: str) -> str:
    """Return `content` with exactly one `key=value` line.

    Replaces the first existing `key=` line in place (preserving order and every
    other line, including comments); appends if the key is absent. Idempotent,
    and the result always ends with a single trailing newline.
    """
    new_line = f"{key}={value}"
    replaced = False
    out: list[str] = []
    for line in content.splitlines():
        if not replaced and line.lstrip().startswith(f"{key}="):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    return "\n".join(out) + "\n"


def read_provisioning_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"error: {path} not found (copy provisioning.env.example → provisioning.env)")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        sys.exit(f"error: {path} must not be readable by group/other (chmod 600)")
    return parse_env(path.read_text())


def fetch_ingest_token(phx: str, project: str, mgmt_host: str) -> str:
    """The phc_ ingest token, from GET /api/projects/<id>/ via curl.

    curl (not urllib) so system Python needs no certifi; the phx_ key goes in a
    stdin-fed curl config (`-K -`), never argv, so `ps` can't see it.
    """
    url = f"{mgmt_host.rstrip('/')}/api/projects/{project}/"
    try:
        res = subprocess.run(
            ["curl", "-sS", "--fail", "-K", "-", url],
            input=f'header = "Authorization: Bearer {phx}"\n',
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        sys.exit("error: curl not found")
    if res.returncode != 0:
        sys.exit(f"error: PostHog API fetch failed: {res.stderr.strip()}")
    token = json.loads(res.stdout).get("api_token", "")
    if not token.startswith("phc_"):
        sys.exit("error: project response had no phc_ api_token")
    return token


def resolve_secrets(env: dict[str, str], *, project: str, mgmt_host: str, fetch=fetch_ingest_token) -> dict[str, str]:
    """The secrets to write, keyed by env-file name. Blank sources are omitted.

    POSTHOG_API_KEY resolves from a direct phc_ value, else by fetching it with
    the phx_ personal key; a missing/blank PostHog config simply skips the key.
    """
    resolved: dict[str, str] = {}
    phc = env.get("POSTHOG_API_KEY", "").strip()
    if not phc:
        phx = env.get("POSTHOG_PERSONAL_API_KEY", "").strip()
        if phx:
            phc = fetch(phx, project, mgmt_host)
    if phc:
        resolved["POSTHOG_API_KEY"] = phc
    for key in DIRECT_SECRETS:
        val = env.get(key, "").strip()
        if val:
            resolved[key] = val
    return resolved


def ssh_read(pi_host: str, path: str) -> str:
    """Current remote file contents, or '' if it does not exist yet."""
    res = subprocess.run(
        ["ssh", pi_host, f"sudo cat {path} 2>/dev/null || true"],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        sys.exit(f"error: ssh read failed ({pi_host}): {res.stderr.strip()}")
    return res.stdout


def ssh_write(pi_host: str, path: str, content: str) -> None:
    """Write `content` to a 0600 remote file via `sudo tee`, content on stdin."""
    remote = f"sudo sh -c 'umask 077 && cat > {path} && chmod 0600 {path}'"
    res = subprocess.run(
        ["ssh", pi_host, remote], input=content, capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        sys.exit(f"error: ssh write failed ({pi_host}): {res.stderr.strip()}")


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 4 else "…"


def main() -> None:
    import os

    ap = argparse.ArgumentParser(description="Set knockstrip secrets in the Pi's env file.")
    ap.add_argument("--pi", default=None,
                    help="user@host (default env KNOCKSTRIP_PI_HOST, provisioning.env, or built-in)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve values and show what would change; do not write to the Pi")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = read_provisioning_env(repo_root / "provisioning.env")
    project = env.get("POSTHOG_PROJECT", "").strip() or DEFAULT_PROJECT
    mgmt_host = env.get("POSTHOG_HOST_MGMT", "").strip() or DEFAULT_MGMT_HOST
    pi_host = (
        args.pi
        or os.environ.get("KNOCKSTRIP_PI_HOST", "").strip()
        or env.get("KNOCKSTRIP_PI_HOST", "").strip()
        or DEFAULT_PI_HOST
    )

    resolved = resolve_secrets(env, project=project, mgmt_host=mgmt_host)
    if not resolved:
        sys.exit("error: no knockstrip secrets set in provisioning.env "
                 "(POSTHOG_API_KEY/POSTHOG_PERSONAL_API_KEY, CLUE_STATUS_API_KEY, PUSHER_SECRET)")
    for key, val in resolved.items():
        print(f"resolved {key}: {_mask(val)}")
    skipped = [k for k in ("POSTHOG_API_KEY", *DIRECT_SECRETS) if k not in resolved]
    if skipped:
        print(f"skipped (blank in provisioning.env): {', '.join(skipped)}")

    if args.dry_run:
        print(f"dry-run: would set {', '.join(resolved)} in {ENV_FILE} on {pi_host}")
        return

    existing = ssh_read(pi_host, ENV_FILE)
    new = existing
    for key, val in resolved.items():
        new = upsert_env_line(new, key, val)
    if new == existing:
        print(f"unchanged: {ENV_FILE} on {pi_host} already matches")
    else:
        ssh_write(pi_host, ENV_FILE, new)
        print(f"set {', '.join(resolved)} in {ENV_FILE} on {pi_host}")
    print("next: gate + restart —")
    print(f"  ssh {pi_host} sudo systemctl start knockstrip-preflight.service")
    print(f"  ssh {pi_host} sudo systemctl restart knockstrip.service")


if __name__ == "__main__":
    main()
