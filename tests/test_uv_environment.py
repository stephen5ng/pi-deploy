"""A uv-managed app must build its venv where the rest of the deploy looks.

cubes moved to uv, so bootstrap took the `uv.lock` branch and uv created
`/opt/lexacube/.venv`. Everything downstream still spells out `cube_env`: the
generated unit's ExecStart, nfc-control reusing lexacube's interpreter, the
pygame.libs search, and the dependency-bindings step -- which found no venv to
activate, ran a bare `pip`, and aborted the whole bootstrap with
"pip: command not found" before nfc-control or knockstrip were reached.
"""

import re
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
BOOTSTRAP = (REPOSITORY / "bootstrap.sh").read_text(encoding="utf-8")
APPS = (REPOSITORY / "apps.yaml").read_text(encoding="utf-8")


class UvEnvironmentTests(unittest.TestCase):
    def test_uv_sync_targets_the_configured_venv_name(self):
        self.assertIn(
            'UV_PROJECT_ENVIRONMENT="$path/$venv_name"',
            BOOTSTRAP,
            "uv would otherwise create .venv, which nothing else references",
        )

    def test_uv_binary_is_on_path_for_later_dependency_commands(self):
        # install_python_cmd runs `uv pip ...` later in the same bootstrap
        # whichever venv branch the app takes, so the export must precede the
        # branch -- not sit inside the lock branch, and not be reachable only
        # when uv had to be installed. This used to pin the export inside the
        # lock branch's text, which is how the requirements branch ended up
        # running the dependency step with uv present on disk but not on PATH.
        export_line = 'export PATH="$HOME/.local/bin:$PATH"'
        export_at = BOOTSTRAP.index(export_line)
        for marker, label in [
            ('if [[ -f "$path/uv.lock" ]]', "the venv branch"),
            ("dep_python_cmd=$(yq", "the dependency commands"),
        ]:
            use_at = BOOTSTRAP.index(marker)
            self.assertLess(
                export_at, use_at, f"PATH must include uv before {label}"
            )
        install_if = BOOTSTRAP.index("if ! command -v uv")
        install_end = BOOTSTRAP.index("fi", install_if)
        self.assertFalse(
            install_if < export_at < install_end,
            "the export must not be reachable only when uv had to be installed",
        )

    def test_dependency_python_installs_do_not_call_bare_pip(self):
        commands = re.findall(r'install_python_cmd:\s*"([^"]+)"', APPS)
        self.assertTrue(commands, "expected at least one install_python_cmd")
        for command in commands:
            for invocation in command.split("&&"):
                self.assertFalse(
                    invocation.strip().startswith("pip "),
                    f"a uv-managed venv carries no pip: {command}",
                )

    def test_every_exec_interpreter_lives_under_a_declared_venv(self):
        venvs = set(re.findall(r"^\s*venv:\s*(\S+)", APPS, re.MULTILINE))
        self.assertIn("cube_env", venvs)
        for exec_line in re.findall(r"^\s*exec:\s*(\S+)", APPS, re.MULTILINE):
            if "/bin/python" not in exec_line:
                continue
            self.assertTrue(
                any(f"/{venv}/bin/python" in exec_line for venv in venvs),
                f"{exec_line} does not point into a declared venv",
            )


if __name__ == "__main__":
    unittest.main()
