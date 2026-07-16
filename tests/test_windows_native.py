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
        self.assertIn('"msys-file-manager"', source)
        self.assertIn('"msys-touch-calibration"', source)
        self.assertIn('"msys-input-touch"', source)
        self.assertIn('"msys-calculator"', source)
        self.assertIn('"msys-device-info"', source)
        self.assertIn('"msys-notes"', source)
        self.assertIn('"msys-openstick-ch347"', source)
        self.assertIn('@("start", "stop")', source)

    def test_optional_checks_stay_in_the_single_sync_build(self) -> None:
        source = (ROOT / "msys-native.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('-RunTest:($NativeArgs -contains "--test")', source)
        self.assertIn('-RunProbe:($NativeArgs -contains "--probe")', source)
        self.assertIn('Neither option runs doctor.', source)
        self.assertIn('$targets += "lvgl-probe"', source)
        self.assertIn('$targets += "probe"', source)
        self.assertIn("-m unittest discover -s tests -v", source)
        self.assertNotIn("make -j2 UI_DIR=$uiQ clean all", source)

    def test_lvgl_repository_build_targets_match_their_makefiles(self) -> None:
        source = (ROOT / "msys-native.ps1").read_text(encoding="utf-8-sig")

        def case(name: str, following: str) -> str:
            start = source.index(f'        "{name}" {{')
            end = source.index(f'        "{following}" {{', start)
            return source[start:end]

        self.assertIn('@("stage")', case("msys-ui-lvgl", "msys-settings"))
        self.assertIn('@("stage")', case("msys-file-manager", "msys-touch-calibration"))
        self.assertIn('@("stage")', case("msys-input-touch", "msys-calculator"))
        self.assertIn("UI_DIR=$uiQ all", case("msys-calculator", "msys-device-info"))
        device_info = case("msys-device-info", "msys-notes")
        self.assertIn('@("all")', device_info)
        self.assertIn('$targets += "probe"', device_info)
        notes = case("msys-notes", "msys-openstick-ch347")
        self.assertIn('@("stage")', notes)
        self.assertIn("UI_ROOT=$uiQ SDK_ROOT=$sdkQ", notes)
        self.assertIn('$targets += "probe"', notes)

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
