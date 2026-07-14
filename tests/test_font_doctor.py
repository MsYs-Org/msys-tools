from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.remote_font_probe import assess_report


class FontReportTests(unittest.TestCase):
    def test_clear_xft_report_is_healthy(self) -> None:
        report = {
            "windowing_system": "x11",
            "mapped_font_libraries": [
                "libXft.so.2",
                "libfontconfig.so.1",
                "libfreetype.so.6",
            ],
            "requested_font": {
                "actual": {"family": "Noto Sans CJK SC"},
                "sample_width": 96,
                "glyph_widths": [16, 16, 16],
            },
            "controls": {"label": 100, "entry": 120, "treeview": 80},
        }
        self.assertEqual(assess_report(report), [])

    def test_fixed_zero_width_report_explains_failure(self) -> None:
        report = {
            "windowing_system": "x11",
            "mapped_font_libraries": [],
            "requested_font": {
                "actual": {"family": "fixed"},
                "sample_width": 0,
                "glyph_widths": [0],
            },
            "controls": {},
        }
        self.assertEqual(
            assess_report(report),
            [
                "XFT_BACKEND_NOT_LOADED",
                "BITMAP_FIXED_FALLBACK",
                "CJK_SAMPLE_HAS_NO_ADVANCE",
                "CJK_GLYPH_MISSING",
            ],
        )


class FontDoctorCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_command_uses_selected_runtime_without_writing_bytecode(self) -> None:
        completed = subprocess.CompletedProcess(["ssh"], 3)
        with mock.patch.object(dev, "ssh", return_value=completed) as ssh:
            result = dev.command_font_doctor(
                self.ctx,
                "/tmp/msys-main",
                python="/opt/msys/candidate/.runtime/python/bin/python3",
                display=":24",
                family="Noto Sans CJK SC",
                size=16,
            )
        self.assertEqual(result, 3)
        command = ssh.call_args.args[1]
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertIn("-B", command)
        self.assertIn("msys_tools.remote_font_probe", command)
        self.assertIn("/opt/msys/candidate/.runtime/python/bin/python3", command)
        self.assertIn("'--display' ':24'", command)
        self.assertNotIn("/opt/msys/current/", command)

    def test_default_prefers_formal_current_then_falls_back_to_development(self) -> None:
        completed = subprocess.CompletedProcess(["ssh"], 0)
        with mock.patch.object(dev, "ssh", return_value=completed) as ssh:
            result = dev.command_font_doctor(
                self.ctx,
                "/tmp/msys-main",
                python=None,
                display=":24",
                family="Noto Sans CJK SC",
                size=16,
            )
        self.assertEqual(result, 0)
        command = ssh.call_args.args[1]
        current = "/opt/msys/current/.runtime/python/bin/python3"
        development = "/opt/msys-dev/.runtime/python/bin/python3"
        self.assertIn(f"if test -x '{current}'", command)
        self.assertIn(f"elif test -x '{development}'", command)
        self.assertLess(command.index(current), command.index(development))
        self.assertEqual(command.count("PYTHONDONTWRITEBYTECODE=1"), 1)
        self.assertEqual(command.count("msys_tools.remote_font_probe"), 2)
        self.assertEqual(command.count("'-B'"), 2)

    def test_command_rejects_relative_python_before_ssh(self) -> None:
        with mock.patch.object(dev, "ssh") as ssh:
            result = dev.command_font_doctor(
                self.ctx,
                "/tmp/msys-main",
                python="python3",
                display=":24",
                family="Noto Sans CJK SC",
                size=16,
            )
        self.assertEqual(result, 2)
        ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
