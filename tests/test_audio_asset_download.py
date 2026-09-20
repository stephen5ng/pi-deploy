"""A missing word-sound release must cost the words, not the whole Pi.

The assets live in a release on the private cubes repo. `curl -sf` on the
resulting 404 prints nothing, json.load() raised on the empty input, and under
`set -euo pipefail` that killed bootstrap with a traceback — before any systemd
unit was installed. Measured on a fresh lexacube rig: no lexacube.service, no
nfc-control.service, no knockstrip.service.
"""

import json
import subprocess
import textwrap
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP_SOURCE = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")


def extract_asset_parser() -> str:
    """The inline python3 program bootstrap feeds the release response to."""
    start = BOOTSTRAP_SOURCE.index("python3 -c \"$(cat <<'PYTHON'")
    start = BOOTSTRAP_SOURCE.index("\n", start) + 1
    end = BOOTSTRAP_SOURCE.index("PYTHON", start)
    return BOOTSTRAP_SOURCE[start:end]


def run_parser(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-c", extract_asset_parser()],
        input=payload,
        capture_output=True,
        text=True,
    )


class AssetParserTests(unittest.TestCase):
    def test_parts_are_returned_as_name_and_api_url_in_order(self):
        payload = json.dumps(
            {
                "assets": [
                    {"name": "word_sounds.tar.gz.part.ab", "url": "https://api/2"},
                    {"name": "word_sounds.tar.gz.part.aa", "url": "https://api/1"},
                    {"name": "release-notes.txt", "url": "https://api/3"},
                ]
            }
        )

        result = run_parser(payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "word_sounds.tar.gz.part.aa\thttps://api/1",
                "word_sounds.tar.gz.part.ab\thttps://api/2",
            ],
        )

    def test_the_asset_name_is_carried_so_the_parts_can_be_reassembled(self):
        # The API asset URL ends in a numeric id, so basename(url) would name
        # every part something the `word_sounds.tar.gz.part.*` glob misses.
        payload = json.dumps(
            {"assets": [{"name": "word_sounds.tar.gz.part.aa", "url": "https://api/9"}]}
        )

        self.assertTrue(
            run_parser(payload).stdout.startswith("word_sounds.tar.gz.part.aa\t")
        )

    def test_an_empty_response_exits_cleanly(self):
        result = run_parser("")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_an_api_error_document_exits_cleanly(self):
        result = run_parser(json.dumps({"message": "Not Found"}))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class AudioStepDegradesTests(unittest.TestCase):
    def test_the_failing_release_query_cannot_abort_the_run(self):
        script = textwrap.dedent(
            """
            set -euo pipefail
            ASSET_URLS=$(printf '' | python3 -c 'import json,sys
try:
    assets = json.load(sys.stdin)["assets"]
except Exception:
    sys.exit(0)
print("x")') || true
            test -z "$ASSET_URLS"
            echo reached-the-next-step
            """
        )

        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached-the-next-step", result.stdout)

    def test_bootstrap_guards_the_query_and_warns_about_the_consequence(self):
        query = BOOTSTRAP_SOURCE[BOOTSTRAP_SOURCE.index("ASSET_URLS=$(curl"):]
        query = query[: query.index("if [[ -z")]
        self.assertIn(") || true", query, "the release query must not abort the run")
        self.assertIn("The game will run and speak no words.", BOOTSTRAP_SOURCE)

    def test_downloads_use_the_api_asset_endpoint_with_the_token(self):
        self.assertIn('--header "Accept: application/octet-stream"', BOOTSTRAP_SOURCE)
        self.assertIn('curl -Lf "${GITHUB_API_AUTH[@]}"', BOOTSTRAP_SOURCE)
        self.assertIn('curl -sf "${GITHUB_API_AUTH[@]}" "$RELEASE_API"', BOOTSTRAP_SOURCE)


