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
            "font_catalog": {
                "count": 2,
                "families": ["Noto Sans CJK SC", "DejaVu Sans"],
                "requested_present": True,
            },
            "raster_probe": {
                "ink_pixels": 320,
                "ink_bbox": [4, 5, 88, 24],
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
            "font_catalog": {
                "count": 1,
                "families": ["fixed"],
                "requested_present": False,
            },
            "raster_probe": {"ink_pixels": 0, "ink_bbox": None},
            "controls": {},
        }
        self.assertEqual(
            assess_report(report),
            [
                "XFT_BACKEND_NOT_LOADED",
                "FONT_CATALOG_FIXED_ONLY",
                "REQUESTED_FAMILY_UNAVAILABLE",
                "BITMAP_FIXED_FALLBACK",
                "CJK_SAMPLE_HAS_NO_ADVANCE",
                "CJK_GLYPH_MISSING",
                "CJK_SAMPLE_HAS_NO_INK",
            ],
        )

    def test_positive_metrics_without_real_cjk_ink_are_rejected(self) -> None:
        report = {
            "windowing_system": "x11",
            "mapped_font_libraries": ["libXft.so.2"],
            "requested_font": {
                "actual": {"family": "Noto Sans CJK SC"},
                "sample_width": 96,
                "glyph_widths": [16, 16, 16],
            },
            "font_catalog": {
                "count": 2,
                "families": ["Noto Sans CJK SC", "DejaVu Sans"],
                "requested_present": True,
            },
            "raster_probe": {"ink_pixels": 0, "ink_bbox": None},
            "controls": {"label": 100},
        }
        self.assertEqual(assess_report(report), ["CJK_SAMPLE_HAS_NO_INK"])

    def test_fixed_only_catalog_cannot_impersonate_requested_noto(self) -> None:
        report = {
            "windowing_system": "x11",
            "mapped_font_libraries": [],
            "requested_font": {
                "actual": {"family": "Noto Sans CJK SC"},
                "sample_width": 144,
                "glyph_widths": [24] * 6,
            },
            "font_catalog": {
                "count": 1,
                "families": ["fixed"],
                "requested_present": False,
            },
            "raster_probe": {"ink_pixels": 0, "ink_bbox": None},
            "controls": {"label": 100},
        }
        self.assertEqual(
            assess_report(report),
            [
                "XFT_BACKEND_NOT_LOADED",
                "FONT_CATALOG_FIXED_ONLY",
                "REQUESTED_FAMILY_UNAVAILABLE",
                "CJK_SAMPLE_HAS_NO_INK",
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

    def test_default_prefers_live_core_then_formal_and_development_fallbacks(self) -> None:
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
        self.assertIn("/tmp/msys-main/.msysd.lock", command)
        self.assertIn("/tmp/msys-main/msysd.pid", command)
        self.assertLess(command.index(".msysd.lock"), command.index("msysd.pid"))
        self.assertIn("grep -Fqx -- 'msys_core.msysd'", command)
        self.assertIn("/proc/$msys_pid/exe", command)
        self.assertIn('exec "$active_python"', command)
        self.assertIn(f"if test -x '{current}'", command)
        self.assertIn(f"elif test -x '{development}'", command)
        self.assertLess(command.index(current), command.index(development))
        self.assertEqual(command.count("PYTHONDONTWRITEBYTECODE=1"), 1)
        self.assertEqual(command.count("msys_tools.remote_font_probe"), 3)
        self.assertEqual(command.count("'-B'"), 3)

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
