from __future__ import annotations

import subprocess
import os
import shutil
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsNativePathTests(unittest.TestCase):
    def test_cmd_dispatches_before_the_wsl_wrapper(self) -> None:
        source = (ROOT / "msys.cmd").read_text(encoding="utf-8-sig")
        self.assertLess(
            source.index('if /I "%~1"=="--native"'),
            source.index('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%MSYS_SCRIPT_ROOT%msys.ps1"'),
        )
        self.assertIn("MSYS_NATIVE_ARG_COUNT", source)

    def test_sync_has_bounded_paths_and_atomic_swap(self) -> None:
        source = (ROOT / "msys-native.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('if mv `"`$stage`"', source)
        self.assertIn("repository escapes the configured workspace", source)
        self.assertIn('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$', source)
        self.assertNotIn("ControlMaster", source)
        self.assertNotIn("--exclude=cache", source)
        self.assertIn('"msys-ui-lvgl"', source)
        self.assertIn('"msys-settings"', source)

    def test_remote_python_disables_bytecode(self) -> None:
        source = (ROOT / "msys-native.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", source)
        self.assertIn(' `"`$python`" -B', source)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "PowerShell unavailable")
    def test_help_needs_no_wsl(self) -> None:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "msys-native.ps1"),
                "help",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("no WSL", completed.stdout)


if __name__ == "__main__":
    unittest.main()