class ApiTokenInstallTests(unittest.TestCase):
    def run_configure(self, root: Path) -> subprocess.CompletedProcess:
        start = BOOTSTRAP_SOURCE.index("GITHUB_API_TOKEN_FILE=")
        end = BOOTSTRAP_SOURCE.index("# GitHub SSH host keys")
        body = BOOTSTRAP_SOURCE[start:end]
        body = body.replace("/etc/github-api-token", f"{root}/etc/github-api-token")
        body = body.replace("/boot/firmware", f"{root}/boot/firmware")
        body = body.replace("/boot", f"{root}/boot")
        script = (
            "set -euo pipefail\n"
            + body
            + "\nconfigure_github_api_token\n"
            + 'printf "%s\\n" "${GITHUB_API_AUTH[@]:-}"\n'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_a_staged_token_is_installed_private_and_removed_from_boot(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "boot").mkdir()
            (root / "etc").mkdir()
            staged = root / "boot" / "github-api-token"
            staged.write_text("ghp_example\n")

            result = self.run_configure(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            installed = root / "etc" / "github-api-token"
            self.assertEqual(installed.read_text(), "ghp_example\n")
            self.assertEqual(installed.stat().st_mode & 0o077, 0)
            self.assertFalse(staged.exists())
            self.assertIn("Authorization: Bearer ghp_example", result.stdout)

    def test_no_token_leaves_the_auth_arguments_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "boot").mkdir()
            (root / "etc").mkdir()

            result = self.run_configure(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Authorization", result.stdout)


if __name__ == "__main__":
    unittest.main()


class InstallWordSoundsTests(unittest.TestCase):
    """The download and unpack must degrade the same way the query does.

    Each of these failures previously ran under `set -e` and ended the whole
    bootstrap before any systemd unit was installed.
    """

    def run_install(self, root, curl_script: str, parts: str = "aa") -> subprocess.CompletedProcess:
        start = BOOTSTRAP_SOURCE.index("install_word_sounds() {")
        end = BOOTSTRAP_SOURCE.index("# GitHub SSH host keys")
        body = BOOTSTRAP_SOURCE[start:end]

        binaries = root / "bin"
        binaries.mkdir(exist_ok=True)
        (binaries / "curl").write_text(curl_script)
        (binaries / "curl").chmod(0o755)

        assets = root / "assets"
        assets.mkdir(exist_ok=True)
        downloads = root / "downloads"
        downloads.mkdir(exist_ok=True)

        urls = "\n".join(
            f"word_sounds.tar.gz.part.{part}\thttps://api/{part}" for part in parts.split()
        )
        script = (
            "set -euo pipefail\n"
            f'export PATH="{binaries}:$PATH"\n'
            "GITHUB_API_AUTH=()\n"
            + body
            + f'\nif install_word_sounds "{urls}" "{downloads}" "{assets}"; then\n'
            "    echo INSTALLED\nelse\n    echo DEGRADED\nfi\n"
            "echo reached-the-next-step\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def make_corpus_curl(self, root) -> str:
        """A curl stand-in that writes a real one-word corpus tarball."""
        import tarfile

        corpus = root / "corpus"
        (corpus / "word_sounds_0").mkdir(parents=True, exist_ok=True)
        (corpus / "word_sounds_0" / "aah.wav").write_bytes(b"RIFF")
        (corpus / "word_sounds_1").mkdir(exist_ok=True)
        (corpus / "word_sounds_1" / "aah.wav").write_bytes(b"RIFF")
        archive = root / "word_sounds.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("word_sounds_0", "word_sounds_1"):
                tar.add(corpus / name, arcname=name)
        return (
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "-o" ]; then out=$2; fi\n'
            "  shift\n"
            "done\n"
            f'cp "{archive}" "$out"\n'
        )

    def test_a_complete_download_publishes_all_three_voices(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_install(root, self.make_corpus_curl(root))

            self.assertIn("INSTALLED", result.stdout, result.stderr)
            for voice in ("word_sounds_0", "word_sounds_1", "word_sounds_2"):
                self.assertTrue((root / "assets" / voice).is_dir(), voice)
            self.assertFalse((root / "assets" / ".word_sounds_staging").exists())

    def test_a_failing_download_degrades_and_publishes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            result = self.run_install(root, "#!/bin/sh\nexit 22\n")

            self.assertIn("DEGRADED", result.stdout, result.stderr)
            self.assertIn("reached-the-next-step", result.stdout)
            self.assertEqual(list((root / "assets").glob("word_sounds_*")), [])

    def test_a_truncated_archive_degrades_without_a_half_corpus(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A part that downloads fine but is not a valid gzip stream: the
            # failure mode that would otherwise leave word_sounds_0 present
            # and make every later run skip the download.
            curl = (
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                '  if [ "$1" = "-o" ]; then out=$2; fi\n'
                "  shift\n"
                "done\n"
                'printf "not a gzip stream" > "$out"\n'
            )

            result = self.run_install(root, curl)

            self.assertIn("DEGRADED", result.stdout, result.stderr)
            self.assertEqual(list((root / "assets").glob("word_sounds_*")), [])

    def test_one_bad_part_among_several_degrades(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curl = (
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                '  if [ "$1" = "-o" ]; then out=$2; fi\n'
                '  if [ "$1" = "https://api/ab" ]; then fail=1; fi\n'
                "  shift\n"
                "done\n"
                '[ -n "${fail:-}" ] && exit 18\n'
                'printf "x" > "$out"\n'
            )

            result = self.run_install(root, curl, parts="aa ab")

            self.assertIn("DEGRADED", result.stdout, result.stderr)
            self.assertEqual(list((root / "assets").glob("word_sounds_*")), [])
