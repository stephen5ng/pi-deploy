import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "provision_knockstrip_env.py"
SPEC = importlib.util.spec_from_file_location("provision_knockstrip_env", SCRIPT_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class UpsertEnvLine(unittest.TestCase):
    def test_into_empty(self):
        self.assertEqual(mod.upsert_env_line("", "POSTHOG_API_KEY", "phc_a"),
                         "POSTHOG_API_KEY=phc_a\n")

    def test_replaces_in_place_preserving_others(self):
        content = "# t\nPUSHER_SECRET=shh\nPOSTHOG_API_KEY=old\nCLUE_STATUS_API_KEY=c\n"
        self.assertEqual(
            mod.upsert_env_line(content, "POSTHOG_API_KEY", "new"),
            "# t\nPUSHER_SECRET=shh\nPOSTHOG_API_KEY=new\nCLUE_STATUS_API_KEY=c\n",
        )

    def test_appends_when_absent(self):
        self.assertEqual(
            mod.upsert_env_line("PUSHER_SECRET=shh\n", "POSTHOG_API_KEY", "new"),
            "PUSHER_SECRET=shh\nPOSTHOG_API_KEY=new\n",
        )

    def test_prefix_key_not_matched(self):
        self.assertEqual(
            mod.upsert_env_line("POSTHOG_API_KEY_BACKUP=keep\n", "POSTHOG_API_KEY", "v"),
            "POSTHOG_API_KEY_BACKUP=keep\nPOSTHOG_API_KEY=v\n",
        )

    def test_idempotent(self):
        once = mod.upsert_env_line("PUSHER_SECRET=shh\n", "POSTHOG_API_KEY", "x")
        self.assertEqual(once, mod.upsert_env_line(once, "POSTHOG_API_KEY", "x"))

    def test_applying_multiple_keys_accumulates(self):
        content = ""
        for k, v in {"POSTHOG_API_KEY": "p", "CLUE_STATUS_API_KEY": "c"}.items():
            content = mod.upsert_env_line(content, k, v)
        self.assertEqual(content, "POSTHOG_API_KEY=p\nCLUE_STATUS_API_KEY=c\n")


class ParseEnv(unittest.TestCase):
    def test_ignores_comments_blanks_strips_quotes(self):
        text = '# c\n\nPUSHER_SECRET="shh"\nCLUE_STATUS_API_KEY=k\n'
        self.assertEqual(
            mod.parse_env(text),
            {"PUSHER_SECRET": "shh", "CLUE_STATUS_API_KEY": "k"},
        )


class ResolveSecrets(unittest.TestCase):
    def _no_fetch(self, *a, **k):  # fail loudly if the API is touched unexpectedly
        raise AssertionError("fetch should not be called")

    def test_direct_phc_wins_without_fetch(self):
        env = {"POSTHOG_API_KEY": "phc_direct"}
        out = mod.resolve_secrets(env, project="507032", mgmt_host="h", fetch=self._no_fetch)
        self.assertEqual(out, {"POSTHOG_API_KEY": "phc_direct"})

    def test_phx_triggers_fetch(self):
        calls = {}

        def fake_fetch(phx, project, host):
            calls.update(phx=phx, project=project, host=host)
            return "phc_fetched"

        env = {"POSTHOG_PERSONAL_API_KEY": "phx_1"}
        out = mod.resolve_secrets(env, project="507032", mgmt_host="h", fetch=fake_fetch)
        self.assertEqual(out, {"POSTHOG_API_KEY": "phc_fetched"})
        self.assertEqual(calls, {"phx": "phx_1", "project": "507032", "host": "h"})

    def test_blank_secrets_are_skipped(self):
        env = {"POSTHOG_API_KEY": "  ", "CLUE_STATUS_API_KEY": "", "PUSHER_SECRET": "shh"}
        out = mod.resolve_secrets(env, project="507032", mgmt_host="h", fetch=self._no_fetch)
        self.assertEqual(out, {"PUSHER_SECRET": "shh"})

    def test_all_three_resolved(self):
        env = {
            "POSTHOG_API_KEY": "phc_x",
            "CLUE_STATUS_API_KEY": "clue",
            "PUSHER_SECRET": "push",
        }
        out = mod.resolve_secrets(env, project="507032", mgmt_host="h", fetch=self._no_fetch)
        self.assertEqual(
            out, {"POSTHOG_API_KEY": "phc_x", "CLUE_STATUS_API_KEY": "clue", "PUSHER_SECRET": "push"}
        )


if __name__ == "__main__":
    unittest.main()
